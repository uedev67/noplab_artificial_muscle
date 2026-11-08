"""Desktop controls for the manual profile recorder (voltage / power / current)."""

from __future__ import annotations

import math
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import control_modes
import manual_voltage_profile as recorder
import profile_builder
import profile_store
from control_modes import LIMIT_FIELDS, MODES

MODE_FILTERS = {"전체": None, "전압": "voltage", "전력": "power", "전류": "current"}

MODE_HINTS = {
    "voltage": "각 행 값 = 설정 전압. 장비는 전압 소스로 동작합니다.",
    "power": "각 행 값 = 목표 전력. 전압 소스 + 소프트웨어 루프 (V = √(P·R)), "
             "0 W 행은 0 V (측정 전류 사용 시 V = I_probe·R).",
    "current": "각 행 값 = 설정 전류(mA). 장비는 전류 소스로 동작합니다. "
               "PTC 시료는 가열될수록 전력이 커지므로 P_max를 꼭 확인하세요.",
}

SAFETY_NOTE = (
    "보호 3단계: ① 시작 전 모든 행 검사 ② 장비 compliance ③ 매 샘플 검사.\n"
    "③에서 초과하면 즉시 0 출력 + 출력 OFF, CSV abort_reason에 사유를 기록합니다."
)


def output_path(directory: str, sample: str, cycle: str) -> Path:
    if not re.fullmatch(r"[0-9]+", sample):
        raise ValueError("샘플 번호는 0 이상의 정수로 입력하세요.")
    if not re.fullmatch(r"[0-9]+", cycle) or int(cycle) < 1:
        raise ValueError("사이클 번호는 1 이상의 정수로 입력하세요.")
    if not directory.strip():
        raise ValueError("저장 폴더를 선택하세요.")
    return Path(directory).expanduser() / (
        f"{datetime.now():%m%d}_샘플{sample}_{cycle}_저항.csv"
    )


def format_number(value: float) -> str:
    return f"{float(value):.10g}"


def to_si(display_value: float, scale: float) -> float:
    # Round away binary noise such as 160 * 1e-3 = 0.16000000000000003.
    return float(f"{display_value * scale:.12g}")


def format_duration(seconds: float) -> str:
    if seconds >= 60:
        return f"{int(seconds // 60)}분 {seconds % 60:g}초"
    return f"{seconds:g}초"


class QueueWriter:
    def __init__(self, messages):
        self.messages = messages

    def write(self, text):
        if text:
            self.messages.put(("log", text))
        return len(text)

    def flush(self):
        pass


def ask_mode_change_dialog(parent, old_label: str, new_label: str):
    """Modal [유지 / 비우기 / 취소] dialog.  Returns "keep", "clear" or None."""
    dialog = tk.Toplevel(parent)
    dialog.title("제어 모드 변경")
    dialog.transient(parent)
    dialog.resizable(False, False)
    result = {"value": None}
    ttk.Label(
        dialog, padding=16, justify="left",
        text=(f"{old_label} → {new_label}\n\n"
              "프로파일 값의 의미와 단위가 바뀝니다.\n"
              "현재 표의 숫자를 그대로 유지할까요, 비울까요?"),
    ).pack()
    buttons = ttk.Frame(dialog, padding=(16, 0, 16, 16))
    buttons.pack(fill="x")

    def choose(value):
        result["value"] = value
        dialog.destroy()

    for text, value in (("유지", "keep"), ("비우기", "clear"), ("취소", None)):
        ttk.Button(buttons, text=text, command=lambda v=value: choose(v)).pack(
            side="left", padx=4)
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


class ProfileListDialog:
    """Saved + built-in profiles with mode filter, name search, load, delete."""

    COLUMNS = (("name", "이름", 250), ("mode", "모드", 60), ("steps", "단계", 50),
               ("total", "총 시간", 140), ("updated", "수정일", 130), ("kind", "구분", 100))

    def __init__(self, gui):
        self.gui = gui
        self.window = tk.Toplevel(gui.root)
        self.window.title("프로파일 목록")
        self.window.geometry("820x440")
        self.window.transient(gui.root)
        self.items = {}

        top = ttk.Frame(self.window, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="모드").pack(side="left")
        self.filter = tk.StringVar(value="전체")
        ttk.Combobox(top, textvariable=self.filter, values=list(MODE_FILTERS),
                     state="readonly", width=6).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="이름 검색").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(top, textvariable=self.search, width=30).pack(side="left", padx=4)
        self.filter.trace_add("write", lambda *_: self.refresh())
        self.search.trace_add("write", lambda *_: self.refresh())

        body = ttk.Frame(self.window, padding=(8, 0))
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key == "name" else "center")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _: self.load_selected())
        self.tree.bind("<Return>", lambda _: self.load_selected())

        bottom = ttk.Frame(self.window, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="불러오기", command=self.load_selected).pack(side="left")
        ttk.Button(bottom, text="삭제", command=self.delete_selected).pack(side="left", padx=6)
        ttk.Button(bottom, text="새로고침", command=self.refresh).pack(side="left")
        ttk.Button(bottom, text="닫기", command=self.window.destroy).pack(side="right")
        self.error = tk.StringVar()
        ttk.Label(self.window, textvariable=self.error, foreground="#b00020",
                  padding=(8, 0, 8, 8), wraplength=740).pack(fill="x")
        self.refresh()

    def refresh(self):
        summaries, errors = profile_store.list_profiles(self.gui.profile_dir)
        mode = MODE_FILTERS.get(self.filter.get())
        query = self.search.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for summary in summaries:
            if mode and summary["mode"] != mode:
                continue
            if query and query not in summary["name"].casefold():
                continue
            iid = self.tree.insert("", "end", values=(
                summary["name"], MODES[summary["mode"]].quantity, summary["steps"],
                format_duration(summary["total_s"])
                if summary.get("total_min_s", summary["total_s"]) == summary["total_s"]
                else f"{format_duration(summary['total_min_s'])}~{format_duration(summary['total_s'])}",
                summary["updated_at"][:16].replace("T", " "),
                "기본 (읽기 전용)" if summary["builtin"] else "사용자"))
            self.items[iid] = summary
        self.error.set(
            f"읽을 수 없는 프로파일 파일 {len(errors)}개: " + " / ".join(errors) if errors else "")

    def selected(self):
        selection = self.tree.selection()
        return self.items.get(selection[0]) if selection else None

    def load_selected(self):
        summary = self.selected()
        if summary is None:
            messagebox.showinfo("프로파일 목록", "불러올 프로파일을 선택하세요.", parent=self.window)
            return
        if self.gui.load_profile_summary(summary, parent=self.window):
            self.window.destroy()

    def delete_selected(self):
        summary = self.selected()
        if summary is None:
            messagebox.showinfo("프로파일 목록", "삭제할 프로파일을 선택하세요.", parent=self.window)
            return
        if self.gui.delete_profile(summary, parent=self.window):
            self.refresh()


# ==========================================================================
# Step options / cooling / block / sweep dialogs
# ==========================================================================

STEP_KIND_LABELS = {
    "fixed": "고정 시간",
    "rref": "조건부 · R_ref 복귀",
    "slope": "조건부 · R 안정(기울기)",
    "rref_step": "R_ref 측정 단계",
}

# key: (label, unit, kinds that show the field)
COOLING_FIELDS = {
    "duration_s": ("냉각 시간", "s", ("fixed",)),
    "t_min_s": ("최소 시간 t_min", "s", ("rref", "slope")),
    "t_max_s": ("최대 시간 t_max", "s", ("rref", "slope")),
    "tol_pct": ("허용 오차 |R−R_ref|/R_ref", "%", ("rref",)),
    "k_samples": ("연속 샘플 수 k", "개", ("rref",)),
    "window_s": ("기울기 창 w", "s", ("slope",)),
    "slope_pct_per_s": ("기울기 기준 |dR/dt|/R", "%/s", ("slope",)),
    "t_on_s": ("on 시간 (측정 전류)", "s", ("rref_step",)),
    "t_off_s": ("off 시간 (0 출력)", "s", ("rref_step",)),
    "repeats": ("반복 횟수 n", "회", ("rref_step",)),
}

COOLING_DEFAULTS = {
    "duration_s": 15.0,
    "t_max_s": control_modes.DEFAULT_T_MAX_S,
    **control_modes.DEFAULT_END["rref"],
    **control_modes.DEFAULT_END["slope"],
    **control_modes.DEFAULT_RREF_STEP,
}

CONDITIONAL_HINT = "조건부 종료는 [냉각 중 측정 전류 사용]이 켜져 있어야 고를 수 있습니다."


def options_label(options: dict) -> str:
    if options.get("kind") == "rref":
        return "R_ref 측정"
    end = options.get("end")
    if end is None:
        return "고정"
    return "조건·R_ref" if end["type"] == "rref" else "조건·안정"


def _parse(text, what):
    try:
        number = float(str(text).strip())
    except ValueError:
        raise ValueError(f"{what}: 숫자를 입력하세요.") from None
    if not math.isfinite(number):
        raise ValueError(f"{what}: 유한한 숫자를 입력하세요.")
    return number


class CoolingForm:
    """Radio choice of step end / cooling kind plus the matching fields.

    ``get()`` returns
        {"type": "fixed"[, "duration_s"]}
        {"type": "rref" | "slope", "t_max_s", ...}     (normalized)
        {"type": "rref_step", "rref": {...}}
    """

    def __init__(self, parent, kinds, conditional_allowed, initial=None,
                 fixed_duration=True, disabled_hint=CONDITIONAL_HINT):
        initial = dict(initial or {})
        self.frame = ttk.Frame(parent)
        self.kinds = kinds
        self.fixed_duration = fixed_duration
        start = initial.get("type", kinds[0])
        if start in ("rref", "slope") and not conditional_allowed:
            start = kinds[0]
        self.kind = tk.StringVar(value=start)
        radios = ttk.Frame(self.frame)
        radios.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.radios = {}
        for kind in kinds:
            radio = ttk.Radiobutton(radios, text=STEP_KIND_LABELS[kind], value=kind,
                                    variable=self.kind, command=self.refresh)
            if len(kinds) > 1:   # a single choice needs no radio button
                radio.pack(anchor="w")
            if kind in ("rref", "slope") and not conditional_allowed:
                radio.state(["disabled"])
            self.radios[kind] = radio
        if not conditional_allowed and ("rref" in kinds or "slope" in kinds):
            ttk.Label(radios, text=disabled_hint, foreground="#b00020",
                      wraplength=320).pack(anchor="w")
        self.vars, self.rows = {}, {}
        values = dict(COOLING_DEFAULTS)
        values.update({k: v for k, v in initial.items() if k in COOLING_FIELDS})
        values.update(initial.get("rref") or {})
        for row, (key, (label, unit, shown)) in enumerate(COOLING_FIELDS.items(), 1):
            if not any(kind in kinds for kind in shown):
                continue
            if key == "duration_s" and not fixed_duration:
                continue
            value = values[key]
            self.vars[key] = tk.StringVar(
                value=str(int(value)) if key in ("k_samples", "repeats") else format_number(value))
            widgets = (ttk.Label(self.frame, text=label),
                       ttk.Entry(self.frame, textvariable=self.vars[key], width=9),
                       ttk.Label(self.frame, text=unit))
            for col, widget in enumerate(widgets):
                widget.grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)
            self.rows[key] = (shown, widgets)
        self.refresh()

    def refresh(self):
        kind = self.kind.get()
        for shown, widgets in self.rows.values():
            for widget in widgets:
                if kind in shown:
                    widget.grid()
                else:
                    widget.grid_remove()

    def get(self) -> dict:
        kind = self.kind.get()
        values = {}
        for key, (shown, _) in self.rows.items():
            if kind in shown:
                label = COOLING_FIELDS[key][0]
                number = _parse(self.vars[key].get(), label)
                values[key] = int(number) if key in ("k_samples", "repeats") \
                    and number == int(number) else number
        if kind == "rref_step":
            return {"type": "rref_step", "rref": control_modes.normalize_rref_step(values)}
        if kind == "fixed":
            if not self.fixed_duration:
                return {"type": "fixed"}
            return profile_builder.normalize_cooling({"type": "fixed", **values})
        return profile_builder.normalize_cooling({"type": kind, **values})


class _Dialog:
    """Small non-blocking Toplevel (tests drive it without wait_window)."""

    def __init__(self, gui, title):
        self.gui = gui
        self.window = tk.Toplevel(gui.root)
        self.window.title(title)
        self.window.transient(gui.root)
        self.window.resizable(False, False)
        self.body = ttk.Frame(self.window, padding=12)
        self.body.pack(fill="both", expand=True)

    def buttons(self, *specs):
        bar = ttk.Frame(self.window, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        for text, command in specs:
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="닫기", command=self.window.destroy).pack(side="right")

    def error(self, text):
        messagebox.showerror(self.window.title(), text, parent=self.window)


class StepOptionDialog(_Dialog):
    """End of one table row: fixed / conditional (R_ref, slope) / R_ref step."""

    def __init__(self, gui, item):
        super().__init__(gui, "단계 종료 방식")
        self.item = item
        options = gui.row_options_of(item)
        variables = item[0]
        try:
            value_is_zero = float(variables[0].get()) == 0.0
        except ValueError:
            value_is_zero = False
        allowed = gui.probe_enabled.get() and value_is_zero
        hint = CONDITIONAL_HINT if value_is_zero else "조건부 종료는 값이 0인 단계에서만 쓸 수 있습니다."
        if options.get("kind") == "rref":
            initial = {"type": "rref_step", "rref": options["rref"]}
        elif "end" in options:
            initial = dict(options["end"], t_max_s=variables[1].get())
        else:
            initial = {"type": "fixed"}
        ttk.Label(self.body, text=f"{gui.rows.index(item) + 1}행").pack(anchor="w")
        self.form = CoolingForm(self.body, ("fixed", "rref", "slope", "rref_step"), allowed,
                                initial, fixed_duration=False, disabled_hint=hint)
        self.form.frame.pack(fill="x")
        self.buttons(("확인", self.ok))

    def ok(self):
        try:
            choice = self.form.get()
        except ValueError as exc:
            self.error(str(exc))
            return False
        self.gui.set_row_options(self.item, choice)
        self.window.destroy()
        return True


class RrefStepDialog(_Dialog):
    """[R_ref 측정 단계 추가]: on/off/n and the insert position."""

    def __init__(self, gui):
        super().__init__(gui, "R_ref 측정 단계 추가")
        self.form = CoolingForm(self.body, ("rref_step",), True, {"type": "rref_step"})
        self.form.frame.pack(fill="x")
        ttk.Label(self.body, foreground="#444", wraplength=330, justify="left",
                  text="측정 전류(I_probe)로 on / 0 출력으로 off를 n회 반복합니다. "
                       "R_ref = 각 on 구간 마지막 3초 평균의 평균.").pack(anchor="w", pady=(6, 4))
        self.position = tk.StringVar(value="after_idle")
        for value, text in (("after_idle", "맨 앞 0 단계 다음 (권장)"), ("end", "맨 끝")):
            ttk.Radiobutton(self.body, text=text, value=value,
                            variable=self.position).pack(anchor="w")
        self.buttons(("추가", self.ok))

    def ok(self):
        try:
            spec = self.form.get()["rref"]
        except ValueError as exc:
            self.error(str(exc))
            return False
        self.gui.insert_rref_step(spec, at_start=self.position.get() == "after_idle")
        self.window.destroy()
        return True


class BlockDialog(_Dialog):
    """[반복 블록 추가]: value, heat time, cooling, n, note prefix -> rows."""

    def __init__(self, gui):
        super().__init__(gui, "반복 블록 추가")
        info = MODES[gui.mode]
        self.vars = {key: tk.StringVar(value=value) for key, value in (
            ("value", ""), ("heat_s", "8"), ("repeats", "3"), ("prefix", ""))}
        for row, (key, label, unit) in enumerate((
                ("value", f"가열 값 ({info.quantity})", info.unit),
                ("heat_s", "가열 시간", "s"),
                ("repeats", "반복 횟수 n", "회"),
                ("prefix", "메모 접두어 (비우면 값)", ""))):
            ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(self.body, textvariable=self.vars[key], width=14).grid(
                row=row, column=1, sticky="w", padx=6)
            ttk.Label(self.body, text=unit).grid(row=row, column=2, sticky="w")
        ttk.Label(self.body, text="냉각 방식", font=gui.bold_font).grid(
            row=4, column=0, sticky="w", pady=(8, 2))
        self.cooling = CoolingForm(self.body, ("fixed", "rref", "slope"),
                                   gui.probe_enabled.get())
        self.cooling.frame.grid(row=5, column=0, columnspan=3, sticky="w")
        self.buttons(("추가", self.ok))

    def build(self):
        scale = MODES[self.gui.mode].scale
        value = to_si(_parse(self.vars["value"].get(), "가열 값"), scale)
        return profile_builder.block_steps(
            self.gui.mode, value, _parse(self.vars["heat_s"].get(), "가열 시간"),
            self.cooling.get(), _parse(self.vars["repeats"].get(), "반복 횟수"),
            self.vars["prefix"].get())

    def ok(self):
        try:
            steps = self.build()
        except ValueError as exc:
            self.error(str(exc))
            return False
        self.gui.add_steps(steps)
        self.gui.log_message(f"반복 블록 추가: {len(steps)}행")
        self.window.destroy()
        return True


class SweepDialog(_Dialog):
    """[기준 복귀형 sweep 생성]: build the whole profile, preview, apply."""

    def __init__(self, gui):
        super().__init__(gui, "기준 복귀형 sweep 생성")
        info = MODES[gui.mode]
        self.vars = {key: tk.StringVar(value=value) for key, value in (
            ("idle_s", "5"), ("ref_value", ""), ("ref_initial", "2"),
            ("values", ""), ("start", ""), ("end", ""), ("step", ""),
            ("test_repeats", "3"), ("ref_between", "1"), ("heat_s", "8"))}
        self.include_rref = tk.BooleanVar(value=False)
        self.value_source = tk.StringVar(value="list")
        unit = info.unit
        rows = (("idle_s", "맨 앞 0 단계 (0이면 없음)", "s"),
                ("ref_value", f"기준값 ({info.quantity})", unit),
                ("ref_initial", "시작 기준 반복 수", "회"))
        for row, (key, label, unit_text) in enumerate(rows):
            self._entry(row, key, label, unit_text)
        ttk.Checkbutton(self.body, text="R_ref 측정 단계 포함 (맨 앞 0 단계 다음)",
                        variable=self.include_rref).grid(row=3, column=0, columnspan=3,
                                                         sticky="w", pady=2)
        self.rref = CoolingForm(self.body, ("rref_step",), True, {"type": "rref_step"})
        self.rref.frame.grid(row=4, column=0, columnspan=4, sticky="w", padx=(20, 0))
        ttk.Radiobutton(self.body, text=f"시험값 목록 (쉼표 구분, {unit})", value="list",
                        variable=self.value_source).grid(row=5, column=0, sticky="w", pady=(6, 2))
        ttk.Entry(self.body, textvariable=self.vars["values"], width=30).grid(
            row=5, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Radiobutton(self.body, text=f"시작 / 끝 / 간격 ({unit})", value="range",
                        variable=self.value_source).grid(row=6, column=0, sticky="w")
        range_box = ttk.Frame(self.body)
        range_box.grid(row=6, column=1, columnspan=3, sticky="w", padx=6)
        for key in ("start", "end", "step"):
            ttk.Entry(range_box, textvariable=self.vars[key], width=8).pack(side="left", padx=(0, 4))
        for row, (key, label, unit_text) in enumerate((
                ("test_repeats", "시험값당 반복 수", "회"),
                ("ref_between", "시험값 사이 기준 반복 수", "회"),
                ("heat_s", "가열 시간", "s")), 7):
            self._entry(row, key, label, unit_text)
        ttk.Label(self.body, text="냉각 방식", font=gui.bold_font).grid(
            row=10, column=0, sticky="w", pady=(8, 2))
        self.cooling = CoolingForm(self.body, ("fixed", "rref", "slope"),
                                   gui.probe_enabled.get())
        self.cooling.frame.grid(row=11, column=0, columnspan=4, sticky="w")
        self.preview = tk.StringVar(value="[미리보기]를 누르면 단계 수와 예상 시간을 계산합니다.")
        self.preview_label = ttk.Label(self.body, textvariable=self.preview, justify="left",
                                       wraplength=420)
        self.preview_label.grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.buttons(("미리보기", self.show_preview), ("적용", self.apply))

    def _entry(self, row, key, label, unit):
        ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self.body, textvariable=self.vars[key], width=10).grid(
            row=row, column=1, sticky="w", padx=6)
        ttk.Label(self.body, text=unit).grid(row=row, column=2, sticky="w")

    def build(self):
        gui = self.gui
        scale = MODES[gui.mode].scale
        if self.value_source.get() == "list":
            values = profile_builder.parse_value_list(self.vars["values"].get())
        else:
            values = profile_builder.value_range(
                _parse(self.vars["start"].get(), "시작 값"),
                _parse(self.vars["end"].get(), "끝 값"),
                _parse(self.vars["step"].get(), "간격"))
        return profile_builder.sweep_steps(
            gui.mode,
            ref_value=to_si(_parse(self.vars["ref_value"].get(), "기준값"), scale),
            test_values=[to_si(v, scale) for v in values],
            heat_s=_parse(self.vars["heat_s"].get(), "가열 시간"),
            cooling=self.cooling.get(),
            ref_initial=_parse(self.vars["ref_initial"].get(), "시작 기준 반복 수"),
            test_repeats=_parse(self.vars["test_repeats"].get(), "시험값당 반복 수"),
            ref_between=_parse(self.vars["ref_between"].get(), "시험값 사이 기준 반복 수"),
            idle_s=_parse(self.vars["idle_s"].get(), "맨 앞 0 단계"),
            include_rref=self.include_rref.get(),
            rref_spec=self.rref.get()["rref"] if self.include_rref.get() else None)

    def summary_text(self, steps):
        gui = self.gui
        info = MODES[gui.mode]
        try:
            flat = control_modes.runtime_limits(
                gui.mode, gui.read_limits(control_modes.groups_for_mode(gui.mode)))
        except ValueError as exc:
            return None, f"안전 상한을 읽을 수 없습니다: {exc}"
        summary = profile_builder.summarize(steps, gui.mode, flat)
        low, high = summary["total_min_s"], summary["total_max_s"]
        time_text = format_duration(high) if low == high else \
            f"{format_duration(low)} ~ {format_duration(high)} (냉각 최소/최대 기준)"
        peak = summary["max_value"] / info.scale
        lines = [f"총 {summary['steps']}단계 · 가열 사이클 {summary['cycles']}회",
                 f"총 예상 시간: {time_text}",
                 f"최대값: {format_number(peak)} {info.unit}"]
        if summary["violations"]:
            lines.append("안전 상한 초과 → 적용할 수 없습니다:")
            lines += summary["violations"][:5]
            if len(summary["violations"]) > 5:
                lines.append(f"... 외 {len(summary['violations']) - 5}개")
        else:
            lines.append("모든 단계가 현재 안전 상한 이하입니다.")
        return summary, "\n".join(lines)

    def show_preview(self):
        try:
            steps = self.build()
        except ValueError as exc:
            self.preview.set(f"입력 확인: {exc}")
            return None
        summary, text = self.summary_text(steps)
        self.preview.set(text)
        return steps, summary

    def apply(self):
        result = self.show_preview()
        if result is None:
            self.error(self.preview.get())
            return False
        steps, summary = result
        if summary is None or summary["violations"]:
            self.error(self.preview.get())
            return False
        question = self.preview.get() + "\n\n"
        question += ("현재 표의 행을 모두 지우고 이 sweep으로 바꿀까요?" if self.gui.rows
                     else "이 sweep을 표에 채울까요?")
        if not messagebox.askyesno("sweep 적용", question, parent=self.window):
            return False
        self.gui.replace_steps(steps)
        self.gui.log_message(f"기준 복귀형 sweep 적용: {summary['steps']}단계")
        self.window.destroy()
        return True


class ExperimentGUI:
    def __init__(self, root, profile_dir=None, settings_path=None):
        self.root = root
        root.title("Keithley 2450 · 제어 프로파일 측정")
        root.geometry("1180x940")
        root.minsize(900, 700)
        self.profile_dir = Path(profile_dir or profile_store.PROFILE_DIR)
        self.settings_path = Path(settings_path or profile_store.SETTINGS_PATH)
        self.messages = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.closing = False
        self.variables = {}
        self.rows = []
        self.row_options = {}   # id(row variables) -> step options dict
        self.measured_r_ref = None   # R_ref measured in the current / last run
        self.editors = []
        self.mode = "voltage"
        self.current_profile = None   # {"name": str, "builtin": bool}
        self.dirty = False
        self._suspend_dirty = True
        self.profile_list = None

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=12, pady=10)
        basic = ttk.Frame(notebook, padding=12)
        advanced = ttk.Frame(notebook, padding=12)
        notebook.add(basic, text="측정 / 제어 프로파일")
        notebook.add(advanced, text="장비 / 실험 정보")
        basic.columnconfigure(1, weight=1)
        self.field(basic, 0, "샘플 번호", "sample", "")
        self.field(basic, 1, "사이클 번호 (반복 횟수 아님)", "cycle", "1")
        self.field(basic, 2, "저장 폴더", "directory", str(Path(__file__).resolve().parent / "data"))
        browse = ttk.Button(basic, text="폴더 선택", command=self.browse)
        browse.grid(row=2, column=2, padx=5)
        self.editors.append(browse)
        self.preview = tk.StringVar()
        ttk.Label(basic, textvariable=self.preview).grid(row=3, column=0, columnspan=3, sticky="w", pady=6)
        for key in ("sample", "cycle", "directory"):
            self.variables[key].trace_add("write", self.update_preview)
        self.update_preview()

        # ------------------------------------------------------------------
        # Control mode buttons
        # ------------------------------------------------------------------
        mode_bar = ttk.Frame(basic)
        mode_bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 2))
        ttk.Label(mode_bar, text="제어 모드").pack(side="left", padx=(0, 8))
        self.normal_font = tkfont.nametofont("TkDefaultFont")
        self.bold_font = self.normal_font.copy()
        self.bold_font.configure(weight="bold")
        self.mode_var = tk.StringVar(value=self.mode)
        self.mode_buttons = {}
        for key, info in MODES.items():
            button = tk.Radiobutton(
                mode_bar, text=info.label, variable=self.mode_var, value=key,
                indicatoron=False, width=12, pady=4, selectcolor="#bcd7ff",
                command=self.on_mode_button)
            button.pack(side="left", padx=2)
            self.mode_buttons[key] = button
        self.mode_hint = tk.StringVar()
        ttk.Label(basic, textvariable=self.mode_hint, foreground="#444").grid(
            row=5, column=0, columnspan=3, sticky="w")

        # ------------------------------------------------------------------
        # Profile toolbar
        # ------------------------------------------------------------------
        toolbar = ttk.Frame(basic)
        toolbar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        for text, command in (("프로파일 목록", self.open_profile_list),
                              ("저장", self.save_profile),
                              ("다른 이름으로 저장", self.save_profile_as),
                              ("삭제", self.delete_profile)):
            button = ttk.Button(toolbar, text=text, command=command)
            button.pack(side="left", padx=(0, 4))
            self.editors.append(button)
        self.profile_status = tk.StringVar()
        self.profile_status_label = ttk.Label(toolbar, textvariable=self.profile_status)
        self.profile_status_label.pack(side="left", padx=10)
        description_bar = ttk.Frame(basic)
        description_bar.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(2, 6))
        ttk.Label(description_bar, text="프로파일 설명").pack(side="left", padx=(0, 8))
        self.description = tk.StringVar()
        description = ttk.Entry(description_bar, textvariable=self.description)
        description.pack(side="left", fill="x", expand=True)
        self.editors.append(description)
        self.description.trace_add("write", self.mark_dirty)

        # ------------------------------------------------------------------
        # Profile table (left) + safety limits (right)
        # ------------------------------------------------------------------
        content = ttk.Frame(basic)
        content.grid(row=8, column=0, columnspan=3, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        basic.rowconfigure(8, weight=1)

        profile = ttk.LabelFrame(content, text="제어 프로파일 · 위에서 아래로 한 번 실행", padding=6)
        profile.grid(row=0, column=0, sticky="nsew")
        canvas = tk.Canvas(profile, height=200, highlightthickness=0)
        scrollbar = ttk.Scrollbar(profile, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.row_frame = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.row_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.row_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self.headers = []
        for col, label in enumerate(("단계", "전압 (V)", "시간 (초)", "종료", "메모", "")):
            header = ttk.Label(self.row_frame, text=label)
            header.grid(row=0, column=col, padx=6)
            self.headers.append(header)
        self.value_header = self.headers[1]
        self.row_frame.columnconfigure(4, weight=1)
        for entry in recorder.PROFILE:
            voltage, duration, _, note = recorder.parse_profile_entry(entry)
            self.add_row(format_number(voltage), format_number(duration), note)

        side = ttk.Frame(content)
        side.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.build_safety_panel(side)
        self.build_probe_panel(side)

        row_buttons = ttk.Frame(basic)
        row_buttons.grid(row=9, column=0, columnspan=3, sticky="w", pady=6)
        for text, command in (("+ 행 추가", self.add_row),
                              ("R_ref 측정 단계 추가", self.open_rref_dialog),
                              ("반복 블록 추가", self.open_block_dialog),
                              ("기준 복귀형 sweep 생성", self.open_sweep_dialog)):
            button = ttk.Button(row_buttons, text=text, command=command)
            button.pack(side="left", padx=(0, 4))
            self.editors.append(button)

        # ------------------------------------------------------------------
        # Advanced tab
        # ------------------------------------------------------------------
        advanced.columnconfigure(1, weight=1)
        settings = [
            ("센싱 방식", "WIRE_MODE", recorder.WIRE_MODE),
            ("샘플링 간격 (초)", "SAMPLE_INTERVAL_S", recorder.SAMPLE_INTERVAL_S),
            ("VISA resource", "RESOURCE_NAME", recorder.RESOURCE_NAME),
            ("Arduino COM 포트", "SERIAL_MARKER_PORT", recorder.SERIAL_MARKER_PORT),
            ("Arduino baudrate", "SERIAL_MARKER_BAUDRATE", recorder.SERIAL_MARKER_BAUDRATE),
        ]
        for row, (label, key, value) in enumerate(settings):
            self.field(advanced, row, label, key, value, choices=(2, 4) if key == "WIRE_MODE" else None)
        ttk.Label(advanced, foreground="#444",
                  text="전류 제한(I_lim) / 전압 제한(V_lim)은 첫 탭의 '안전 상한'에서 설정합니다.").grid(
            row=len(settings), column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.marker = tk.BooleanVar(value=recorder.use_serial_marker)
        self.dummy = tk.BooleanVar(value=False)
        first = len(settings) + 1
        for row, label, variable in ((first, "Arduino 시작 마커 사용", self.marker),
                                     (first + 1, "더미 모드 (장비 연결 없이 테스트)", self.dummy)):
            widget = ttk.Checkbutton(advanced, text=label, variable=variable)
            widget.grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
            self.editors.append(widget)
        metadata = [
            ("실험 ID", "EXPERIMENT_ID"), ("실험자", "OPERATOR"),
            ("Preload (g)", "PRELOAD_G"), ("초기 길이 (mm)", "INITIAL_LENGTH_MM"),
            ("주변 온도 (°C)", "AMBIENT_TEMP_C"), ("IR 카메라 ID", "IR_CAMERA_ID"),
            ("Sony 카메라 ID", "SONY_CAMERA_ID"), ("동기화 메모", "SYNC_NOTE"),
            ("실험 메모", "GENERAL_NOTE"),
        ]
        self.metadata_keys = [key for _, key in metadata]
        for row, (label, key) in enumerate(metadata, first + 2):
            self.field(advanced, row, label, key, getattr(recorder, key))

        controls = ttk.Frame(root, padding=(12, 0))
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="시작 (키슬리 실행)", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="중지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.status = tk.StringVar(value="대기 중 · 시작 버튼을 누르면 장비에 연결합니다.")
        ttk.Label(controls, textvariable=self.status).pack(side="left")
        self.log = ScrolledText(root, height=8, state="disabled", wrap="word")
        self.log.pack(fill="both", padx=12, pady=10)

        self.restore_last_settings()
        self._suspend_dirty = False
        self.dirty = False
        self.apply_mode_view()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    # ======================================================================
    # Widgets
    # ======================================================================

    def field(self, parent, row, label, key, value, choices=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        variable = tk.StringVar(value=str(value))
        self.variables[key] = variable
        if choices:
            widget = ttk.Combobox(parent, textvariable=variable, values=choices, state="readonly")
        else:
            widget = ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        self.editors.append(widget)

    def build_safety_panel(self, parent):
        self.safety_frame = ttk.LabelFrame(parent, text="안전 상한", padding=8)
        self.safety_frame.grid(row=0, column=0, sticky="nsew")
        self.limit_vars = {}
        self.limit_frames = {}
        titles = {"common": "공통", "power_control": "전력 제어 세부"}
        for row, group in enumerate(("voltage", "current", "power", "common", "power_control")):
            frame = ttk.Frame(self.safety_frame)
            frame.grid(row=0 if group in MODES else row - 2, column=0, sticky="ew", pady=(0, 6))
            frame.columnconfigure(0, minsize=215)   # align entries across groups
            self.limit_frames[group] = frame
            if group in titles:
                ttk.Label(frame, text=titles[group], font=self.bold_font).grid(
                    row=0, column=0, columnspan=3, sticky="w", pady=(4, 2))
            self.limit_vars[group] = {}
            for index, field in enumerate(LIMIT_FIELDS[group], 1):
                default = control_modes.DEFAULT_LIMITS[group][field.key]
                variable = tk.StringVar(value=self.limit_display(field, default))
                variable.trace_add("write", self.mark_dirty)
                self.limit_vars[group][field.key] = variable
                ttk.Label(frame, text=field.label).grid(row=index, column=0, sticky="w", pady=2)
                entry = ttk.Entry(frame, textvariable=variable, width=9)
                entry.grid(row=index, column=1, padx=4, pady=2)
                ttk.Label(frame, text=field.unit).grid(row=index, column=2, sticky="w")
                self.editors.append(entry)
        ttk.Label(self.safety_frame, text=SAFETY_NOTE, wraplength=300, foreground="#444",
                  justify="left").grid(row=3, column=0, sticky="w", pady=(6, 0))

    def build_probe_panel(self, parent):
        """Probe current in 0 steps, manual R_ref, cooling timeout option."""
        frame = ttk.LabelFrame(parent, text="냉각 중 측정 전류 · R_ref", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        defaults = control_modes.DEFAULT_PROBE
        self.probe_enabled = tk.BooleanVar(value=defaults["enabled"])
        self.probe_vars = {
            "I_probe_A": tk.StringVar(value=format_number(defaults["I_probe_A"] * 1e3)),
            "V_probe_max_V": tk.StringVar(value=format_number(defaults["V_probe_max_V"])),
            "R_jump_skip_samples": tk.StringVar(value=str(defaults["R_jump_skip_samples"])),
        }
        self.r_ref_manual = tk.StringVar(value="")
        self.timeout_abort = tk.BooleanVar(value=control_modes.DEFAULT_COOLING["timeout_abort"])
        self.timeout_count = tk.StringVar(
            value=str(control_modes.DEFAULT_COOLING["timeout_abort_count"]))
        check = ttk.Checkbutton(frame, text="냉각 중 측정 전류 사용 (값 0 단계)",
                                variable=self.probe_enabled)
        check.grid(row=0, column=0, columnspan=6, sticky="w")
        self.editors.append(check)
        # Two label/entry/unit groups per row keep the side column short.
        self._panel_entry(frame, 1, 0, "I_probe", self.probe_vars["I_probe_A"], "mA")
        self._panel_entry(frame, 1, 3, "V_probe_max", self.probe_vars["V_probe_max_V"], "V")
        self._panel_entry(frame, 2, 0, "급변 검사 제외", self.probe_vars["R_jump_skip_samples"],
                          "샘플")
        self._panel_entry(frame, 2, 3, "R_ref 직접", self.r_ref_manual, "Ω")
        timeout_row = ttk.Frame(frame)
        timeout_row.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
        timeout = ttk.Checkbutton(timeout_row, text="조건부 냉각 타임아웃",
                                  variable=self.timeout_abort)
        timeout.pack(side="left")
        count = ttk.Entry(timeout_row, textvariable=self.timeout_count, width=4)
        count.pack(side="left", padx=4)
        ttk.Label(timeout_row, text="회 연속이면 안전 종료").pack(side="left")
        self.editors += [timeout, count]
        self.r_ref_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.r_ref_status, foreground="#0b5394",
                  wraplength=320).grid(row=4, column=0, columnspan=6, sticky="w", pady=(4, 0))
        for variable in (self.probe_enabled, self.r_ref_manual, self.timeout_abort,
                         self.timeout_count, *self.probe_vars.values()):
            variable.trace_add("write", self.mark_dirty)
        self.r_ref_manual.trace_add("write", lambda *_: self.update_r_ref_status())
        self.update_r_ref_status()

    def _panel_entry(self, frame, row, column, label, variable, unit):
        ttk.Label(frame, text=label).grid(row=row, column=column, sticky="w", pady=2)
        entry = ttk.Entry(frame, textvariable=variable, width=6)
        entry.grid(row=row, column=column + 1, padx=4, pady=2)
        ttk.Label(frame, text=unit).grid(row=row, column=column + 2, sticky="w",
                                         padx=(0, 10 if column == 0 else 0))
        self.editors.append(entry)

    def update_r_ref_status(self, measured=None):
        if measured is not None:
            self.r_ref_status.set(f"R_ref: {measured:.6g} Ω (측정값, 이후 R_ref 복귀 조건에 사용)")
            return
        text = self.r_ref_manual.get().strip()
        self.r_ref_status.set(f"R_ref: {text} Ω (직접 입력)" if text else
                              "R_ref: 없음 (측정 단계 또는 직접 입력 필요)")

    def read_probe_settings(self):
        """(probe dict, manual R_ref or None, cooling dict) from the panel."""
        try:
            i_probe = to_si(float(self.probe_vars["I_probe_A"].get()), 1e-3)
            v_probe = float(self.probe_vars["V_probe_max_V"].get())
            skip = float(self.probe_vars["R_jump_skip_samples"].get())
        except ValueError:
            raise ValueError("측정 전류 설정: 숫자를 입력하세요.") from None
        probe = control_modes.normalize_probe({
            "enabled": self.probe_enabled.get(), "I_probe_A": i_probe,
            "V_probe_max_V": v_probe,
            "R_jump_skip_samples": int(skip) if skip == int(skip) else skip})
        text = self.r_ref_manual.get().strip()
        manual = None
        if text:
            try:
                manual = float(text)
            except ValueError:
                raise ValueError("R_ref 직접 입력: 숫자를 입력하거나 비워 두세요.") from None
            if not math.isfinite(manual) or manual <= 0:
                raise ValueError("R_ref 직접 입력은 0보다 커야 합니다.")
        try:
            count = float(self.timeout_count.get())
        except ValueError:
            raise ValueError("타임아웃 연속 횟수: 정수를 입력하세요.") from None
        if not math.isfinite(count) or count != int(count) or count < 1:
            raise ValueError("타임아웃 연속 횟수는 1 이상의 정수여야 합니다.")
        cooling = {"timeout_abort": self.timeout_abort.get(), "timeout_abort_count": int(count)}
        return probe, manual, cooling

    def set_probe_settings(self, profile):
        probe = profile.get("probe") or control_modes.DEFAULT_PROBE
        self.probe_enabled.set(bool(probe["enabled"]))
        self.probe_vars["I_probe_A"].set(format_number(probe["I_probe_A"] * 1e3))
        self.probe_vars["V_probe_max_V"].set(format_number(probe["V_probe_max_V"]))
        self.probe_vars["R_jump_skip_samples"].set(str(int(probe["R_jump_skip_samples"])))
        manual = (profile.get("R_ref") or {}).get("manual_ohm")
        self.r_ref_manual.set("" if manual is None else format_number(manual))
        cooling = profile.get("cooling") or control_modes.DEFAULT_COOLING
        self.timeout_abort.set(bool(cooling["timeout_abort"]))
        self.timeout_count.set(str(int(cooling["timeout_abort_count"])))

    @staticmethod
    def limit_display(field, stored):
        return str(int(stored)) if field.integer else format_number(stored / field.scale)

    def add_row(self, value=0.0, duration=1.0, note="", options=None, index=None):
        variables = [tk.StringVar(value=str(item)) for item in (value, duration, note)]
        for variable in variables:
            variable.trace_add("write", self.mark_dirty)
        value_entry, duration_entry, note_entry = (
            ttk.Entry(self.row_frame, textvariable=v, width=14) for v in variables)
        option_button = ttk.Button(self.row_frame, width=10)
        widgets = [ttk.Label(self.row_frame), value_entry, duration_entry, option_button,
                   note_entry]
        item = (variables, widgets)
        option_button.configure(command=lambda: self.open_step_options(item))
        widgets.append(ttk.Button(self.row_frame, text="삭제", command=lambda: self.remove_row(item)))
        self.row_options[id(variables)] = control_modes.normalize_step_options(options)
        option_button.configure(text=options_label(self.row_options[id(variables)]))
        if index is None:
            self.rows.append(item)
        else:
            self.rows.insert(index, item)
        self.layout_rows()
        self.mark_dirty()
        return item

    def row_options_of(self, item):
        return self.row_options.get(id(item[0]), {})

    def set_row_options(self, item, choice):
        """Apply a CoolingForm choice ({"type": ...}) to a table row."""
        variables, widgets = item
        kind = choice["type"]
        if kind == "rref_step":
            options = {"kind": "rref", "rref": choice["rref"]}
            variables[0].set("0")
            variables[1].set(format_number(control_modes.rref_step_duration(choice["rref"])))
            if not variables[2].get().strip():
                variables[2].set("R_ref measure")
        elif kind == "fixed":
            options = {}
        else:
            options = {"end": {k: v for k, v in choice.items() if k != "t_max_s"}}
            variables[1].set(format_number(choice["t_max_s"]))
        self.row_options[id(variables)] = control_modes.normalize_step_options(options)
        widgets[3].configure(text=options_label(self.row_options[id(variables)]))
        self.mark_dirty()

    def open_step_options(self, item):
        if not self.running and item in self.rows:
            return StepOptionDialog(self, item)
        return None

    def add_steps(self, steps, replace=False):
        """Append (or replace the table with) profile JSON steps (SI values)."""
        scale = MODES[self.mode].scale
        if replace:
            self.clear_rows()
        for step in steps:
            options = {k: step[k] for k in ("kind", "rref", "end") if k in step}
            self.add_row(format_number(step["value"] / scale), format_number(step["duration_s"]),
                         step.get("note", ""), options)

    def replace_steps(self, steps):
        self.add_steps(steps, replace=True)

    def insert_rref_step(self, spec, at_start=True):
        """Insert an R_ref step after the leading plain 0 rows (or at the end)."""
        index = None
        if at_start:
            index = 0
            for item in self.rows:
                try:
                    is_zero = float(item[0][0].get()) == 0.0
                except ValueError:
                    is_zero = False
                if not is_zero or self.row_options_of(item):
                    break
                index += 1
        step = profile_builder.rref_step(spec)
        return self.add_row(format_number(0), format_number(step["duration_s"]), step["note"],
                            {"kind": "rref", "rref": step["rref"]}, index=index)

    def open_rref_dialog(self):
        return None if self.running else RrefStepDialog(self)

    def open_block_dialog(self):
        return None if self.running else BlockDialog(self)

    def open_sweep_dialog(self):
        return None if self.running else SweepDialog(self)

    def layout_rows(self):
        for index, (_, widgets) in enumerate(self.rows, 1):
            widgets[0].configure(text=str(index))
            for col, widget in enumerate(widgets):
                widget.grid(row=index, column=col, sticky="ew", padx=4, pady=3)

    def remove_row(self, item):
        self.rows.remove(item)
        self.row_options.pop(id(item[0]), None)
        for widget in item[1]:
            widget.destroy()
        self.layout_rows()
        self.mark_dirty()

    def clear_rows(self):
        for item in list(self.rows):
            self.remove_row(item)

    def browse(self):
        directory = filedialog.askdirectory(parent=self.root)
        if directory:
            self.variables["directory"].set(directory)

    def update_preview(self, *_):
        try:
            path = output_path(*(self.variables[k].get().strip() for k in ("directory", "sample", "cycle")))
            self.preview.set(f"저장 파일: {path.name}")
        except ValueError:
            self.preview.set("파일명: MMDD_샘플번호_사이클번호_저항.csv")

    def log_message(self, text):
        self.messages.put(("log", text + "\n"))

    # ======================================================================
    # Control mode
    # ======================================================================

    def on_mode_button(self):
        new = self.mode_var.get()
        if new == self.mode:
            return
        if self.running or not self.set_mode(new, ask=True):
            self.mode_var.set(self.mode)

    def ask_mode_change(self, old, new):
        return ask_mode_change_dialog(self.root, MODES[old].label, MODES[new].label)

    def set_mode(self, new, ask=True):
        """Switch control mode.  Returns False if the user cancelled."""
        control_modes.mode_info(new)
        if self.running:
            return False
        if new == self.mode:
            return True
        if ask and self.rows:
            choice = self.ask_mode_change(self.mode, new)
            if choice is None:
                return False
            if choice == "clear":
                self.clear_rows()
        self.mode = new
        self.mode_var.set(new)
        self.apply_mode_view()
        self.mark_dirty()
        return True

    def apply_mode_view(self):
        info = MODES[self.mode]
        self.value_header.configure(text=f"{info.quantity} ({info.unit})")
        self.safety_frame.configure(text=f"안전 상한 · {info.label}")
        self.mode_hint.set(MODE_HINTS[self.mode])
        for group in ("voltage", "current", "power", "power_control"):
            visible = group == self.mode or (group == "power_control" and self.mode == "power")
            if visible:
                self.limit_frames[group].grid()
            else:
                self.limit_frames[group].grid_remove()
        for key, button in self.mode_buttons.items():
            button.configure(font=self.bold_font if key == self.mode else self.normal_font)
        self.update_profile_status()

    # ======================================================================
    # Profile state
    # ======================================================================

    def mark_dirty(self, *_):
        if self._suspend_dirty:
            return
        self.dirty = True
        self.update_profile_status()

    def update_profile_status(self):
        if not hasattr(self, "profile_status"):
            return
        if self.current_profile is None:
            name = "(새 프로파일)"
        else:
            name = ("[기본] " if self.current_profile["builtin"] else "") + self.current_profile["name"]
        text = f"현재 프로파일: {name} · {MODES[self.mode].label}"
        if self.dirty:
            text += " · 수정됨 (저장 안 됨)"
        elif self.current_profile is not None:
            text += " · 저장됨" if not self.current_profile["builtin"] else " · 읽기 전용"
        self.profile_status.set(text)
        self.profile_status_label.configure(foreground="#b00020" if self.dirty else "")

    def read_limits(self, groups):
        """Parse limit entries of *groups* into stored (SI) values."""
        result = {}
        for group in groups:
            result[group] = {}
            for field in LIMIT_FIELDS[group]:
                text = self.limit_vars[group][field.key].get().strip()
                try:
                    number = float(text)
                except ValueError:
                    raise ValueError(f"안전 상한 '{field.label}': 숫자를 입력하세요.") from None
                if not math.isfinite(number) or number <= 0:
                    raise ValueError(f"안전 상한 '{field.label}': 0보다 큰 유한한 숫자를 입력하세요.")
                if field.integer:
                    if number != int(number):
                        raise ValueError(f"안전 상한 '{field.label}': 정수를 입력하세요.")
                    result[group][field.key] = int(number)
                else:
                    result[group][field.key] = to_si(number, field.scale)
        return result

    def readable_limits(self):
        """All groups that currently parse (used for last-settings)."""
        limits = {}
        for group in LIMIT_FIELDS:
            try:
                limits.update(self.read_limits([group]))
            except ValueError:
                pass
        return limits

    def set_limits(self, limits):
        for group, values in limits.items():
            if group not in LIMIT_FIELDS or not isinstance(values, dict):
                continue
            for field in LIMIT_FIELDS[group]:
                value = values.get(field.key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and math.isfinite(value) and value > 0:
                    self.limit_vars[group][field.key].set(self.limit_display(field, value))

    def read_steps(self):
        """Rows as [(SI value, duration_s, note)]."""
        scale = MODES[self.mode].scale
        steps = []
        for index, (variables, _) in enumerate(self.rows, 1):
            try:
                value, duration = (float(v.get()) for v in variables[:2])
            except ValueError:
                raise ValueError(f"{index}행: 값과 시간은 숫자로 입력하세요.") from None
            if not math.isfinite(value) or not math.isfinite(duration) or duration <= 0:
                raise ValueError(f"{index}행: 값은 유한한 숫자, 시간은 0보다 큰 숫자여야 합니다.")
            steps.append((to_si(value, scale), duration, variables[2].get()))
        if not steps:
            raise ValueError("제어 프로파일을 한 행 이상 입력하세요.")
        return steps

    def read_step_options(self):
        return [self.row_options_of(item) for item in self.rows]

    def restore_last_settings(self):
        saved = profile_store.load_last_settings(self.settings_path)
        if isinstance(saved.get("limits"), dict):
            self.set_limits(saved["limits"])
        if any(key in saved for key in ("probe", "R_ref", "cooling")):
            try:
                self.set_probe_settings(profile_store.normalize_cooling_settings(saved))
            except (ValueError, TypeError, KeyError):
                pass

    def save_last_settings(self):
        data = {"limits": self.readable_limits()}
        try:
            probe, manual, cooling = self.read_probe_settings()
            data.update(probe=probe, R_ref={"manual_ohm": manual}, cooling=cooling)
        except ValueError:
            pass
        try:
            profile_store.save_last_settings(data, self.settings_path)
        except OSError as exc:
            self.log_message(f"마지막 설정 저장 실패: {exc}")

    # ======================================================================
    # Profile save / list / load / delete
    # ======================================================================

    def build_profile(self, name):
        steps = self.read_steps()
        limits = self.read_limits(control_modes.groups_for_mode(self.mode))
        probe, manual, cooling = self.read_probe_settings()
        return {
            "name": name, "mode": self.mode,
            "steps": [{"value": v, "duration_s": d, "note": n, **options}
                      for (v, d, n), options in zip(steps, self.read_step_options())],
            "limits": limits, "probe": probe, "R_ref": {"manual_ohm": manual},
            "cooling": cooling, "description": self.description.get().strip(),
        }

    def save_profile(self):
        if self.running:
            return False
        if self.current_profile is None or self.current_profile["builtin"]:
            return self.save_profile_as()
        name = self.current_profile["name"]
        if not messagebox.askyesno("덮어쓰기 확인", f"'{name}' 프로파일을 현재 내용으로 덮어쓸까요?",
                                   parent=self.root):
            return False
        return self.write_profile(name, overwrite=True)

    def ask_profile_name(self, initial):
        return simpledialog.askstring("다른 이름으로 저장", "프로파일 이름:",
                                      initialvalue=initial, parent=self.root)

    def save_profile_as(self):
        if self.running:
            return False
        initial = ""
        if self.current_profile is not None and not self.current_profile["builtin"]:
            initial = self.current_profile["name"]
        name = self.ask_profile_name(initial)
        if name is None:
            return False
        try:
            name = profile_store.validate_name(name)
        except ValueError as exc:
            messagebox.showerror("이름 확인", str(exc), parent=self.root)
            return False
        overwrite = False
        if profile_store.profile_path(name, self.profile_dir).exists():
            if not messagebox.askyesno("덮어쓰기 확인", f"'{name}' 프로파일이 이미 있습니다. 덮어쓸까요?",
                                       parent=self.root):
                return False
            overwrite = True
        return self.write_profile(name, overwrite)

    def write_profile(self, name, overwrite):
        try:
            data = self.build_profile(name)
        except ValueError as exc:
            messagebox.showerror("저장할 수 없음", str(exc), parent=self.root)
            return False
        flat = control_modes.runtime_limits(self.mode, data["limits"])
        violations = control_modes.profile_violations(
            self.mode, [step["value"] for step in data["steps"]], flat)
        if violations and not messagebox.askyesno(
                "안전 상한 초과",
                "이 프로파일에는 안전 상한을 넘는 행이 있어 이대로는 실행할 수 없습니다.\n\n"
                + "\n".join(violations) + "\n\n그래도 저장할까요?", parent=self.root):
            return False
        try:
            path = profile_store.save_profile(data, self.profile_dir, overwrite=overwrite)
        except (OSError, ValueError) as exc:
            messagebox.showerror("저장 실패", str(exc), parent=self.root)
            return False
        self.current_profile = {"name": name, "builtin": False}
        self.dirty = False
        self.update_profile_status()
        self.log_message(f"프로파일 저장: {path}")
        return True

    def delete_profile(self, summary=None, parent=None):
        parent = parent or self.root
        target = summary or self.current_profile
        if target is None:
            messagebox.showinfo("삭제", "삭제할 사용자 프로파일을 먼저 불러오거나 목록에서 선택하세요.",
                                parent=parent)
            return False
        if target["builtin"]:
            messagebox.showinfo("삭제할 수 없음",
                                "기본 제공 프로파일은 삭제할 수 없습니다 (voltage_profiles.py에서 관리).",
                                parent=parent)
            return False
        name = target["name"]
        if not messagebox.askyesno("삭제 확인", f"'{name}' 프로파일 파일을 삭제할까요?\n되돌릴 수 없습니다.",
                                   parent=parent):
            return False
        try:
            profile_store.delete_profile(name, self.profile_dir)
        except (OSError, ValueError) as exc:
            messagebox.showerror("삭제 실패", str(exc), parent=parent)
            return False
        if self.current_profile is not None and not self.current_profile["builtin"] \
                and self.current_profile["name"] == name:
            self.current_profile = None
            self.dirty = True
            self.update_profile_status()
        self.log_message(f"프로파일 삭제: {name}")
        return True

    def open_profile_list(self):
        if self.running:
            return
        if self.profile_list is not None and self.profile_list.window.winfo_exists():
            self.profile_list.refresh()
            self.profile_list.window.lift()
            return
        self.profile_list = ProfileListDialog(self)

    def load_profile_summary(self, summary, parent=None):
        parent = parent or self.root
        if self.running:
            messagebox.showerror("측정 중", "측정 중에는 프로파일을 불러올 수 없습니다.", parent=parent)
            return False
        try:
            if summary["builtin"]:
                profile = profile_store.builtin_profile(summary["name"])
            else:
                profile = profile_store.load_profile(summary["path"])
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("불러오기 실패", str(exc), parent=parent)
            return False
        return self.apply_profile(profile, builtin=summary["builtin"], parent=parent)

    def apply_profile(self, profile, builtin=False, parent=None):
        """Replace mode, rows, limits and description with *profile*."""
        if self.running:
            return False
        if self.dirty and not messagebox.askyesno(
                "저장 안 된 변경", "현재 프로파일에 저장하지 않은 변경이 있습니다.\n버리고 불러올까요?",
                parent=parent or self.root):
            return False
        info = control_modes.mode_info(profile["mode"])
        self._suspend_dirty = True
        try:
            self.clear_rows()
            self.mode = profile["mode"]
            self.mode_var.set(self.mode)
            for step in profile["steps"]:
                self.add_row(format_number(step["value"] / info.scale),
                             format_number(step["duration_s"]), step["note"],
                             {k: step[k] for k in ("kind", "rref", "end") if k in step})
            self.set_limits(profile.get("limits") or {})
            if not builtin:   # built-in profiles keep the current probe settings
                self.set_probe_settings(profile)
            self.description.set(profile.get("description", ""))
        finally:
            self._suspend_dirty = False
        self.current_profile = {"name": profile["name"], "builtin": builtin}
        self.dirty = False
        self.apply_mode_view()
        self.log_message(f"프로파일 불러옴: {profile['name']} ({info.label}, {len(profile['steps'])}단계)")
        if builtin:
            self.log_message("기본 제공 프로파일에는 안전 상한 정보가 없어 현재 상한을 그대로 사용합니다.")
        return True

    # ======================================================================
    # Run
    # ======================================================================

    def collect_settings(self):
        values = {key: var.get().strip() for key, var in self.variables.items()}
        path = output_path(values["directory"], values["sample"], values["cycle"])
        if path.exists():
            raise ValueError(f"같은 파일이 이미 있습니다. 샘플/사이클 번호 또는 저장 폴더를 변경하세요.\n{path}")
        settings = {key: values[key] for key in self.metadata_keys}
        settings.update(SAMPLE_ID=values["sample"], WIRE_MODE=int(values["WIRE_MODE"]),
                        RESOURCE_NAME=values["RESOURCE_NAME"],
                        SERIAL_MARKER_PORT=values["SERIAL_MARKER_PORT"],
                        SERIAL_MARKER_BAUDRATE=int(values["SERIAL_MARKER_BAUDRATE"]),
                        use_serial_marker=self.marker.get() and not self.dummy.get())
        number = float(values["SAMPLE_INTERVAL_S"])
        if not math.isfinite(number) or number <= 0:
            raise ValueError("SAMPLE_INTERVAL_S: 0보다 큰 유한한 숫자를 입력하세요.")
        settings["SAMPLE_INTERVAL_S"] = number
        if settings["WIRE_MODE"] not in (2, 4):
            raise ValueError("센싱 방식은 2 또는 4여야 합니다.")
        if not self.dummy.get() and not settings["RESOURCE_NAME"]:
            raise ValueError("VISA resource를 입력하세요.")
        if settings["SERIAL_MARKER_BAUDRATE"] <= 0:
            raise ValueError("Arduino baudrate는 양수여야 합니다.")
        if settings["use_serial_marker"] and not settings["SERIAL_MARKER_PORT"]:
            raise ValueError("Arduino COM 포트를 입력하세요.")

        # Safety limits of the selected mode (protection level 1 + 2 + 3).
        mode = self.mode
        flat = control_modes.runtime_limits(mode, self.read_limits(control_modes.groups_for_mode(mode)))
        control_modes.validate_limits(mode, flat)
        steps = self.read_steps()
        violations = control_modes.profile_violations(mode, [v for v, _, _ in steps], flat)
        if violations:
            raise ValueError("안전 상한을 넘는 행이 있어 시작할 수 없습니다.\n\n" + "\n".join(violations))
        current_limit = flat["I_max"] if mode == "current" else flat["I_lim"]
        voltage_limit = flat["V_lim"] if mode == "current" else recorder.VOLTAGE_LIMIT

        # Probe current / conditional cooling / R_ref steps.
        probe, manual_r_ref, cooling = self.read_probe_settings()
        options = self.read_step_options()
        uses_probe = probe["enabled"] or any(o.get("kind") == "rref" for o in options)
        if uses_probe:
            control_modes.validate_probe(mode, probe, flat, current_limit, voltage_limit)
        problems = control_modes.validate_step_plan(
            [(v, d, o) for (v, d, _), o in zip(steps, options)], probe, manual_r_ref)
        if problems:
            raise ValueError("단계 설정을 확인하세요.\n\n" + "\n".join(problems))
        settings.update(
            CONTROL_MODE=mode,
            CURRENT_LIMIT=current_limit,
            VOLTAGE_LIMIT=voltage_limit,
            SAFETY_LIMITS=flat,
            PROFILE=[(v, d, int(values["cycle"]), n, o) for (v, d, n), o in zip(steps, options)],
            PROBE=probe if uses_probe else None,
            MANUAL_R_REF=manual_r_ref,
            COOLING_TIMEOUT_ABORT=(cooling["timeout_abort_count"]
                                   if cooling["timeout_abort"] else None),
        )
        return settings, path, self.dummy.get()

    def set_running(self, running):
        self.running = running
        for widget in self.editors + [w for _, widgets in self.rows for w in widgets[1:]]:
            widget.state(["disabled"] if running else ["!disabled"])
        for button in self.mode_buttons.values():
            button.configure(state="disabled" if running else "normal")
        self.start_button.state(["disabled"] if running else ["!disabled"])
        self.stop_button.state(["!disabled"] if running else ["disabled"])

    def start(self):
        if self.running:
            return
        try:
            settings, path, dummy = self.collect_settings()
        except (ValueError, OSError) as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.root)
            return
        self.save_last_settings()
        self.measured_r_ref = None
        self.update_r_ref_status()
        self.stop_event.clear()
        self.set_running(True)
        self.status.set(("더미 측정 중" if dummy else "장비 연결 / 측정 중") + f" · {MODES[self.mode].label}")
        threading.Thread(target=self.worker, args=(settings, path, dummy), daemon=False).start()

    def worker(self, settings, path, dummy):
        # Only one worker runs at a time. Restore CLI defaults after every run.
        previous = {key: getattr(recorder, key) for key in settings}
        try:
            for key, value in settings.items():
                setattr(recorder, key, value)
            smu = (recorder.DummySourceMeasureUnit(current_limit=settings["CURRENT_LIMIT"]) if dummy else
                   recorder.Keithley2450(settings["RESOURCE_NAME"], settings["CURRENT_LIMIT"]))
            with redirect_stdout(QueueWriter(self.messages)):
                recorder.run_profile(smu, path, stop_event=self.stop_event, exclusive_output=True,
                                     on_event=self.on_recorder_event)
            result = "중지됨" if self.stop_event.is_set() else "측정 완료"
            detail = f"저장 파일: {path}" if path.exists() else "측정 시작 전 중지되어 저장 파일이 없습니다."
            outcome = ("done", f"{result}\n{detail}")
        except control_modes.SafetyAbort as exc:
            outcome = ("safety", f"안전 종료: {exc.reason}\n"
                                 "출력을 0으로 내리고 OFF 했습니다. CSV의 abort_reason 열에 사유를 기록했습니다.\n"
                                 f"{path}")
        except Exception as exc:
            outcome = ("error", f"측정 실패: {exc}\n생성된 CSV가 있다면 중단 전 데이터가 남아 있습니다.\n{path}")
        finally:
            for key, value in previous.items():
                setattr(recorder, key, value)
        self.messages.put(outcome)

    def on_recorder_event(self, kind, value):
        """Called from the worker thread; the Tk side reads it in poll()."""
        if kind == "R_ref":
            self.messages.put(("rref", value))

    def stop(self):
        self.stop_event.set()
        self.stop_button.state(["disabled"])
        self.status.set("중지 요청 · 진행 중인 통신 종료 후 출력 OFF 처리 중")

    def close(self):
        if self.running:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()

    def poll(self):
        for _ in range(500):
            try:
                kind, text = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "rref":
                self.measured_r_ref = text
                self.update_r_ref_status(text)
                self.status.set(self.status.get().split(" · R_ref")[0] + f" · R_ref {text:.6g} Ω")
                continue
            self.log.configure(state="normal")
            self.log.insert("end", text if kind == "log" else f"\n{text}\n")
            if int(self.log.index("end-1c").split(".")[0]) > 2000:
                self.log.delete("1.0", "500.0")
            self.log.see("end")
            self.log.configure(state="disabled")
            if kind != "log":
                self.set_running(False)
                status = text.splitlines()[0]
                if self.measured_r_ref is not None:
                    status += f" · R_ref {self.measured_r_ref:.6g} Ω (측정)"
                self.status.set(status)
                if kind in ("error", "safety"):
                    messagebox.showerror("안전 종료" if kind == "safety" else "측정 오류", text,
                                         parent=self.root)
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    ExperimentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

"""Real-time graph panel for main_gui (display only).

The worker thread hands every CSV row it has just written to the GUI
(``run_profile(on_event=...)`` -> ("sample", row)).  The Tk thread stores the
values here and redraws at most every ``REDRAW_MS``.  Nothing in this module
talks to the instrument, and the measurement never waits for the graph.

Four graphs share the time axis: power (W), resistance (ohm), voltage (V),
current (mA).  Heating / cooling / R_ref phases are shaded.  Dashed lines:
P_max, V_max (or V_lim), I_lim (or I_max) of the run's safety limits, and
R_ref on the resistance graph.
"""

from __future__ import annotations

import bisect
import math
import tkinter as tk
from tkinter import ttk

import control_modes

try:   # matplotlib is optional: without it the panel shows a notice only.
    import matplotlib
    from matplotlib import font_manager
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.collections import PolyCollection
    from matplotlib.figure import Figure
    MATPLOTLIB_ERROR = None
except Exception as exc:   # pragma: no cover - depends on the installation
    MATPLOTLIB_ERROR = exc

KOREAN_FONTS = ("Malgun Gothic", "NanumGothic", "AppleGothic", "Noto Sans CJK KR")


def _korean_font() -> str | None:
    """First installed font that has Hangul glyphs (None: matplotlib default)."""
    installed = {font.name for font in font_manager.fontManager.ttflist}
    return next((name for name in KOREAN_FONTS if name in installed), None)

REDRAW_MS = 500
MAX_POINTS = 2000                 # points per line after decimation
WINDOWS = {"최근 1분": 60.0, "최근 2분": 120.0, "최근 5분": 300.0, "전체": None}
DEFAULT_WINDOW = "최근 2분"

PHASE_STYLE = {                   # phase -> (shade colour, label)
    "heating": ("#f6d5cf", "가열"),
    "cooling": ("#d3e4f5", "냉각"),
    "rref": ("#e3d9f2", "R_ref 측정"),
}
PHASE_LABEL = {"idle": "대기", **{k: v[1] for k, v in PHASE_STYLE.items()}}

# key, title, unit, colour, scale (SI -> shown), include 0 in the y range
CHANNELS = (
    ("P", "전력", "W", "#c0392b", 1.0, True),
    ("R", "저항", "Ω", "#1f6fb2", 1.0, False),
    ("V", "전압", "V", "#2e8b57", 1.0, True),
    ("I", "전류", "mA", "#8e44ad", 1e3, True),
)


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def sample_from_row(row: dict) -> dict:
    """Plot values (SI) of one CSV row.

    R is shown only while a current is sourced (heating or probe current) and
    |I| >= 0.1 mA, i.e. where it is meaningful; otherwise it is a gap.
    """
    current = _number(row.get("current_A"))
    resistance = _number(row.get("resistance_ohm"))
    phase = str(row.get("phase") or "")
    sourcing = phase == "heating" or str(row.get("probe_active")) == "1"
    if not (sourcing and math.isfinite(current)
            and abs(current) >= control_modes.R_VALID_MIN_CURRENT_A
            and math.isfinite(resistance) and resistance > 0):
        resistance = math.nan
    r_ref = _number(row.get("R_ref_ohm"))
    return {
        "t": _number(row.get("time_s")),
        "phase": phase,
        "P": abs(_number(row.get("power_W"))),
        "R": resistance,
        "V": _number(row.get("measured_voltage_V")),
        "I": current,
        "R_ref": r_ref if math.isfinite(r_ref) and r_ref > 0 else None,
        "step": row.get("step_index", ""),
        "note": row.get("step_note", ""),
    }


def limit_lines(mode: str, flat: dict | None) -> dict:
    """Safety limits drawn as dashed lines, in SI units: {channel: (value, label)}."""
    flat = flat or {}
    lines = {}
    if "P_max" in flat:
        lines["P"] = (float(flat["P_max"]), "P_max")
    voltage_key = "V_lim" if mode == "current" else "V_max"
    current_key = "I_max" if mode == "current" else "I_lim"
    if voltage_key in flat:
        lines["V"] = (float(flat[voltage_key]), voltage_key)
    if current_key in flat:
        lines["I"] = (float(flat[current_key]), current_key)
    return lines


def _format(value: float, unit: str) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.4g} {unit}"


class LivePlot:
    """Right-hand graph panel.  All methods run in the Tk thread."""

    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=(6, 10, 12, 10))
        self.times: list[float] = []
        self.data = {key: [] for key, *_ in CHANNELS}
        self.spans: list[list] = []          # [phase, t_start, t_end]
        self.limits: dict = {}
        self.r_ref: float | None = None
        self.last: dict | None = None
        self.dirty = False
        self.paused = False
        self.after_id = None
        self.redraw_count = 0

        bar = ttk.Frame(self.frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="실시간 그래프", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.pause_button = ttk.Button(bar, text="일시정지", width=9, command=self.toggle_pause)
        self.pause_button.pack(side="right")
        self.window = tk.StringVar(value=DEFAULT_WINDOW)
        window = ttk.Combobox(bar, textvariable=self.window, values=list(WINDOWS),
                              state="readonly", width=9)
        window.pack(side="right", padx=6)
        window.bind("<<ComboboxSelected>>", lambda _e: self.request_redraw())
        ttk.Label(bar, text="표시 구간").pack(side="right")
        self.state_text = tk.StringVar(value="대기 중 · 시작하면 그래프를 새로 그립니다.")
        ttk.Label(self.frame, textvariable=self.state_text, foreground="#444").pack(
            fill="x", pady=(4, 2))

        values = ttk.Frame(self.frame)
        values.pack(fill="x", pady=(0, 4))
        self.value_vars = {}
        big = ("TkDefaultFont", 15, "bold")
        for column, (key, title, unit, colour, _scale, _zero) in enumerate(CHANNELS):
            values.columnconfigure(column, weight=1, uniform="v")
            cell = ttk.Frame(values)
            cell.grid(row=0, column=column, sticky="ew", padx=4)
            ttk.Label(cell, text=title, foreground="#555").pack(anchor="w")
            variable = tk.StringVar(value="—")
            tk.Label(cell, textvariable=variable, font=big, fg=colour, anchor="w").pack(
                anchor="w")
            self.value_vars[key] = variable

        self.figure = None
        if MATPLOTLIB_ERROR is not None:
            ttk.Label(self.frame, foreground="#a00",
                      text=f"그래프를 표시할 수 없습니다 (matplotlib: {MATPLOTLIB_ERROR}).\n"
                           "측정과 CSV 저장은 영향을 받지 않습니다.").pack(anchor="w", pady=20)
            return
        self._build_figure()
        self.after_id = self.frame.after(REDRAW_MS, self._tick)

    # ------------------------------------------------------------------
    # Figure
    # ------------------------------------------------------------------

    def _build_figure(self):
        # Text objects keep the font chosen at creation: build inside the rc
        # context so axis labels show Hangul (global rcParams stay untouched).
        rc = {"axes.unicode_minus": False}
        font = _korean_font()
        if font is not None:
            rc["font.family"] = [font, "DejaVu Sans"]
        with matplotlib.rc_context(rc):
            self._build_figure_artists()

    def _build_figure_artists(self):
        self.figure = Figure(figsize=(6.0, 6.0), dpi=96)
        self.axes = {}
        self.lines = {}
        self.limit_artists = {}
        self.shades = {}
        first = None
        for index, (key, title, unit, colour, _scale, _zero) in enumerate(CHANNELS):
            ax = self.figure.add_subplot(len(CHANNELS), 1, index + 1, sharex=first)
            if first is None:
                first = ax
            ax.set_ylabel(f"{title} ({unit})", fontsize=9)
            ax.tick_params(labelsize=8)
            ax.grid(True, color="#e4e4e4", linewidth=0.6)
            if index < len(CHANNELS) - 1:
                ax.tick_params(labelbottom=False)
            (line,) = ax.plot([], [], color=colour, linewidth=1.2)
            self.lines[key] = line
            limit = ax.axhline(0.0, color="#d35400" if key != "R" else "#27ae60",
                               linestyle="--", linewidth=1.0)
            limit.set_visible(False)
            label = ax.text(1.0, 0.0, "", transform=limit.get_transform(), ha="right",
                            va="bottom", fontsize=8, color=limit.get_color())
            self.limit_artists[key] = (limit, label)
            self.shades[key] = {}
            for phase, (shade, _) in PHASE_STYLE.items():
                collection = PolyCollection([], facecolors=shade, edgecolors="none",
                                            transform=ax.get_xaxis_transform(), zorder=0)
                ax.add_collection(collection, autolim=False)
                self.shades[key][phase] = collection
            self.axes[key] = ax
        first.set_xlim(0, WINDOWS[DEFAULT_WINDOW])
        self.axes["I"].set_xlabel("시간 (s)", fontsize=9)
        self.figure.subplots_adjust(left=0.13, right=0.97, top=0.98, bottom=0.08, hspace=0.12)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.frame)
        widget = self.canvas.get_tk_widget()
        widget.configure(width=640, height=360)   # requested size; grows with the pane
        widget.pack(fill="both", expand=True)
        legend = ttk.Frame(self.frame)
        legend.pack(fill="x")
        for phase, (shade, text) in PHASE_STYLE.items():
            tk.Label(legend, text="   ", bg=shade).pack(side="left", padx=(8, 3))
            ttk.Label(legend, text=text).pack(side="left")
        ttk.Label(legend, text="- - 안전 상한 / R_ref", foreground="#d35400").pack(
            side="left", padx=12)
        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def reset(self, mode: str, flat: dict | None, r_ref: float | None = None):
        """New run: clear the data and set the limit / R_ref lines."""
        self.times.clear()
        for values in self.data.values():
            values.clear()
        self.spans.clear()
        self.last = None
        self.limits = limit_lines(mode, flat)
        self.r_ref = r_ref
        for variable in self.value_vars.values():
            variable.set("—")
        self.state_text.set("측정 시작 · 데이터를 기다리는 중")
        self.request_redraw()

    def set_r_ref(self, value: float | None):
        self.r_ref = value
        self.request_redraw()

    def add_row(self, row: dict):
        sample = sample_from_row(row)
        t = sample["t"]
        if not math.isfinite(t) or (self.times and t < self.times[-1]):
            return
        self.times.append(t)
        for key in self.data:
            self.data[key].append(sample[key])
        if sample["R_ref"] is not None:
            self.r_ref = sample["R_ref"]
        phase = sample["phase"]
        if self.spans and self.spans[-1][0] == phase:
            self.spans[-1][2] = t
        else:
            if self.spans:
                self.spans[-1][2] = t      # close the previous span at this sample
            self.spans.append([phase, t, t])
        self.last = sample
        self.dirty = True

    def request_redraw(self):
        self.dirty = True

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_button.configure(text="재개" if self.paused else "일시정지")
        if not self.paused:
            self.request_redraw()
        self._update_state_text()

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _tick(self):
        self.after_id = None
        try:
            if self.dirty and not self.paused:
                self.redraw()
        except Exception as exc:   # the graph must never take the GUI down
            self.state_text.set(f"그래프 갱신 오류 (측정은 계속됨): {exc}")
        finally:
            try:
                self.after_id = self.frame.after(REDRAW_MS, self._tick)
            except tk.TclError:
                pass   # window closed

    def visible_range(self):
        """(first index, x_left, x_right) of the selected display window."""
        window = WINDOWS.get(self.window.get())
        if not self.times:
            return 0, 0.0, window or 10.0
        end = self.times[-1]
        if window is None:
            start = self.times[0]
            return 0, start, max(end, start + 1.0)
        left = max(end - window, 0.0)
        first = bisect.bisect_left(self.times, left)
        return first, left, max(end, left + window)

    def redraw(self):
        self.dirty = False
        self._update_values()
        self._update_state_text()
        if self.figure is None:
            return
        first, left, right = self.visible_range()
        times = self.times[first:]
        stride = max(1, math.ceil(len(times) / MAX_POINTS))
        shown_t = times[::stride]
        for key, _title, _unit, _colour, scale, include_zero in CHANNELS:
            values = self.data[key][first:]
            self.lines[key].set_data(shown_t, [v * scale for v in values[::stride]])
            reference = self._reference(key)
            limit, label = self.limit_artists[key]
            if reference is None:
                limit.set_visible(False)
                label.set_text("")
            else:
                value, text = reference
                limit.set_ydata([value * scale, value * scale])
                limit.set_visible(True)
                label.set_position((1.0, value * scale))
                label.set_text(f"{text} {value * scale:.4g} ")
            self.axes[key].set_ylim(self._y_range(values, scale, include_zero, reference))
            for phase, collection in self.shades[key].items():
                collection.set_verts([
                    [(a, 0), (a, 1), (b, 1), (b, 0)]
                    for p, a, b in self.spans if p == phase and b >= left and a <= right])
        self.axes["P"].set_xlim(left, right)
        self.canvas.draw_idle()
        self.redraw_count += 1

    def _reference(self, key):
        if key == "R":
            return None if self.r_ref is None else (self.r_ref, "R_ref")
        return self.limits.get(key)

    @staticmethod
    def _y_range(values, scale, include_zero, reference):
        finite = [v * scale for v in values if math.isfinite(v)]
        if reference is not None:
            finite.append(reference[0] * scale)
        if include_zero:
            finite.append(0.0)
        if not finite:
            return (0.0, 1.0)
        low, high = min(finite), max(finite)
        span = high - low
        margin = span * 0.08 if span > 0 else max(abs(high) * 0.05, 1e-6)
        return (low - (0 if include_zero and low >= 0 else margin), high + margin)

    def _update_values(self):
        sample = self.last
        if sample is None:
            return
        for key, _title, unit, _colour, scale, _zero in CHANNELS:
            value = sample[key]
            self.value_vars[key].set(_format(value * scale if math.isfinite(value) else value,
                                             unit))

    def _update_state_text(self):
        if self.paused:
            self.state_text.set("일시정지됨 · 데이터는 계속 받고 있습니다 (재개하면 따라잡음)")
            return
        sample = self.last
        if sample is None:
            return
        phase = PHASE_LABEL.get(sample["phase"], sample["phase"])
        note = f" · {sample['note']}" if sample["note"] else ""
        self.state_text.set(f"t = {sample['t']:.1f} s · {phase} · 단계 {sample['step']}{note}")

    def close(self):
        if self.after_id is not None:
            try:
                self.frame.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

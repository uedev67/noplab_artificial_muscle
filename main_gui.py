"""Desktop controls for the existing manual voltage profile recorder."""

from __future__ import annotations

import math
import queue
import re
import threading
import tkinter as tk
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import manual_voltage_profile as recorder


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


class QueueWriter:
    def __init__(self, messages):
        self.messages = messages

    def write(self, text):
        if text:
            self.messages.put(("log", text))
        return len(text)

    def flush(self):
        pass


class ExperimentGUI:
    def __init__(self, root):
        self.root = root
        root.title("Keithley 2450 · 전압 프로파일 측정")
        root.geometry("980x830")
        root.minsize(780, 650)
        self.messages = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.closing = False
        self.variables = {}
        self.rows = []
        self.editors = []

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=12, pady=10)
        basic = ttk.Frame(notebook, padding=12)
        advanced = ttk.Frame(notebook, padding=12)
        notebook.add(basic, text="측정 / 전압 프로파일")
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

        profile = ttk.LabelFrame(basic, text="전압 프로파일 · 위에서 아래로 한 번 실행", padding=6)
        profile.grid(row=4, column=0, columnspan=3, sticky="nsew")
        basic.rowconfigure(4, weight=1)
        canvas = tk.Canvas(profile, height=220, highlightthickness=0)
        scrollbar = ttk.Scrollbar(profile, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.row_frame = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.row_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.row_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        for col, label in enumerate(("단계", "전압 (V)", "시간 (초)", "메모", "")):
            ttk.Label(self.row_frame, text=label).grid(row=0, column=col, padx=6)
        self.row_frame.columnconfigure(3, weight=1)
        for entry in recorder.PROFILE:
            voltage, duration, _, note = recorder.parse_profile_entry(entry)
            self.add_row(voltage, duration, note)
        add = ttk.Button(basic, text="+ 행 추가", command=self.add_row)
        add.grid(row=5, column=0, sticky="w", pady=6)
        self.editors.append(add)

        advanced.columnconfigure(1, weight=1)
        settings = [
            ("센싱 방식", "WIRE_MODE", recorder.WIRE_MODE),
            ("Current limit (A)", "CURRENT_LIMIT", recorder.CURRENT_LIMIT),
            ("샘플링 간격 (초)", "SAMPLE_INTERVAL_S", recorder.SAMPLE_INTERVAL_S),
            ("VISA resource", "RESOURCE_NAME", recorder.RESOURCE_NAME),
            ("Arduino COM 포트", "SERIAL_MARKER_PORT", recorder.SERIAL_MARKER_PORT),
            ("Arduino baudrate", "SERIAL_MARKER_BAUDRATE", recorder.SERIAL_MARKER_BAUDRATE),
        ]
        for row, (label, key, value) in enumerate(settings):
            self.field(advanced, row, label, key, value, choices=(2, 4) if key == "WIRE_MODE" else None)
        self.marker = tk.BooleanVar(value=recorder.use_serial_marker)
        self.dummy = tk.BooleanVar(value=False)
        for row, label, variable in ((6, "Arduino 시작 마커 사용", self.marker), (7, "더미 모드 (장비 연결 없이 테스트)", self.dummy)):
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
        for row, (label, key) in enumerate(metadata, 8):
            self.field(advanced, row, label, key, getattr(recorder, key))

        controls = ttk.Frame(root, padding=(12, 0))
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="시작 (키슬리 실행)", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="중지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.status = tk.StringVar(value="대기 중 · 시작 버튼을 누르면 장비에 연결합니다.")
        ttk.Label(controls, textvariable=self.status).pack(side="left")
        self.log = ScrolledText(root, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", padx=12, pady=10)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

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

    def add_row(self, voltage=0.0, duration=1.0, note=""):
        variables = [tk.StringVar(value=str(value)) for value in (voltage, duration, note)]
        widgets = [ttk.Label(self.row_frame)]
        widgets.extend(ttk.Entry(self.row_frame, textvariable=v, width=14) for v in variables)
        item = (variables, widgets)
        widgets.append(ttk.Button(self.row_frame, text="삭제", command=lambda: self.remove_row(item)))
        self.rows.append(item)
        self.layout_rows()

    def layout_rows(self):
        for index, (_, widgets) in enumerate(self.rows, 1):
            widgets[0].configure(text=str(index))
            for col, widget in enumerate(widgets):
                widget.grid(row=index, column=col, sticky="ew", padx=4, pady=3)

    def remove_row(self, item):
        self.rows.remove(item)
        for widget in item[1]:
            widget.destroy()
        self.layout_rows()

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
        for key in ("CURRENT_LIMIT", "SAMPLE_INTERVAL_S"):
            number = float(values[key])
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{key}: 0보다 큰 유한한 숫자를 입력하세요.")
            settings[key] = number
        if settings["WIRE_MODE"] not in (2, 4):
            raise ValueError("센싱 방식은 2 또는 4여야 합니다.")
        if not self.dummy.get() and not settings["RESOURCE_NAME"]:
            raise ValueError("VISA resource를 입력하세요.")
        if settings["SERIAL_MARKER_BAUDRATE"] <= 0:
            raise ValueError("Arduino baudrate는 양수여야 합니다.")
        if settings["use_serial_marker"] and not settings["SERIAL_MARKER_PORT"]:
            raise ValueError("Arduino COM 포트를 입력하세요.")
        profile = []
        for index, (variables, _) in enumerate(self.rows, 1):
            voltage, duration = (float(v.get()) for v in variables[:2])
            if not math.isfinite(voltage) or not math.isfinite(duration) or duration <= 0:
                raise ValueError(f"{index}행: 전압은 유한한 숫자, 시간은 0보다 큰 숫자여야 합니다.")
            profile.append((voltage, duration, int(values["cycle"]), variables[2].get()))
        if not profile:
            raise ValueError("전압 프로파일을 한 행 이상 입력하세요.")
        settings["PROFILE"] = profile
        return settings, path, self.dummy.get()

    def set_running(self, running):
        self.running = running
        for widget in self.editors + [w for _, widgets in self.rows for w in widgets[1:]]:
            widget.state(["disabled"] if running else ["!disabled"])
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
        self.stop_event.clear()
        self.set_running(True)
        self.status.set("더미 측정 중" if dummy else "장비 연결 / 측정 중")
        threading.Thread(target=self.worker, args=(settings, path, dummy), daemon=False).start()

    def worker(self, settings, path, dummy):
        # Only one worker runs at a time. Restore CLI defaults after every run.
        previous = {key: getattr(recorder, key) for key in settings}
        try:
            for key, value in settings.items():
                setattr(recorder, key, value)
            smu = (recorder.DummySourceMeasureUnit() if dummy else
                   recorder.Keithley2450(settings["RESOURCE_NAME"], settings["CURRENT_LIMIT"]))
            with redirect_stdout(QueueWriter(self.messages)):
                recorder.run_profile(smu, path, stop_event=self.stop_event, exclusive_output=True)
            result = "중지됨" if self.stop_event.is_set() else "측정 완료"
            detail = f"저장 파일: {path}" if path.exists() else "측정 시작 전 중지되어 저장 파일이 없습니다."
            outcome = ("done", f"{result}\n{detail}")
        except Exception as exc:
            outcome = ("error", f"측정 실패: {exc}\n생성된 CSV가 있다면 중단 전 데이터가 남아 있습니다.\n{path}")
        finally:
            for key, value in previous.items():
                setattr(recorder, key, value)
        self.messages.put(outcome)

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
            self.log.configure(state="normal")
            self.log.insert("end", text if kind == "log" else f"\n{text}\n")
            if int(self.log.index("end-1c").split(".")[0]) > 2000:
                self.log.delete("1.0", "500.0")
            self.log.see("end")
            self.log.configure(state="disabled")
            if kind != "log":
                self.set_running(False)
                self.status.set(text.splitlines()[0])
                if kind == "error":
                    messagebox.showerror("측정 오류", text, parent=self.root)
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

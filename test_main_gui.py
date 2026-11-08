"""Hardware-free checks: python -m unittest test_main_gui -v."""

import csv
import io
import json
import math
import tempfile
import threading
import time
import tkinter as tk
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import control_modes
import keithley_driver as driver
import main_gui
import manual_voltage_profile as recorder
import profile_store


class GUIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.profile_dir = Path(self.temp.name) / "profiles"
        self.settings_path = Path(self.temp.name) / "settings" / "last_settings.json"
        self.gui = main_gui.ExperimentGUI(self.root, profile_dir=self.profile_dir,
                                          settings_path=self.settings_path)
        for item in list(self.gui.rows):
            self.gui.remove_row(item)
        self.gui.add_row(2, 0.03, "test")
        self.gui.add_row(0, 0.03)
        for key, value in {"sample": "22", "cycle": "5", "directory": self.temp.name,
                           "SAMPLE_INTERVAL_S": "0.01"}.items():
            self.gui.variables[key].set(value)
        self.gui.dummy.set(True)

    def tearDown(self):
        for after_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", after_id)
        self.root.destroy()
        self.temp.cleanup()

    def test_worker_csv_and_settings_restoration(self):
        settings, path, dummy = self.gui.collect_settings()
        original = recorder.PROFILE
        self.assertEqual(path.name, f"{datetime.now():%m%d}_샘플22_5_저항.csv")
        self.assertFalse(settings["use_serial_marker"])
        self.gui.worker(settings, path, dummy)
        self.assertIs(recorder.PROFILE, original)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertEqual(list(rows[0]), recorder.FIELDNAMES)
        self.assertEqual({r["control_mode"] for r in rows}, {"voltage"})
        self.assertEqual(float(rows[0]["current_limit_A"]), 0.16)  # GUI I_lim default
        self.assertIs(recorder.SAFETY_LIMITS, None)  # CLI default restored
        self.assertEqual(recorder.CONTROL_MODE, "voltage")
        self.assertEqual({r["sample_id"] for r in rows}, {"22"})
        self.assertEqual({r["cycle_index"] for r in rows}, {"5"})
        self.assertEqual({r["step_index"] for r in rows}, {"1", "2"})
        self.assertTrue(any(kind == "done" for kind, _ in self.gui.messages.queue))
        with self.assertRaises(ValueError):
            self.gui.collect_settings()

    def test_validation(self):
        i_lim = self.gui.limit_vars["voltage"]["I_lim"]
        for bad in ("nan", "inf", "0", "-1", "abc", "1.2"):
            i_lim.set(bad)
            with self.assertRaises(ValueError):
                self.gui.collect_settings()
        i_lim.set("0.35")
        self.gui.collect_settings()
        self.gui.rows[0][0][1].set("0")
        with self.assertRaises(ValueError):
            self.gui.collect_settings()

    def test_gui_start_stays_responsive_and_finishes(self):
        self.gui.start()
        self.assertTrue(self.gui.running)
        self.assertIn("disabled", self.gui.start_button.state())
        deadline = time.monotonic() + 5
        while self.gui.running and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.gui.running)
        self.assertEqual(self.gui.status.get(), "측정 완료")

    def test_stop_and_error_switch_output_off(self):
        for fail in (False, True):
            smu = recorder.DummySourceMeasureUnit()
            event = threading.Event()
            measure = smu.measure

            def measurement():
                if fail:
                    raise RuntimeError("simulated communication failure")
                result = measure()
                event.set()
                return result

            smu.measure = measurement
            path = Path(self.temp.name) / f"stop_{fail}.csv"
            with patch.multiple(recorder, PROFILE=[(2, 10)], use_serial_marker=False,
                                SAMPLE_INTERVAL_S=5):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "simulated"):
                        recorder.run_profile(smu, path, event, exclusive_output=True)
                else:
                    start = time.monotonic()
                    recorder.run_profile(smu, path, event, exclusive_output=True)
                    self.assertLess(time.monotonic() - start, 1)
            self.assertFalse(smu._output_enabled)
            self.assertFalse(smu._connected)
            self.assertEqual(smu._voltage, 0)
            with path.open(encoding="utf-8-sig") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 0 if fail else 1)

    # ------------------------------------------------------------------
    # Control modes
    # ------------------------------------------------------------------

    def visible_limit_groups(self):
        return {group for group, frame in self.gui.limit_frames.items()
                if frame.winfo_manager() == "grid"}

    def test_mode_switch_changes_header_unit_and_limit_panel(self):
        self.assertEqual(self.gui.value_header.cget("text"), "전압 (V)")
        self.assertEqual(self.visible_limit_groups(), {"voltage", "common"})
        with patch.object(self.gui, "ask_mode_change", return_value=None) as ask:
            self.assertFalse(self.gui.set_mode("power"))
        ask.assert_called_once_with("voltage", "power")
        self.assertEqual(self.gui.mode, "voltage")
        with patch.object(self.gui, "ask_mode_change", return_value="keep"):
            self.assertTrue(self.gui.set_mode("power"))
        self.assertEqual(self.gui.value_header.cget("text"), "전력 (W)")
        self.assertEqual(self.visible_limit_groups(), {"power", "common", "power_control"})
        self.assertEqual(len(self.gui.rows), 2)
        self.assertIn("수정됨", self.gui.profile_status.get())
        with patch.object(self.gui, "ask_mode_change", return_value="clear"):
            self.assertTrue(self.gui.set_mode("current"))
        self.assertEqual(self.gui.value_header.cget("text"), "전류 (mA)")
        self.assertEqual(self.visible_limit_groups(), {"current", "common"})
        self.assertEqual(self.gui.rows, [])
        self.assertEqual(self.gui.mode_var.get(), "current")
        # No rows -> no question.
        with patch.object(self.gui, "ask_mode_change") as ask:
            self.assertTrue(self.gui.set_mode("voltage"))
        ask.assert_not_called()

    def test_running_locks_mode_buttons(self):
        self.gui.set_running(True)
        try:
            for button in self.gui.mode_buttons.values():
                self.assertEqual(str(button.cget("state")), "disabled")
            self.assertFalse(self.gui.set_mode("power", ask=False))
            self.assertEqual(self.gui.mode, "voltage")
            for entry in (w for group in self.gui.limit_frames.values()
                          for w in group.winfo_children() if isinstance(w, tk.ttk.Entry)):
                self.assertIn("disabled", entry.state())
        finally:
            self.gui.set_running(False)
        self.assertEqual(str(self.gui.mode_buttons["power"].cget("state")), "normal")

    def test_start_blocked_when_profile_exceeds_limits(self):
        self.gui.add_row(13, 0.03)          # row 3 > V_max 12 V
        self.gui.add_row(12, 0.03)          # equal is allowed
        with self.assertRaisesRegex(ValueError, "3행"):
            self.gui.collect_settings()
        with patch("main_gui.messagebox.showerror") as error, \
                patch("main_gui.threading.Thread") as thread:
            self.gui.start()
        self.assertIn("3행", error.call_args[0][1])
        self.assertNotIn("4행", error.call_args[0][1])
        thread.assert_not_called()
        self.assertFalse(self.gui.running)
        # Current mode: GUI mA vs I_max (mA)
        with patch.object(self.gui, "ask_mode_change", return_value="clear"):
            self.gui.set_mode("current")
        self.gui.add_row(160, 0.03)
        self.gui.add_row(161, 0.03)
        with self.assertRaisesRegex(ValueError, "2행: 설정 전류 161 mA > I_max 160 mA"):
            self.gui.collect_settings()

    def test_current_mode_run_from_gui(self):
        with patch.object(self.gui, "ask_mode_change", return_value="clear"):
            self.gui.set_mode("current")
        self.gui.add_row(20, 0.05)
        settings, path, dummy = self.gui.collect_settings()
        self.assertEqual(settings["CONTROL_MODE"], "current")
        self.assertEqual(settings["PROFILE"][0][0], 0.02)
        self.assertEqual(settings["VOLTAGE_LIMIT"], 12.0)
        self.gui.worker(settings, path, dummy)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({r["control_mode"] for r in rows}, {"current"})
        self.assertEqual({r["set_voltage_V"] for r in rows}, {""})
        self.assertEqual({float(r["set_current_A"]) for r in rows}, {0.02})

    def test_gui_safety_abort_reports_and_records_reason(self):
        self.gui.limit_vars["voltage"]["P_max"].set("0.01")   # 2 V / 100 ohm = 0.04 W
        self.gui.rows[0][0][1].set("5")
        settings, path, dummy = self.gui.collect_settings()
        self.gui.worker(settings, path, dummy)
        kind, text = self.gui.messages.queue[-1]
        self.assertEqual(kind, "safety")
        self.assertIn("측정 전력", text)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertIn("P_max", rows[0]["abort_reason"])
        self.assertIs(recorder.SAFETY_LIMITS, None)
        with patch("main_gui.messagebox.showerror") as error:
            self.gui.running = True
            self.gui.poll()
        self.assertEqual(error.call_args[0][0], "안전 종료")
        self.assertFalse(self.gui.running)

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def test_profile_save_list_load_round_trip(self):
        with patch.object(self.gui, "ask_mode_change", return_value="clear"):
            self.gui.set_mode("current")
        self.gui.add_row(20, 10, "가열")
        self.gui.add_row(0, 30, "냉각")
        self.gui.limit_vars["current"]["I_max"].set("100")
        self.gui.limit_vars["common"]["R_jump_pct"].set("7")
        self.gui.description.set("왕복 테스트 설명")
        with patch.object(self.gui, "ask_profile_name", return_value="왕복/테스트"), \
                patch("main_gui.messagebox.showerror") as error:
            self.assertFalse(self.gui.save_profile_as())   # invalid file name
        self.assertIn("'/'", error.call_args[0][1])
        with patch.object(self.gui, "ask_profile_name", return_value="왕복 테스트"):
            self.assertTrue(self.gui.save_profile_as())
        self.assertFalse(self.gui.dirty)
        self.assertIn("저장됨", self.gui.profile_status.get())
        saved = json.loads((self.profile_dir / "왕복 테스트.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "current")
        self.assertEqual(saved["steps"][0], {"value": 0.02, "duration_s": 10.0, "note": "가열"})
        self.assertEqual(saved["limits"]["current"]["I_max"], 0.1)
        self.assertNotIn("voltage", saved["limits"])

        # [저장] overwrites the current profile after confirmation.
        self.gui.rows[1][0][1].set("40")
        self.assertTrue(self.gui.dirty)
        with patch("main_gui.messagebox.askyesno", return_value=False):
            self.assertFalse(self.gui.save_profile())
        with patch("main_gui.messagebox.askyesno", return_value=True):
            self.assertTrue(self.gui.save_profile())

        # Change everything, then load it back from the list.
        with patch.object(self.gui, "ask_mode_change", return_value="clear"):
            self.gui.set_mode("voltage")
        self.gui.add_row(5, 1)
        self.gui.limit_vars["current"]["I_max"].set("150")
        self.gui.limit_vars["common"]["R_jump_pct"].set("5")
        summaries, _ = profile_store.list_profiles(self.profile_dir)
        summary = next(s for s in summaries if s["name"] == "왕복 테스트")
        self.assertEqual((summary["mode"], summary["steps"], summary["total_s"]),
                         ("current", 2, 50.0))
        with patch("main_gui.messagebox.askyesno", return_value=False):
            self.assertFalse(self.gui.load_profile_summary(summary))   # keep unsaved edits
        self.assertEqual(self.gui.mode, "voltage")
        with patch("main_gui.messagebox.askyesno", return_value=True):
            self.assertTrue(self.gui.load_profile_summary(summary))
        self.assertEqual(self.gui.mode, "current")
        self.assertEqual(self.gui.mode_var.get(), "current")
        self.assertEqual(self.gui.value_header.cget("text"), "전류 (mA)")
        self.assertEqual([[v.get() for v in variables] for variables, _ in self.gui.rows],
                         [["20", "10", "가열"], ["0", "40", "냉각"]])
        self.assertEqual(self.gui.limit_vars["current"]["I_max"].get(), "100")
        self.assertEqual(self.gui.limit_vars["common"]["R_jump_pct"].get(), "7")
        self.assertEqual(self.gui.description.get(), "왕복 테스트 설명")
        self.assertFalse(self.gui.dirty)
        self.assertIn("왕복 테스트", self.gui.profile_status.get())

        # Delete with confirmation.
        with patch("main_gui.messagebox.askyesno", return_value=True):
            self.assertTrue(self.gui.delete_profile())
        self.assertFalse((self.profile_dir / "왕복 테스트.json").exists())
        self.assertIsNone(self.gui.current_profile)

    def test_builtin_profile_is_read_only(self):
        summaries, _ = profile_store.list_profiles(self.profile_dir)
        exp4 = next(s for s in summaries if s["builtin"] and s["name"] == "exp4")
        with patch("main_gui.messagebox.askyesno", return_value=True):
            self.assertTrue(self.gui.load_profile_summary(exp4))
        self.assertEqual(self.gui.mode, "voltage")
        self.assertEqual(len(self.gui.rows), len(recorder.PROFILES["exp4"]))
        self.assertIn("[기본] exp4", self.gui.profile_status.get())
        with patch("main_gui.messagebox.showinfo") as info:
            self.assertFalse(self.gui.delete_profile())
        info.assert_called_once()
        # [저장] on a built-in becomes [다른 이름으로 저장].
        with patch.object(self.gui, "ask_profile_name", return_value=None) as ask:
            self.assertFalse(self.gui.save_profile())
        ask.assert_called_once()

    def test_profile_list_dialog_filter_and_search(self):
        for name, mode in (("가열 전력", "power"), ("전류 시험", "current")):
            profile_store.save_profile(
                {"name": name, "mode": mode, "steps": [{"value": 0.1, "duration_s": 1}]},
                self.profile_dir)
        self.gui.open_profile_list()
        dialog = self.gui.profile_list
        names = lambda: [dialog.items[i]["name"] for i in dialog.tree.get_children()]
        self.assertIn("exp4", names())
        dialog.filter.set("전력")
        self.assertEqual(names(), ["가열 전력"])
        dialog.filter.set("전체")
        dialog.search.set("시험")
        self.assertEqual(names(), ["전류 시험"])
        dialog.tree.selection_set(dialog.tree.get_children()[0])
        with patch("main_gui.messagebox.askyesno", return_value=True):
            dialog.load_selected()
        self.assertEqual(self.gui.mode, "current")
        self.assertEqual(self.gui.rows[0][0][0].get(), "100")   # 0.1 A -> 100 mA
        self.assertFalse(dialog.window.winfo_exists())

    def test_last_used_limits_are_restored(self):
        self.gui.limit_vars["power"]["P_max"].set("0.8")
        self.gui.limit_vars["power_control"]["max_R_failures"].set("5")
        self.gui.save_last_settings()
        other = main_gui.ExperimentGUI(tk.Toplevel(self.root), profile_dir=self.profile_dir,
                                       settings_path=self.settings_path)
        self.assertEqual(other.limit_vars["power"]["P_max"].get(), "0.8")
        self.assertEqual(other.limit_vars["power_control"]["max_R_failures"].get(), "5")
        self.assertEqual(other.mode, "voltage")
        self.assertFalse(other.dirty)


LEGACY_FIELDNAMES = [
    "experiment_id", "sample_id", "operator", "sample_index",
    "timestamp_local", "timestamp_utc", "time_s", "dt_s", "measurement_duration_s",
    "step_index", "cycle_index", "state", "step_note", "step_elapsed_s",
    "step_duration_s", "wire_mode", "set_voltage_V", "measured_voltage_V",
    "voltage_error_V", "current_A", "current_limit_A", "current_limit_ratio",
    "near_current_limit", "resistance_ohm", "conductance_S", "power_W",
    "energy_J", "step_energy_J", "preload_g", "initial_length_mm",
    "ambient_temp_C", "ir_camera_id", "sony_camera_id", "sync_note", "general_note",
]

POWER_LIMITS = {"P_max": 1.5, "V_max": 12.0, "R_jump_pct": 5.0,
                "probe_V": 0.1, "max_step_V": 1.0, "max_R_failures": 3}
VOLTAGE_LIMITS = {"V_max": 12.0, "P_max": 1.5, "R_jump_pct": 5.0}
CURRENT_LIMITS = {"I_max": 0.16, "P_max": 1.5, "R_jump_pct": 5.0}


class RecorderTestBase(unittest.TestCase):
    """Runs run_profile() with the dummy SMU (no hardware)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def run_mode(self, mode, profile, limits, smu=None, name="run.csv", **settings):
        smu = smu or recorder.DummySourceMeasureUnit(current_limit=0.16)
        path = Path(self.temp.name) / name
        settings.setdefault("PROBE", None)
        settings.setdefault("MANUAL_R_REF", None)
        settings.setdefault("COOLING_TIMEOUT_ABORT", None)
        on_event = settings.pop("on_event", None)
        with patch.multiple(recorder, CONTROL_MODE=mode, PROFILE=profile,
                            SAFETY_LIMITS=limits, use_serial_marker=False,
                            SAMPLE_INTERVAL_S=0.02, CURRENT_LIMIT=0.16,
                            VOLTAGE_LIMIT=12.0, **settings), \
                redirect_stdout(io.StringIO()):
            try:
                recorder.run_profile(smu, path, on_event=on_event)
                error = None
            except control_modes.SafetyAbort as exc:
                error = exc
        rows = []
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        return smu, rows, error


class ControlModeRecorderTests(RecorderTestBase):
    """run_profile() in each control mode with the dummy SMU (no hardware)."""

    def test_legacy_columns_keep_order_and_new_columns_are_appended(self):
        self.assertEqual(recorder.FIELDNAMES[:len(LEGACY_FIELDNAMES)], LEGACY_FIELDNAMES)
        self.assertEqual(recorder.FIELDNAMES[len(LEGACY_FIELDNAMES):], [
            "control_mode", "set_value", "set_unit", "set_current_A",
            "target_power_W", "command_voltage_V", "limit_settings", "abort_reason",
            "phase", "cycle_number", "probe_active", "R_ref_ohm",
            "R_cold_before_heating_ohm", "cooling_end_reason", "cooling_duration_s"])

    def test_voltage_mode_legacy_values(self):
        smu, rows, error = self.run_mode("voltage", [(2.0, 0.1)], None)
        self.assertIsNone(error)
        row = rows[0]
        self.assertEqual(float(row["set_voltage_V"]), 2.0)
        self.assertEqual(float(row["current_limit_A"]), 0.16)
        self.assertEqual((row["control_mode"], row["set_unit"], row["abort_reason"]),
                         ("voltage", "V", ""))
        self.assertEqual(row["set_current_A"], "")
        self.assertFalse(smu._output_enabled)

    def test_power_mode_converges_within_3_percent(self):
        smu, rows, error = self.run_mode(
            "power", [(0.0, 0.2), (0.5, 2.0), (0.0, 0.2), (1.0, 1.5)], POWER_LIMITS)
        self.assertIsNone(error)
        for step, target in (("2", 0.5), ("4", 1.0)):
            step_rows = [r for r in rows if r["step_index"] == step]
            self.assertEqual(float(step_rows[0]["command_voltage_V"]), 0.1)  # probe
            settled = step_rows[len(step_rows) // 2:]
            self.assertGreater(len(settled), 10)
            for row in settled:
                self.assertAlmostEqual(float(row["power_W"]), target, delta=target * 0.03)
                self.assertEqual(row["set_voltage_V"], row["command_voltage_V"])
                self.assertEqual(float(row["target_power_W"]), target)
                self.assertLessEqual(float(row["command_voltage_V"]), 12.0)
        self.assertEqual(smu._source_function, "voltage")  # never switched
        self.assertFalse(smu._output_enabled)
        self.assertEqual(smu._voltage, 0)

    def test_power_mode_rate_limits_increases_and_clamps_at_v_max(self):
        limits = dict(POWER_LIMITS, V_max=3.0, P_max=1.5)
        smu, rows, error = self.run_mode("power", [(1.0, 0.6)], limits)
        self.assertIsNone(error)
        commands = [float(r["command_voltage_V"]) for r in rows]
        for before, after in zip(commands, commands[1:]):
            self.assertLessEqual(after - before, 1.0 + 1e-9)
        self.assertEqual(max(commands), 3.0)

    def test_current_mode_csv(self):
        smu, rows, error = self.run_mode("current", [(0.02, 0.2)], CURRENT_LIMITS)
        self.assertIsNone(error)
        row = rows[-1]
        self.assertEqual(row["set_voltage_V"], "")
        self.assertEqual(row["current_limit_A"], "")
        self.assertEqual(float(row["set_current_A"]), 0.02)
        self.assertEqual((row["control_mode"], row["set_unit"]), ("current", "A"))
        self.assertAlmostEqual(float(row["current_A"]), 0.02, delta=0.001)
        self.assertEqual(json.loads(row["limit_settings"])["V_lim"], 12.0)
        self.assertEqual(smu._current, 0)
        self.assertFalse(smu._output_enabled)

    def test_runtime_limit_switches_output_off_and_records_reason(self):
        for mode, profile, limits in (
                ("voltage", [(2.0, 5.0)], dict(VOLTAGE_LIMITS, P_max=0.01)),
                ("current", [(0.05, 5.0)], dict(CURRENT_LIMITS, P_max=0.1)),
                ("power", [(0.5, 5.0)], dict(POWER_LIMITS, P_max=0.5,
                                            probe_V=0.1))):
            smu = recorder.DummySourceMeasureUnit(current_limit=1.0)
            if mode == "power":
                # Sample suddenly conducts much better -> power overshoot.
                measure = smu.measure
                calls = []

                def measure_with_short(measure=measure, calls=calls, smu=smu):
                    calls.append(1)
                    if len(calls) == 15:
                        smu.base_resistance = 20.0
                    return measure()
                smu.measure = measure_with_short
                limits["R_jump_pct"] = 1000.0
            with self.subTest(mode=mode):
                start = time.monotonic()
                smu, rows, error = self.run_mode(mode, profile, limits, smu=smu,
                                                 name=f"abort_{mode}.csv")
                self.assertLess(time.monotonic() - start, 3)
                self.assertIsInstance(error, control_modes.SafetyAbort)
                self.assertIn("측정 전력", rows[-1]["abort_reason"])
                self.assertTrue(all(r["abort_reason"] == "" for r in rows[:-1]))
                self.assertFalse(smu._output_enabled)
                self.assertFalse(smu._connected)
                self.assertEqual((smu._voltage, smu._current), (0, 0))

    def test_resistance_jump_aborts(self):
        smu = recorder.DummySourceMeasureUnit(current_limit=0.16)
        measure = smu.measure
        calls = []

        def jumping():
            calls.append(1)
            if len(calls) == 5:
                smu.base_resistance *= 1.2  # e.g. a contact starting to fail
            return measure()
        smu.measure = jumping
        smu, rows, error = self.run_mode("voltage", [(1.0, 5.0)], VOLTAGE_LIMITS, smu=smu)
        self.assertIsInstance(error, control_modes.SafetyAbort)
        self.assertIn("저항 급변", rows[-1]["abort_reason"])
        self.assertEqual(len(rows), 5)
        self.assertFalse(smu._output_enabled)

    def test_power_mode_aborts_after_repeated_invalid_resistance(self):
        smu = recorder.DummySourceMeasureUnit(current_limit=0.16)
        smu.measure = lambda: driver.Measurement(0.1, 0.0, float("inf"))
        smu, rows, error = self.run_mode("power", [(0.5, 5.0)], POWER_LIMITS, smu=smu)
        self.assertIsInstance(error, control_modes.SafetyAbort)
        self.assertEqual(len(rows), 3)
        self.assertIn("연속", rows[-1]["abort_reason"])
        self.assertFalse(smu._output_enabled)

    def test_profile_over_limit_never_touches_the_instrument(self):
        for mode, profile, limits in (
                ("voltage", [(5, 1), (13, 1), (12.5, 1)], VOLTAGE_LIMITS),
                ("current", [(0.2, 1)], CURRENT_LIMITS),
                ("power", [(-0.1, 1), (2.0, 1)], POWER_LIMITS)):
            smu = recorder.DummySourceMeasureUnit()
            with self.subTest(mode=mode), patch.multiple(
                    recorder, CONTROL_MODE=mode, PROFILE=profile, SAFETY_LIMITS=limits,
                    use_serial_marker=False):
                with self.assertRaisesRegex(ValueError, "safety limits") as caught:
                    recorder.run_profile(smu, Path(self.temp.name) / "never.csv")
                self.assertFalse(smu._connected)
                if mode == "voltage":
                    self.assertIn("2행", str(caught.exception))
                    self.assertIn("3행", str(caught.exception))
        self.assertFalse((Path(self.temp.name) / "never.csv").exists())

    def test_new_modes_require_software_limits(self):
        for mode in ("power", "current"):
            with patch.multiple(recorder, CONTROL_MODE=mode, SAFETY_LIMITS=None,
                                use_serial_marker=False):
                with self.assertRaisesRegex(ValueError, "requires SAFETY_LIMITS"):
                    recorder.validate_settings()

    def test_infer_state_uses_step_value_in_any_mode(self):
        self.assertEqual(recorder.infer_state(None, 0.0), "initial_off")
        self.assertEqual(recorder.infer_state(0.0, 0.5), "heating")
        self.assertEqual(recorder.infer_state(0.5, 0.3), "cooling")
        self.assertEqual(recorder.infer_state(0.3, 0.3), "hold")
        self.assertEqual(recorder.infer_state(0.0, 0.0), "off_hold")


PROBE = {"enabled": True, "I_probe_A": 0.001, "V_probe_max_V": 0.2,
         "R_jump_skip_samples": 2}


def step_rows(rows, step):
    return [r for r in rows if r["step_index"] == str(step)]


class ProbeCurrentTests(RecorderTestBase):
    """Probe current in 0-output steps (냉각 중 측정 전류)."""

    HEAT = {"voltage": (2.0, VOLTAGE_LIMITS), "current": (0.05, CURRENT_LIMITS),
            "power": (0.3, POWER_LIMITS)}

    def test_cooling_resistance_recorded_in_every_mode(self):
        for mode, (heat, limits) in self.HEAT.items():
            smu = recorder.DummySourceMeasureUnit(base_resistance=30.0, current_limit=0.16)
            with self.subTest(mode=mode):
                smu, rows, error = self.run_mode(
                    mode, [(0.0, 0.3), (heat, 0.4), (0.0, 0.6)], limits, smu=smu,
                    name=f"probe_{mode}.csv", PROBE=PROBE)
                self.assertIsNone(error)
                self.assertEqual([r["phase"] for r in rows],
                                 [{1: "idle", 2: "heating", 3: "cooling"}[int(r["step_index"])]
                                  for r in rows])
                self.assertEqual({r["cycle_number"] for r in step_rows(rows, 1)}, {"0"})
                self.assertEqual({r["cycle_number"] for r in rows[len(step_rows(rows, 1)):]},
                                 {"1"})
                for step in (1, 3):
                    zero = step_rows(rows, step)
                    self.assertGreater(len(zero), 5)
                    for row in zero:
                        self.assertEqual(row["probe_active"], "1")
                        self.assertEqual(float(row["set_value"]), 0.0)
                        self.assertTrue(math.isfinite(float(row["resistance_ohm"])))
                        self.assertGreater(float(row["resistance_ohm"]), 25.0)
                        if mode != "current":
                            self.assertLessEqual(float(row["command_voltage_V"]), 0.2)
                    for row in zero[1:]:   # first idle sample starts from V_probe_max/4
                        self.assertAlmostEqual(float(row["current_A"]), 0.001, delta=0.0002)
                    self.assertEqual([r["cooling_end_reason"] for r in zero],
                                     [""] * (len(zero) - 1) + ["fixed"])
                    self.assertEqual(float(zero[-1]["cooling_duration_s"]),
                                     float(zero[-1]["step_duration_s"]))
                heating = step_rows(rows, 2)
                self.assertTrue(all(r["probe_active"] == "0" for r in heating))
                self.assertAlmostEqual(float(heating[0]["R_cold_before_heating_ohm"]), 30.0,
                                       delta=0.5)
                self.assertEqual(step_rows(rows, 1)[0]["R_cold_before_heating_ohm"], "")
                self.assertFalse(smu._output_enabled)
                self.assertEqual((smu._voltage, smu._current), (0, 0))

    def test_probe_off_keeps_zero_output(self):
        smu, rows, error = self.run_mode("voltage", [(0.0, 0.1), (1.0, 0.1), (0.0, 0.1)],
                                         VOLTAGE_LIMITS)
        self.assertIsNone(error)
        self.assertEqual({r["probe_active"] for r in rows}, {"0"})
        self.assertEqual({float(r["set_voltage_V"]) for r in rows if r["step_index"] != "2"},
                         {0.0})

    def test_power_mode_first_heating_uses_cold_resistance(self):
        smu = recorder.DummySourceMeasureUnit(base_resistance=30.0, current_limit=0.16,
                                              noise_ratio=0.0, time_constant_s=0.05)
        limits = dict(POWER_LIMITS, max_step_V=20.0)
        smu, rows, error = self.run_mode(
            "power", [(0.0, 0.3), (0.3, 0.5), (0.0, 0.5), (0.3, 0.3)], limits, smu=smu,
            PROBE=PROBE)
        self.assertIsNone(error)
        for heat_step in (2, 4):
            last_probe = step_rows(rows, heat_step - 1)[-1]
            first_heat = step_rows(rows, heat_step)[0]
            cold_r = float(last_probe["resistance_ohm"])
            self.assertAlmostEqual(cold_r, 30.0, delta=0.5)        # cooled down
            command = float(first_heat["command_voltage_V"])
            self.assertNotEqual(command, 0.1)                      # no 0.1 V re-probe
            self.assertAlmostEqual(command, math.sqrt(0.3 * cold_r), delta=0.02)

    def test_jump_check_skipped_only_right_after_regime_change(self):
        def glitching_smu(glitch_samples, extra=None):
            """R reads 30 % high on the given probe samples after heating."""
            smu = recorder.DummySourceMeasureUnit(base_resistance=30.0, current_limit=0.16,
                                                  noise_ratio=0.0)
            measure = smu.measure
            state = {"probe_count": 0}

            def measure_with_glitch():
                m = measure()
                if smu._voltage > 0.2:
                    state["probe_count"] = 0
                    return m
                state["probe_count"] += 1
                if extra is not None and state["probe_count"] == 1:
                    return extra(m)
                if state["probe_count"] in glitch_samples:
                    return driver.Measurement(m.voltage * 1.3, m.current, m.resistance * 1.3)
                return m
            smu.measure = measure_with_glitch
            return smu

        profile = [(2.0, 0.3), (0.0, 0.5)]
        # Glitch on the 2 excluded samples right after heating -> no abort.
        smu, rows, error = self.run_mode("voltage", profile, VOLTAGE_LIMITS,
                                         smu=glitching_smu({1, 2}), PROBE=PROBE)
        self.assertIsNone(error)
        self.assertGreater(float(step_rows(rows, 2)[0]["resistance_ohm"]), 30 * 1.25)
        # The same glitch later in the step is still a safety abort.
        smu, rows, error = self.run_mode("voltage", profile, VOLTAGE_LIMITS,
                                         smu=glitching_smu({6}), PROBE=PROBE, name="late.csv")
        self.assertIsInstance(error, control_modes.SafetyAbort)
        self.assertIn("저항 급변", rows[-1]["abort_reason"])
        self.assertEqual(rows[-1]["probe_active"], "1")
        self.assertFalse(smu._output_enabled)
        # Other limits are not excluded: power over P_max in an excluded sample.
        smu, rows, error = self.run_mode(
            "voltage", profile, VOLTAGE_LIMITS, PROBE=PROBE, name="power.csv",
            smu=glitching_smu(set(), extra=lambda m: driver.Measurement(0.1, 20.0, 0.005)))
        self.assertIsInstance(error, control_modes.SafetyAbort)
        self.assertIn("측정 전력", rows[-1]["abort_reason"])
        self.assertFalse(smu._output_enabled)

    def test_current_mode_probe_voltage_over_limit_aborts(self):
        smu = recorder.DummySourceMeasureUnit(base_resistance=1000.0)   # open contact
        smu, rows, error = self.run_mode("current", [(0.0, 1.0)], CURRENT_LIMITS, smu=smu,
                                         PROBE=PROBE)
        self.assertIsInstance(error, control_modes.SafetyAbort)
        self.assertIn("V_probe_max", rows[-1]["abort_reason"])
        self.assertEqual(len(rows), 1)
        self.assertFalse(smu._output_enabled)

    def test_probe_settings_validated_before_start(self):
        for mode, probe, limits, message in (
                ("voltage", dict(PROBE, I_probe_A=5e-5), VOLTAGE_LIMITS, "R 유효 기준"),
                ("voltage", dict(PROBE, I_probe_A=0.2), VOLTAGE_LIMITS, "I_lim"),
                ("power", dict(PROBE, V_probe_max_V=20.0), POWER_LIMITS, "V_max"),
                ("current", dict(PROBE, I_probe_A=0.2), CURRENT_LIMITS, "I_max"),
                ("voltage", PROBE, None, "requires SAFETY_LIMITS")):
            smu = recorder.DummySourceMeasureUnit()
            with self.subTest(message=message), patch.multiple(
                    recorder, CONTROL_MODE=mode, PROFILE=[(0.0, 1.0)], SAFETY_LIMITS=limits,
                    PROBE=probe, use_serial_marker=False, CURRENT_LIMIT=0.16):
                with self.assertRaisesRegex(ValueError, message):
                    recorder.run_profile(smu, Path(self.temp.name) / "never.csv")
                self.assertFalse(smu._connected)


def cool(t_max, end, note="cool"):
    return (0.0, t_max, 1, note, {"end": end})


class ConditionalCoolingTests(RecorderTestBase):
    """Conditional end of 0-output steps (조건부 냉각)."""

    LIMITS = dict(VOLTAGE_LIMITS, R_jump_pct=50.0)   # training jumps R on purpose

    def smu(self, training=0.0):
        return recorder.DummySourceMeasureUnit(
            base_resistance=30.0, current_limit=0.16, noise_ratio=0.0005,
            time_constant_s=0.1, training_gain_per_w=training)

    def test_rref_return_ends_cooling(self):
        end = {"type": "rref", "t_min_s": 0.2, "tol_pct": 0.5, "k_samples": 5}
        smu, rows, error = self.run_mode(
            "voltage", [(2.0, 0.4), cool(5.0, end)], self.LIMITS, smu=self.smu(),
            PROBE=PROBE, MANUAL_R_REF=30.0)
        self.assertIsNone(error)
        cooling = step_rows(rows, 2)
        last = cooling[-1]
        self.assertEqual(last["cooling_end_reason"], "condition")
        self.assertTrue(all(r["cooling_end_reason"] == "" for r in cooling[:-1]))
        duration = float(last["cooling_duration_s"])
        self.assertGreaterEqual(duration, 0.2)
        self.assertLess(duration, 2.0)
        self.assertGreater(float(cooling[0]["resistance_ohm"]), 30.0 * 1.02)   # was hot
        for row in cooling[-5:]:
            self.assertLessEqual(abs(float(row["resistance_ohm"]) - 30.0) / 30.0, 0.005)
            self.assertEqual(float(row["R_ref_ohm"]), 30.0)

    def test_slope_ends_cooling_when_reference_moved(self):
        # Training: cold R rises ~13 % for good, R_ref is never reached again.
        end = {"type": "slope", "t_min_s": 0.2, "window_s": 0.3, "slope_pct_per_s": 0.5}
        smu, rows, error = self.run_mode(
            "voltage", [(2.0, 0.4), cool(5.0, end)], self.LIMITS, smu=self.smu(training=1.0),
            PROBE=PROBE)
        self.assertIsNone(error)
        cooling = step_rows(rows, 2)
        self.assertEqual(cooling[-1]["cooling_end_reason"], "condition")
        self.assertLess(float(cooling[-1]["cooling_duration_s"]), 2.0)
        settled = float(cooling[-1]["resistance_ohm"])
        self.assertGreater(settled, 30.0 * 1.08)                       # new cold R
        self.assertLess(float(cooling[0]["resistance_ohm"]) - settled, 5.0)
        self.assertGreater(float(cooling[0]["resistance_ohm"]), settled * 1.02)

    def test_timeout_moves_on_and_optional_abort(self):
        end = {"type": "rref", "t_min_s": 0.1, "tol_pct": 0.5, "k_samples": 5}
        profile = [(2.0, 0.3), cool(0.5, end), (2.0, 0.2), cool(0.5, end), (2.0, 0.2)]
        smu, rows, error = self.run_mode(
            "voltage", profile, self.LIMITS, smu=self.smu(training=1.0),
            PROBE=PROBE, MANUAL_R_REF=30.0)
        self.assertIsNone(error)                                   # option off: continue
        for step in (2, 4):
            last = step_rows(rows, step)[-1]
            self.assertEqual(last["cooling_end_reason"], "timeout")
            self.assertEqual(float(last["cooling_duration_s"]), 0.5)
            self.assertGreaterEqual(float(last["step_elapsed_s"]), 0.45)
        self.assertEqual(rows[-1]["step_index"], "5")

        smu, rows, error = self.run_mode(
            "voltage", profile, self.LIMITS, smu=self.smu(training=1.0), name="abort.csv",
            PROBE=PROBE, MANUAL_R_REF=30.0, COOLING_TIMEOUT_ABORT=2)
        self.assertIsInstance(error, control_modes.SafetyAbort)
        self.assertIn("타임아웃 2회", rows[-1]["abort_reason"])
        self.assertEqual(rows[-1]["step_index"], "4")
        self.assertEqual(rows[-1]["cooling_end_reason"], "timeout")
        self.assertFalse(smu._output_enabled)

    def test_invalid_conditional_steps_refused_before_start(self):
        rref = {"type": "rref"}
        for profile, probe, r_ref, message in (
                ([(2.0, 40.0, 1, "", {"end": rref})], PROBE, 30.0, "값이 0인 단계"),
                ([cool(40.0, rref)], None, 30.0, "측정 전류가 켜져"),
                ([cool(40.0, rref)], PROBE, None, "R_ref가 없습니다"),
                ([cool(5.0, rref)], PROBE, 30.0, "t_min"),
                ([cool(40.0, {"type": "magic"})], PROBE, 30.0, "알 수 없는")):
            smu = recorder.DummySourceMeasureUnit()
            with self.subTest(message=message), patch.multiple(
                    recorder, CONTROL_MODE="voltage", PROFILE=profile,
                    SAFETY_LIMITS=VOLTAGE_LIMITS, PROBE=probe, MANUAL_R_REF=r_ref,
                    use_serial_marker=False, CURRENT_LIMIT=0.16):
                with self.assertRaisesRegex(ValueError, message):
                    recorder.run_profile(smu, Path(self.temp.name) / "never.csv")
                self.assertFalse(smu._connected)


class RrefStepTests(RecorderTestBase):
    """R_ref measurement step (R_ref 측정 단계)."""

    SPEC = {"t_on_s": 0.4, "t_off_s": 0.2, "repeats": 2}

    def rref_step(self, spec=None):
        spec = spec or self.SPEC
        duration = spec["repeats"] * (spec["t_on_s"] + spec["t_off_s"])
        return (0.0, duration, 1, "R_ref measure", {"kind": "rref", "rref": spec})

    def test_average_of_last_window_of_each_on_segment(self):
        segments = [[(1.0, 100.0), (2.5, 31.0), (3.0, 30.0), (4.0, 30.0), (4.9, 30.0)],
                    [(2.1, 29.0), (4.0, 31.0)],
                    [(0.5, 50.0)],          # nothing in its last 3 s -> ignored
                    []]
        self.assertAlmostEqual(control_modes.rref_from_segments(segments, 5.0), 30.125)
        self.assertAlmostEqual(control_modes.rref_from_segments([[(0.1, 20.0), (1.0, 22.0)]],
                                                                2.0), 21.0)  # t_on < 3 s
        self.assertIsNone(control_modes.rref_from_segments([[], []], 5.0))
        self.assertEqual(control_modes.rref_segment(self.SPEC, 0.1)[:2], (0, True))
        self.assertEqual(control_modes.rref_segment(self.SPEC, 0.5)[:2], (0, False))
        self.assertEqual(control_modes.rref_segment(self.SPEC, 0.7)[:2], (1, True))
        self.assertEqual(control_modes.rref_segment(self.SPEC, 9.0)[:2], (1, False))

    def test_rref_step_in_every_mode_feeds_r_ref_condition(self):
        end = {"type": "rref", "t_min_s": 0.1, "tol_pct": 1.0, "k_samples": 3}
        heat = {"voltage": (2.0, VOLTAGE_LIMITS), "current": (0.05, CURRENT_LIMITS),
                "power": (0.15, POWER_LIMITS)}
        for mode, (value, limits) in heat.items():
            smu = recorder.DummySourceMeasureUnit(base_resistance=30.0, current_limit=0.16,
                                                  noise_ratio=0.0005, time_constant_s=0.1)
            events = []
            with self.subTest(mode=mode):
                smu, rows, error = self.run_mode(
                    mode, [self.rref_step(), (value, 0.3), cool(5.0, end)],
                    dict(limits, R_jump_pct=20.0), smu=smu, name=f"rref_{mode}.csv",
                    PROBE=PROBE, MANUAL_R_REF=99.0,
                    on_event=lambda kind, v: events.append((kind, v)))
                self.assertIsNone(error)
                rref_rows = step_rows(rows, 1)
                self.assertEqual({r["phase"] for r in rref_rows}, {"rref"})
                self.assertEqual({float(r["R_ref_ohm"]) for r in rref_rows}, {99.0})
                segments = {}
                for row in rref_rows:
                    elapsed = float(row["step_elapsed_s"])
                    within = elapsed % 0.6
                    # Segment is chosen at loop start; step_elapsed_s is the
                    # measurement time (up to one sample later) -> margins.
                    if 0.05 < within < 0.37 and elapsed < 1.18:
                        self.assertEqual(row["probe_active"], "1", elapsed)
                    if 0.45 < within < 0.58:
                        self.assertEqual(row["probe_active"], "0", elapsed)
                        self.assertAlmostEqual(float(row["current_A"]), 0.0, delta=1e-6)
                    if row["probe_active"] == "1":
                        if mode != "current":
                            self.assertLessEqual(float(row["command_voltage_V"]), 0.2)
                        segments.setdefault(int(elapsed // 0.6), []).append(
                            float(row["resistance_ohm"]))
                self.assertEqual(sorted(segments), [0, 1])
                expected = sum(sum(v) / len(v) for v in segments.values()) / 2
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0][0], "R_ref")
                self.assertAlmostEqual(events[0][1], expected, places=9)
                self.assertAlmostEqual(expected, 30.0, delta=0.2)
                for row in rows[len(rref_rows):]:
                    self.assertAlmostEqual(float(row["R_ref_ohm"]), expected, places=9)
                self.assertEqual(step_rows(rows, 3)[-1]["cooling_end_reason"], "condition")

    def test_rref_step_without_valid_resistance_fails_safely(self):
        smu = recorder.DummySourceMeasureUnit(current_limit=0.16)
        smu.measure = lambda: driver.Measurement(0.0, 0.0, float("inf"))
        with self.assertRaisesRegex(RuntimeError, "R_ref 측정 실패"):
            self.run_mode("voltage", [self.rref_step()], VOLTAGE_LIMITS,
                          smu=smu, PROBE=dict(PROBE, enabled=False))
        self.assertFalse(smu._output_enabled)
        self.assertFalse(smu._connected)

    def test_rref_step_validation(self):
        for profile, probe, message in (
                ([self.rref_step()], None, "측정 전류 설정이 필요"),
                ([(0.0, 5.0, 1, "", {"kind": "rref", "rref": self.SPEC})], PROBE, "≠"),
                ([(1.0, 1.2, 1, "", {"kind": "rref", "rref": self.SPEC})], PROBE, "값은 0")):
            with self.subTest(message=message), patch.multiple(
                    recorder, CONTROL_MODE="voltage", PROFILE=profile,
                    SAFETY_LIMITS=VOLTAGE_LIMITS, PROBE=probe, MANUAL_R_REF=None,
                    use_serial_marker=False, CURRENT_LIMIT=0.16):
                with self.assertRaisesRegex(ValueError, message):
                    recorder.validate_settings()


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name) / "profiles"

    def tearDown(self):
        self.temp.cleanup()

    def test_name_validation(self):
        # Surrounding spaces are trimmed, not rejected.
        self.assertEqual(profile_store.validate_name("  0.5W 가열 10초  "), "0.5W 가열 10초")
        for bad in ("", "  ", "a/b", "a\\b", "a:b", 'a"b', "a*b", "a?b", "a<b", "a|b",
                    "tab\there", "끝점.", "점 .", "CON", "com1.json", "x" * 101,
                    "default", "EXP4"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                profile_store.validate_name(bad)

    def test_save_list_load_delete_round_trip(self):
        data = {"name": "전력 테스트", "mode": "power",
                "steps": [{"value": 0.5, "duration_s": 10, "note": "가열"},
                          {"value": 0, "duration_s": 30, "note": ""}],
                "limits": {"power": {"I_lim": 0.16, "P_max": 1.0, "V_max": 10.0},
                           "common": {"R_jump_pct": 5.0}},
                "description": "설명"}
        path = profile_store.save_profile(data, self.dir)
        self.assertEqual(path.name, "전력 테스트.json")
        with self.assertRaises(FileExistsError):
            profile_store.save_profile(data, self.dir)
        first = profile_store.load_profile(path)
        profile_store.save_profile(dict(data, description="수정"), self.dir, overwrite=True)
        loaded = profile_store.load_profile(path)
        self.assertEqual(loaded["created_at"], first["created_at"])
        self.assertEqual(loaded["description"], "수정")
        self.assertEqual(loaded["steps"], [
            {"value": 0.5, "duration_s": 10.0, "note": "가열"},
            {"value": 0.0, "duration_s": 30.0, "note": ""}])
        self.assertEqual(loaded["limits"]["power"]["P_max"], 1.0)

        summaries, errors = profile_store.list_profiles(self.dir)
        self.assertEqual(errors, [])
        user = [s for s in summaries if not s["builtin"]]
        self.assertEqual([(s["name"], s["mode"], s["steps"], s["total_s"]) for s in user],
                         [("전력 테스트", "power", 2, 40.0)])
        builtin = {s["name"]: s for s in summaries if s["builtin"]}
        self.assertEqual(builtin["exp4"]["mode"], "voltage")

        profile_store.delete_profile("전력 테스트", self.dir)
        self.assertFalse(path.exists())

    def test_broken_files_are_reported_not_fatal(self):
        self.dir.mkdir()
        (self.dir / "broken.json").write_text("{", encoding="utf-8")
        (self.dir / "bad_mode.json").write_text(
            json.dumps({"mode": "magic", "steps": [{"value": 1, "duration_s": 1}]}),
            encoding="utf-8")
        summaries, errors = profile_store.list_profiles(self.dir)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(s["builtin"] for s in summaries))

    def test_last_settings(self):
        path = Path(self.temp.name) / "settings" / "last_settings.json"
        self.assertEqual(profile_store.load_last_settings(path), {})
        profile_store.save_last_settings({"limits": {"common": {"R_jump_pct": 7}}}, path)
        self.assertEqual(profile_store.load_last_settings(path)["limits"],
                         {"common": {"R_jump_pct": 7}})
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(profile_store.load_last_settings(path), {})


class FakeInstrument:
    def __init__(self, replies=()):
        self.writes = []
        self.replies = list(replies)

    def write(self, command):
        self.writes.append(command)

    def query(self, command):
        self.writes.append(command)
        return self.replies.pop(0)

    def close(self):
        pass


class DriverTests(unittest.TestCase):
    def keithley(self, replies=()):
        smu = driver.Keithley2450("fake", current_limit=0.16)
        smu._instrument = FakeInstrument(replies)
        return smu

    def test_current_source_configuration_commands(self):
        smu = self.keithley()
        smu.configure_current_source(4, 12.0)
        self.assertEqual(smu._instrument.writes, [
            ":SOUR:FUNC CURR", ":SOUR:CURR 0", ":SOUR:CURR:VLIM 12",
            ':SENS:FUNC "VOLT"', ":SENS:VOLT:RANG:AUTO ON", ":SENS:VOLT:RSEN ON",
            ":SOUR:CURR:READ:BACK ON", ':TRAC:CLE "defbuffer1"'])
        smu._instrument.writes.clear()
        smu.set_current(0.02)
        smu.set_source_zero()
        self.assertEqual(smu._instrument.writes, [":SOUR:CURR 0.02", ":SOUR:CURR 0"])
        with self.assertRaises(RuntimeError):
            smu.set_voltage(1.0)

    def test_source_function_never_changes_with_output_on(self):
        for smu in (self.keithley(), recorder.DummySourceMeasureUnit()):
            smu.connect() if isinstance(smu, recorder.DummySourceMeasureUnit) else None
            smu.output_on()
            with self.assertRaises(RuntimeError):
                smu.configure_current_source(2, 12.0)
            smu.output_off()
            smu.configure_current_source(2, 12.0)

    def test_current_source_measure_maps_reading_and_readback(self):
        smu = self.keithley(replies=["1.6", "0.02"])
        smu.configure_current_source(2, 12.0)
        measured = smu.measure()
        self.assertEqual((measured.voltage, measured.current), (1.6, 0.02))
        self.assertAlmostEqual(measured.resistance, 80.0)

    def test_dummy_power_heats_and_raises_resistance(self):
        smu = recorder.DummySourceMeasureUnit(noise_ratio=0.0, time_constant_s=0.05)
        smu.connect()
        smu.set_voltage(8.0)
        smu.output_on()
        cold = smu.measure().resistance
        time.sleep(0.3)
        hot = smu.measure().resistance
        self.assertGreater(hot, cold * 1.1)

    def test_dummy_cools_down_over_time_at_zero_output(self):
        smu = recorder.DummySourceMeasureUnit(noise_ratio=0.0, time_constant_s=0.05)
        smu.connect()
        smu.temperature_rise = 50.0          # hot sample
        smu.set_voltage(0.01)                # ~0.1 mA probe: negligible heating
        smu.output_on()
        hot = smu.measure().resistance
        time.sleep(0.3)
        cold = smu.measure().resistance
        self.assertGreater(hot, 100.0 * 1.2)
        self.assertAlmostEqual(cold, 100.0, delta=0.5)

    def test_dummy_training_raises_cold_resistance_permanently(self):
        smu = recorder.DummySourceMeasureUnit(noise_ratio=0.0, time_constant_s=0.02,
                                              training_gain_per_w=0.2)
        smu.connect()
        smu.output_on()
        for volts in (5.0, 0.0, 5.0, 0.0):   # same peak power twice
            smu.set_voltage(volts)
            time.sleep(0.15)
            smu.measure()
        first = smu.base_resistance
        self.assertGreater(first, 100.0)     # new maximum -> permanent rise
        smu.set_voltage(3.0)                 # lower power: no further training
        time.sleep(0.1)
        smu.measure()
        self.assertEqual(smu.base_resistance, first)
        default = recorder.DummySourceMeasureUnit()
        self.assertEqual(default.training_gain, 0.0)


if __name__ == "__main__":
    unittest.main()

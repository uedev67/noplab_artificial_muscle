"""Hardware-free checks: python -m unittest test_main_gui -v."""

import csv
import io
import json
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


class GUIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.gui = main_gui.ExperimentGUI(self.root)
        for item in list(self.gui.rows):
            self.gui.remove_row(item)
        self.gui.add_row(2, 0.03, "test")
        self.gui.add_row(0, 0.03)
        for key, value in {"sample": "22", "cycle": "5", "directory": self.temp.name,
                           "SAMPLE_INTERVAL_S": "0.01"}.items():
            self.gui.variables[key].set(value)
        self.gui.dummy.set(True)

    def tearDown(self):
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
        self.assertEqual(set(rows[0]), set(recorder.FIELDNAMES))
        self.assertEqual({r["sample_id"] for r in rows}, {"22"})
        self.assertEqual({r["cycle_index"] for r in rows}, {"5"})
        self.assertEqual({r["step_index"] for r in rows}, {"1", "2"})
        self.assertTrue(any(kind == "done" for kind, _ in self.gui.messages.queue))
        with self.assertRaises(ValueError):
            self.gui.collect_settings()

    def test_validation(self):
        for bad in ("nan", "inf", "0", "-1"):
            self.gui.variables["CURRENT_LIMIT"].set(bad)
            with self.assertRaises(ValueError):
                self.gui.collect_settings()
        self.gui.variables["CURRENT_LIMIT"].set("0.35")
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


class ControlModeRecorderTests(unittest.TestCase):
    """run_profile() in each control mode with the dummy SMU (no hardware)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def run_mode(self, mode, profile, limits, smu=None, name="run.csv"):
        smu = smu or recorder.DummySourceMeasureUnit(current_limit=0.16)
        path = Path(self.temp.name) / name
        with patch.multiple(recorder, CONTROL_MODE=mode, PROFILE=profile,
                            SAFETY_LIMITS=limits, use_serial_marker=False,
                            SAMPLE_INTERVAL_S=0.02, CURRENT_LIMIT=0.16,
                            VOLTAGE_LIMIT=12.0), \
                redirect_stdout(io.StringIO()):
            try:
                recorder.run_profile(smu, path)
                error = None
            except control_modes.SafetyAbort as exc:
                error = exc
        rows = []
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        return smu, rows, error

    def test_legacy_columns_keep_order_and_new_columns_are_appended(self):
        self.assertEqual(recorder.FIELDNAMES[:len(LEGACY_FIELDNAMES)], LEGACY_FIELDNAMES)
        self.assertEqual(recorder.FIELDNAMES[len(LEGACY_FIELDNAMES):], [
            "control_mode", "set_value", "set_unit", "set_current_A",
            "target_power_W", "command_voltage_V", "limit_settings", "abort_reason"])

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


if __name__ == "__main__":
    unittest.main()

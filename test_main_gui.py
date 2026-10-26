"""Hardware-free checks: python -m unittest test_main_gui -v."""

import csv
import tempfile
import threading
import time
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()

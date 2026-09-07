"""Run a manually specified DC voltage profile with a Keithley 2450."""

from __future__ import annotations

import argparse
import csv
import math
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO

from keithley_driver import DummySourceMeasureUnit, Keithley2450, SourceMeasureUnit

# Edit these settings before an experiment.
PROFILE = [(0.0,5.0),(5.0, 10.0), (10.0, 10.0), 
           (15.0, 10.0), (20.0, 10.0),
           (25.0, 10.0), (0.0, 10.0),
           ]
WIRE_MODE = 4  # 2 or 4
CURRENT_LIMIT = 0.15  # A
SAMPLE_INTERVAL_S = 0.1
RESOURCE_NAME = "USB0::0x05E6::0x2450::04495764::INSTR"


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%m%d%H%M")
    return Path(f"data/manual_voltage_profile_{timestamp}.csv")

FIELDNAMES = ["time", "step_index", "set_voltage", "measured_voltage", "current",
              "resistance", "power", "energy", "step_energy"]


def validate_settings() -> None:
    if WIRE_MODE not in (2, 4):
        raise ValueError("WIRE_MODE must be 2 or 4")
    if CURRENT_LIMIT <= 0 or SAMPLE_INTERVAL_S <= 0 or not PROFILE:
        raise ValueError("CURRENT_LIMIT, SAMPLE_INTERVAL_S, and PROFILE must be positive/non-empty")
    for index, (voltage, duration) in enumerate(PROFILE, 1):
        if not math.isfinite(voltage) or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"Invalid PROFILE entry at step {index}")


def run_profile(smu: SourceMeasureUnit, output_path: Path) -> None:
    validate_settings()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_file: TextIO | None = None
    total_energy = 0.0
    total_previous_time = total_previous_power = None
    smu.connect()
    try:
        smu.configure_voltage_measurement(WIRE_MODE)
        csv_file = output_path.open("w", newline="", encoding="utf-8-sig")
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        writer.writeheader()
        smu.set_voltage(0.0)
        smu.output_on()
        experiment_start = time.monotonic()

        for step_index, (set_voltage, duration_s) in enumerate(PROFILE, 1):
            print(f"Step {step_index}/{len(PROFILE)} : {set_voltage:.1f} V for {duration_s:.1f} s")
            smu.set_voltage(set_voltage)
            step_start = time.monotonic()
            step_energy = 0.0
            step_previous_time = step_previous_power = None

            while True:
                sample_time = time.monotonic()
                if step_previous_time is not None and sample_time - step_start >= duration_s:
                    break
                measured = smu.measure()
                power = measured.voltage * measured.current
                if total_previous_time is not None and total_previous_power is not None:
                    total_energy += ((total_previous_power + power) / 2
                                     * (sample_time - total_previous_time))
                if step_previous_time is not None and step_previous_power is not None:
                    step_energy += ((step_previous_power + power) / 2
                                    * (sample_time - step_previous_time))
                writer.writerow({
                    "time": sample_time - experiment_start, "step_index": step_index,
                    "set_voltage": set_voltage, "measured_voltage": measured.voltage,
                    "current": measured.current, "resistance": measured.resistance,
                    "power": power, "energy": total_energy, "step_energy": step_energy,
                })
                csv_file.flush()
                print(f"V={measured.voltage:.6g} V, I={measured.current:.6g} A, "
                      f"R={measured.resistance:.6g} ohm, P={power:.6g} W, "
                      f"total E={total_energy:.6g} J, step E={step_energy:.6g} J")
                total_previous_time, total_previous_power = sample_time, power
                step_previous_time, step_previous_power = sample_time, power
                sleep_s = min(SAMPLE_INTERVAL_S,
                              step_start + duration_s - time.monotonic())
                if sleep_s > 0:
                    time.sleep(sleep_s)
    finally:
        if csv_file is not None:
            csv_file.close()
        try:
            smu.set_voltage(0.0)
        finally:
            try:
                smu.output_off()
            finally:
                smu.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a manual DC voltage profile")
    parser.add_argument(
        "--dummy",
        action="store_true",
        help="run without a Keithley 2450 for logic/CSV testing",
    )
    parser.add_argument("--resource", default=RESOURCE_NAME)
    parser.add_argument("--output", type=Path, default=default_output_path())
    args = parser.parse_args()
    smu = (
        DummySourceMeasureUnit()
        if args.dummy
        else Keithley2450(args.resource, CURRENT_LIMIT)
    )
    print(f"Starting {'dummy' if args.dummy else 'Keithley 2450'} mode; CSV: {args.output}")
    try:
        run_profile(smu, args.output)
    except KeyboardInterrupt:
        print("Interrupted. Safe shutdown completed (0 V -> output OFF).")
    except Exception as exc:
        print(f"Experiment failed: {exc}")
        raise
    else:
        print("Profile complete. Safe shutdown completed (0 V -> output OFF).")


if __name__ == "__main__":
    main()

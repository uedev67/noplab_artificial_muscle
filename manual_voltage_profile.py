"""Run a manually specified DC voltage profile with a Keithley 2450.

This script is intended to be the common raw-data logger for artificial-muscle
experiments 1-4.

Main goals
----------
1. Keep the existing simple PROFILE-based workflow.
2. Store as much raw / contextual information as possible.
3. Make later synchronization with Sony camera and IR camera easier.
4. Preserve step, cycle, state, energy, and timing information for later analysis.

PROFILE entry formats
---------------------
Simple form:
    (voltage_V, duration_s)

Extended form:
    (voltage_V, duration_s, cycle_index, note)

Examples:
    (10.0, 10.0)
    (10.0, 10.0, 3, "cycle 3 heating")
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from keithley_driver import (
    DummySourceMeasureUnit,
    Keithley2450,
    SourceMeasureUnit,
)


# =============================================================================
# USER SETTINGS
# =============================================================================

# ---------------------------------------------------------------------------
# Voltage profile
#
# Basic:
#   (voltage_V, duration_s)
#
# Extended:
#   (voltage_V, duration_s, cycle_index, note)
#
# 실험 4처럼 cycle 정보를 명확하게 남기고 싶으면 extended form을 권장.
# ---------------------------------------------------------------------------

PROFILE = [
    (0.0, 5.0),
    (5.0, 10.0),
    (10.0, 10.0),
    (15.0, 10.0),
    (20.0, 10.0),
    (25.0, 10.0),
    (0.0, 10.0),
]


# ---------------------------------------------------------------------------
# Keithley settings
# ---------------------------------------------------------------------------

WIRE_MODE = 4  # 2 or 4

CURRENT_LIMIT = 0.15  # A

SAMPLE_INTERVAL_S = 0.1

RESOURCE_NAME = "USB0::0x05E6::0x2450::04495764::INSTR"


# ---------------------------------------------------------------------------
# Experiment metadata
#
# 모르는 값은 "" 로 두면 됨.
# 가능하면 실험 직전에 채우는 것을 권장.
# ---------------------------------------------------------------------------

EXPERIMENT_ID = ""
# 예:
# "EXP1A"
# "EXP1B"
# "EXP1C"
# "EXP2"
# "EXP3_10V"
# "EXP4"

SAMPLE_ID = ""
# 예: "NYLON_NI_01"

OPERATOR = ""

PRELOAD_G = ""
# 예: 100 g preload라면 "100"

INITIAL_LENGTH_MM = ""
# 초기 길이를 알고 있으면 입력

AMBIENT_TEMP_C = ""
# 실험 시작 시 주변온도

IR_CAMERA_ID = ""
# 예: "FLIR_A655sc"

SONY_CAMERA_ID = ""
# 예: "Sony_A6400"

SYNC_NOTE = ""
# 예:
# "Sony LED + IR thermal marker triggered at experiment start"

GENERAL_NOTE = ""
# 예:
# "sample visually intact before test"


# =============================================================================
# CSV SETTINGS
# =============================================================================


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    experiment = EXPERIMENT_ID.strip() or "manual_voltage_profile"

    return Path(
        f"data/{experiment}_{timestamp}.csv"
    )


FIELDNAMES = [

    # -------------------------------------------------------------------------
    # Identification
    # -------------------------------------------------------------------------

    "experiment_id",
    "sample_id",
    "operator",

    "sample_index",

    # -------------------------------------------------------------------------
    # Absolute and relative time
    # -------------------------------------------------------------------------

    "timestamp_local",
    "timestamp_utc",

    "time_s",
    "dt_s",

    "measurement_duration_s",

    # -------------------------------------------------------------------------
    # Step / cycle information
    # -------------------------------------------------------------------------

    "step_index",
    "cycle_index",

    "state",

    "step_note",

    "step_elapsed_s",
    "step_duration_s",

    # -------------------------------------------------------------------------
    # Keithley configuration
    # -------------------------------------------------------------------------

    "wire_mode",

    # -------------------------------------------------------------------------
    # Electrical raw data
    # -------------------------------------------------------------------------

    "set_voltage_V",

    "measured_voltage_V",

    "voltage_error_V",

    "current_A",

    "current_limit_A",

    "current_limit_ratio",

    "near_current_limit",

    "resistance_ohm",

    "conductance_S",

    "power_W",

    # -------------------------------------------------------------------------
    # Energy
    # -------------------------------------------------------------------------

    "energy_J",

    "step_energy_J",

    # -------------------------------------------------------------------------
    # Fixed / slowly varying experiment metadata
    # -------------------------------------------------------------------------

    "preload_g",

    "initial_length_mm",

    "ambient_temp_C",

    "ir_camera_id",

    "sony_camera_id",

    "sync_note",

    "general_note",
]


# =============================================================================
# PROFILE HELPERS
# =============================================================================


def parse_profile_entry(
    entry,
    default_cycle: int = 1,
):
    """Parse a PROFILE entry.

    Supported formats:

    (voltage, duration)

    or

    (voltage, duration, cycle_index, note)
    """

    if len(entry) == 2:

        voltage, duration = entry

        cycle_index = default_cycle

        note = ""

    elif len(entry) == 4:

        voltage, duration, cycle_index, note = entry

    else:

        raise ValueError(
            "PROFILE entry must be "
            "(voltage, duration) "
            "or "
            "(voltage, duration, cycle_index, note)"
        )

    return (
        float(voltage),
        float(duration),
        int(cycle_index),
        str(note),
    )


def infer_state(
    previous_set_voltage: float | None,
    set_voltage: float,
) -> str:
    """Infer an input-state label.

    IMPORTANT:
    이 state는 실제 thermal state가 아니라
    '전압 명령 변화 기준' 라벨이다.

    실제 heating / cooling 여부는 나중에 IR 데이터의
    dT/dt를 이용해 재분류하는 것이 더 정확하다.
    """

    if previous_set_voltage is None:

        if set_voltage == 0.0:
            return "initial_off"

        return "initial"

    if set_voltage > previous_set_voltage:
        return "heating"

    if set_voltage < previous_set_voltage:
        return "cooling"

    if set_voltage == 0.0:
        return "off_hold"

    return "hold"


# =============================================================================
# VALIDATION
# =============================================================================


def validate_settings() -> None:

    if WIRE_MODE not in (2, 4):

        raise ValueError(
            "WIRE_MODE must be 2 or 4"
        )

    if CURRENT_LIMIT <= 0:

        raise ValueError(
            "CURRENT_LIMIT must be positive"
        )

    if SAMPLE_INTERVAL_S <= 0:

        raise ValueError(
            "SAMPLE_INTERVAL_S must be positive"
        )

    if not PROFILE:

        raise ValueError(
            "PROFILE must not be empty"
        )

    for index, entry in enumerate(
        PROFILE,
        start=1,
    ):

        (
            voltage,
            duration,
            cycle_index,
            _,
        ) = parse_profile_entry(entry)

        if not math.isfinite(voltage):

            raise ValueError(
                f"Invalid voltage at PROFILE step {index}"
            )

        if (
            not math.isfinite(duration)
            or duration <= 0
        ):

            raise ValueError(
                f"Invalid duration at PROFILE step {index}"
            )

        if cycle_index <= 0:

            raise ValueError(
                f"cycle_index must be >= 1 "
                f"at PROFILE step {index}"
            )


# =============================================================================
# MAIN EXPERIMENT
# =============================================================================


def run_profile(
    smu: SourceMeasureUnit,
    output_path: Path,
) -> None:

    validate_settings()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_file: TextIO | None = None

    # -------------------------------------------------------------------------
    # Energy integration states
    # -------------------------------------------------------------------------

    total_energy = 0.0

    total_previous_time: float | None = None
    total_previous_power: float | None = None

    previous_sample_time: float | None = None

    previous_set_voltage: float | None = None

    sample_index = 0


    # -------------------------------------------------------------------------
    # Connect Keithley
    # -------------------------------------------------------------------------

    smu.connect()

    try:

        smu.configure_voltage_measurement(
            WIRE_MODE
        )

        csv_file = output_path.open(
            "w",
            newline="",
            encoding="utf-8-sig",
        )

        writer = csv.DictWriter(
            csv_file,
            fieldnames=FIELDNAMES,
        )

        writer.writeheader()

        csv_file.flush()


        # ---------------------------------------------------------------------
        # Initial safe state
        # ---------------------------------------------------------------------

        smu.set_voltage(0.0)

        smu.output_on()


        # ---------------------------------------------------------------------
        # Experiment start time
        # ---------------------------------------------------------------------

        experiment_start = time.monotonic()

        local_start = datetime.now().astimezone()

        utc_start = datetime.now(timezone.utc)


        print()
        print("=" * 70)
        print("EXPERIMENT START")
        print("=" * 70)

        print(
            f"Experiment ID : {EXPERIMENT_ID}"
        )

        print(
            f"Sample ID     : {SAMPLE_ID}"
        )

        print(
            f"Wire mode     : {WIRE_MODE}-wire"
        )

        print(
            f"Current limit : {CURRENT_LIMIT:.6g} A"
        )

        print(
            f"Sample interval target : "
            f"{SAMPLE_INTERVAL_S:.6g} s"
        )

        print(
            f"Output CSV    : {output_path}"
        )

        print()

        print(
            "SYNC EVENT:"
        )

        print(
            "Experiment time origin established."
        )

        print(
            "t = 0 s"
        )

        print(
            f"PC local start : "
            f"{local_start.isoformat(timespec='milliseconds')}"
        )

        print(
            f"PC UTC start   : "
            f"{utc_start.isoformat(timespec='milliseconds')}"
        )

        print("=" * 70)
        print()


        # =====================================================================
        # Voltage steps
        # =====================================================================

        for step_index, entry in enumerate(
            PROFILE,
            start=1,
        ):

            (
                set_voltage,
                duration_s,
                cycle_index,
                step_note,
            ) = parse_profile_entry(entry)


            state = infer_state(
                previous_set_voltage,
                set_voltage,
            )


            print(
                f"Step "
                f"{step_index}/{len(PROFILE)}"
            )

            print(
                f"  Vset     : "
                f"{set_voltage:.6g} V"
            )

            print(
                f"  duration : "
                f"{duration_s:.6g} s"
            )

            print(
                f"  cycle    : "
                f"{cycle_index}"
            )

            print(
                f"  state    : "
                f"{state}"
            )

            if step_note:

                print(
                    f"  note     : "
                    f"{step_note}"
                )

            print()


            # -----------------------------------------------------------------
            # Apply voltage step
            # -----------------------------------------------------------------

            smu.set_voltage(
                set_voltage
            )

            step_start = time.monotonic()


            # -----------------------------------------------------------------
            # Step energy integration state
            # -----------------------------------------------------------------

            step_energy = 0.0

            step_previous_time: float | None = None

            step_previous_power: float | None = None


            # =================================================================
            # Sampling loop
            # =================================================================

            while True:

                loop_start = time.monotonic()

                step_elapsed = (
                    loop_start - step_start
                )

                if (
                    step_previous_time is not None
                    and step_elapsed >= duration_s
                ):

                    break


                # -------------------------------------------------------------
                # Keithley measurement
                # -------------------------------------------------------------

                measure_start = time.monotonic()

                measured = smu.measure()

                measure_end = time.monotonic()


                sample_time = measure_end

                sample_index += 1


                # -------------------------------------------------------------
                # Derived electrical values
                # -------------------------------------------------------------

                power = (
                    measured.voltage
                    * measured.current
                )


                voltage_error = (
                    measured.voltage
                    - set_voltage
                )


                if (
                    math.isfinite(
                        measured.resistance
                    )
                    and abs(
                        measured.resistance
                    ) > 1e-15
                ):

                    conductance = (
                        1.0
                        / measured.resistance
                    )

                else:

                    conductance = float("nan")


                current_limit_ratio = (
                    abs(measured.current)
                    / CURRENT_LIMIT
                )


                near_current_limit = int(
                    current_limit_ratio >= 0.98
                )


                # -------------------------------------------------------------
                # Total energy integration
                #
                # Trapezoidal integration
                # -------------------------------------------------------------

                if (
                    total_previous_time is not None
                    and total_previous_power is not None
                ):

                    dt_total = (
                        sample_time
                        - total_previous_time
                    )

                    total_energy += (
                        (
                            total_previous_power
                            + power
                        )
                        / 2.0
                        * dt_total
                    )


                # -------------------------------------------------------------
                # Step energy integration
                # -------------------------------------------------------------

                if (
                    step_previous_time is not None
                    and step_previous_power is not None
                ):

                    dt_step = (
                        sample_time
                        - step_previous_time
                    )

                    step_energy += (
                        (
                            step_previous_power
                            + power
                        )
                        / 2.0
                        * dt_step
                    )


                # -------------------------------------------------------------
                # Actual sampling interval
                # -------------------------------------------------------------

                if previous_sample_time is None:

                    dt_s = float("nan")

                else:

                    dt_s = (
                        sample_time
                        - previous_sample_time
                    )


                # -------------------------------------------------------------
                # Absolute timestamps
                # -------------------------------------------------------------

                now_local = (
                    datetime.now().astimezone()
                )

                now_utc = (
                    datetime.now(timezone.utc)
                )


                # -------------------------------------------------------------
                # Save CSV row
                # -------------------------------------------------------------

                writer.writerow(
                    {
                        # identification
                        "experiment_id":
                            EXPERIMENT_ID,

                        "sample_id":
                            SAMPLE_ID,

                        "operator":
                            OPERATOR,

                        "sample_index":
                            sample_index,


                        # time
                        "timestamp_local":
                            now_local.isoformat(
                                timespec="milliseconds"
                            ),

                        "timestamp_utc":
                            now_utc.isoformat(
                                timespec="milliseconds"
                            ),

                        "time_s":
                            sample_time
                            - experiment_start,

                        "dt_s":
                            dt_s,

                        "measurement_duration_s":
                            measure_end
                            - measure_start,


                        # step / cycle
                        "step_index":
                            step_index,

                        "cycle_index":
                            cycle_index,

                        "state":
                            state,

                        "step_note":
                            step_note,

                        "step_elapsed_s":
                            sample_time
                            - step_start,

                        "step_duration_s":
                            duration_s,


                        # measurement config
                        "wire_mode":
                            WIRE_MODE,


                        # electrical raw data
                        "set_voltage_V":
                            set_voltage,

                        "measured_voltage_V":
                            measured.voltage,

                        "voltage_error_V":
                            voltage_error,

                        "current_A":
                            measured.current,

                        "current_limit_A":
                            CURRENT_LIMIT,

                        "current_limit_ratio":
                            current_limit_ratio,

                        "near_current_limit":
                            near_current_limit,

                        "resistance_ohm":
                            measured.resistance,

                        "conductance_S":
                            conductance,

                        "power_W":
                            power,


                        # energy
                        "energy_J":
                            total_energy,

                        "step_energy_J":
                            step_energy,


                        # experiment metadata
                        "preload_g":
                            PRELOAD_G,

                        "initial_length_mm":
                            INITIAL_LENGTH_MM,

                        "ambient_temp_C":
                            AMBIENT_TEMP_C,

                        "ir_camera_id":
                            IR_CAMERA_ID,

                        "sony_camera_id":
                            SONY_CAMERA_ID,

                        "sync_note":
                            SYNC_NOTE,

                        "general_note":
                            GENERAL_NOTE,
                    }
                )

                # Immediately flush to reduce loss if experiment crashes.
                csv_file.flush()


                # -------------------------------------------------------------
                # Terminal output
                # -------------------------------------------------------------

                print(
                    f"t="
                    f"{sample_time - experiment_start:8.3f} s | "
                    f"Vset="
                    f"{set_voltage:8.4f} V | "
                    f"V="
                    f"{measured.voltage:8.4f} V | "
                    f"I="
                    f"{measured.current:10.6f} A | "
                    f"R="
                    f"{measured.resistance:10.4f} ohm | "
                    f"P="
                    f"{power:8.4f} W | "
                    f"E="
                    f"{total_energy:9.4f} J | "
                    f"stepE="
                    f"{step_energy:9.4f} J"
                )


                if near_current_limit:

                    print(
                        "WARNING: current is within "
                        "2% of CURRENT_LIMIT"
                    )


                # -------------------------------------------------------------
                # Update integration states
                # -------------------------------------------------------------

                total_previous_time = (
                    sample_time
                )

                total_previous_power = (
                    power
                )

                step_previous_time = (
                    sample_time
                )

                step_previous_power = (
                    power
                )

                previous_sample_time = (
                    sample_time
                )


                # -------------------------------------------------------------
                # Sampling timing control
                # -------------------------------------------------------------

                elapsed_this_loop = (
                    time.monotonic()
                    - loop_start
                )

                desired_sleep = (
                    SAMPLE_INTERVAL_S
                    - elapsed_this_loop
                )

                remaining_step_time = (
                    step_start
                    + duration_s
                    - time.monotonic()
                )

                sleep_s = min(
                    desired_sleep,
                    remaining_step_time,
                )

                if sleep_s > 0:

                    time.sleep(
                        sleep_s
                    )


            previous_set_voltage = (
                set_voltage
            )


    # =========================================================================
    # Safe shutdown
    # =========================================================================

    finally:

        if csv_file is not None:

            csv_file.close()


        try:

            smu.set_voltage(
                0.0
            )

        finally:

            try:

                smu.output_off()

            finally:

                smu.close()


# =============================================================================
# COMMAND LINE
# =============================================================================


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Run a manual DC voltage profile "
            "and save extended raw experimental data."
        )
    )


    parser.add_argument(
        "--dummy",
        action="store_true",
        help=(
            "Run without a Keithley 2450 "
            "for logic/CSV testing"
        ),
    )


    parser.add_argument(
        "--resource",
        default=RESOURCE_NAME,
    )


    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )


    args = parser.parse_args()


    if args.output is None:

        output_path = (
            default_output_path()
        )

    else:

        output_path = (
            args.output
        )


    if args.dummy:

        smu = (
            DummySourceMeasureUnit()
        )

    else:

        smu = (
            Keithley2450(
                args.resource,
                CURRENT_LIMIT,
            )
        )


    mode_name = (
        "DUMMY"
        if args.dummy
        else "KEITHLEY 2450"
    )


    print()

    print(
        f"Starting {mode_name} mode"
    )

    print(
        f"CSV output: {output_path}"
    )

    print()


    try:

        run_profile(
            smu,
            output_path,
        )


    except KeyboardInterrupt:

        print()

        print(
            "Interrupted by user."
        )

        print(
            "Safe shutdown completed "
            "(0 V -> output OFF)."
        )


    except Exception as exc:

        print()

        print(
            f"Experiment failed: {exc}"
        )

        raise


    else:

        print()

        print(
            "Profile complete."
        )

        print(
            "Safe shutdown completed "
            "(0 V -> output OFF)."
        )


if __name__ == "__main__":
    main()

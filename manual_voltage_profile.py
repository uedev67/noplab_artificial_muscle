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
    (value, duration_s)

Extended form:
    (value, duration_s, cycle_index, note)

``value`` is interpreted according to CONTROL_MODE (SI units):
    "voltage" -> set voltage V   (default, original behaviour)
    "power"   -> target power W  (voltage source + software loop)
    "current" -> set current A   (current source)

Examples:
    (10.0, 10.0)
    (10.0, 10.0, 3, "cycle 3 heating")
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import TextIO

from voltage_profiles import PROFILES

from control_modes import (
    MODES,
    PowerController,
    ProbeController,
    SafetyAbort,
    SafetyMonitor,
    normalize_probe,
    profile_violations,
    resistance_is_valid,
    validate_limits,
    validate_probe,
)
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

# voltage_profiles.py에 저장된 프로파일 이름을 선택하세요.
# Preserve the locally customized experiment profile.
PROFILES["local_custom"] = [
    (0.0, 5.0), 
    (10.0, 6.0), (0.0, 6.0),
    (10.0, 6.0), (0.0, 6.0),
    (10.0, 6.0), (0.0, 6.0),
    (10.0, 6.0), (0.0, 6.0),
    (10.0, 6.0), (0.0, 6.0),
    (10.0, 6.0), (0.0, 6.0),

    
]
PROFILE_NAME = "local_custom"

if PROFILE_NAME not in PROFILES:
    raise ValueError(
        f"Unknown PROFILE_NAME: {PROFILE_NAME!r}. "
        f"Available profiles: {', '.join(PROFILES)}"
    )
PROFILE = list(PROFILES[PROFILE_NAME])


# ---------------------------------------------------------------------------
# Keithley settings
# ---------------------------------------------------------------------------

WIRE_MODE = 4  # 2 or 4

CURRENT_LIMIT = 0.35  # A
SAMPLE_INTERVAL_S = 0.1

RESOURCE_NAME = "USB0::0x05E6::0x2450::04495764::INSTR"

# ---------------------------------------------------------------------------
# Control mode and safety limits
#
# CONTROL_MODE:
#   "voltage" : PROFILE values are set voltages (V)  - original behaviour
#   "power"   : PROFILE values are target powers (W) - voltage source + software loop
#   "current" : PROFILE values are set currents (A)  - current source
#
# Hardware compliance: CURRENT_LIMIT (voltage / power mode),
#                      VOLTAGE_LIMIT (current mode).
#
# SAFETY_LIMITS: software limits checked before start and on every sample.
#   None keeps the original CLI behaviour (voltage mode only; hardware
#   compliance only).  Power / current mode always require a dict, e.g.
#       voltage: {"V_max": 12, "P_max": 1.5, "R_jump_pct": 5}
#       current: {"I_max": 0.16, "P_max": 1.5, "R_jump_pct": 5}
#       power  : {"P_max": 1.5, "V_max": 12, "R_jump_pct": 5,
#                 "probe_V": 0.1, "max_step_V": 1.0, "max_R_failures": 3}
# ---------------------------------------------------------------------------

CONTROL_MODE = "voltage"

VOLTAGE_LIMIT = 12.0  # V, compliance used in current mode

SAFETY_LIMITS = None

# ---------------------------------------------------------------------------
# Probe current in 0-output steps (냉각 중 측정 전류)
#
# PROBE = None keeps the original behaviour (0 V / 0 A in every 0 step).
# With {"enabled": True, ...} every 0-value step sources a small measurement
# current instead of 0 so R is recorded while the sample cools:
#   voltage / power mode: V = I_probe * R_latest, clamped at V_probe_max
#   current mode        : I_probe
#   PROBE = {"enabled": True, "I_probe_A": 0.001, "V_probe_max_V": 0.2,
#            "R_jump_skip_samples": 2}
# Requires SAFETY_LIMITS.
# ---------------------------------------------------------------------------

PROBE = None

# 아두이노 마커 시스템 스위치(False이면 아두이노 미사용)
use_serial_marker = True
SERIAL_MARKER_PORT = "COM9"  # Arduino UNO R4 WiFi; update if the assigned port changes.
SERIAL_MARKER_BAUDRATE = 9600  # Must match Arduino Serial.begin(...).
SERIAL_MARKER_READY_DELAY_S = 2.0  # Allow Arduino to reboot after opening USB.
SERIAL_MARKER_RETRY_DELAY_S = 1.0
# For Arduino readStringUntil('\n'); change if the sketch uses another framing.
SERIAL_MARKER_COMMAND = b"start\n"


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

    # -------------------------------------------------------------------------
    # Control mode / safety (appended - the legacy column order above is kept
    # unchanged so existing analysis scripts keep working)
    # -------------------------------------------------------------------------

    "control_mode",

    "set_value",

    "set_unit",

    "set_current_A",

    "target_power_W",

    "command_voltage_V",

    "limit_settings",

    "abort_reason",

    # -------------------------------------------------------------------------
    # Probe current / conditional cooling / R_ref (appended)
    # -------------------------------------------------------------------------

    "phase",                      # heating / cooling / rref / idle

    "cycle_number",               # +1 whenever heating starts

    "probe_active",               # 1 while the probe current is sourced

    "R_ref_ohm",

    "R_cold_before_heating_ohm",  # mean R of the last 1 s before heating

    "cooling_end_reason",         # fixed / condition / timeout (last row)

    "cooling_duration_s",
]

# Window (s) of the cold-resistance average written on heating rows.
R_COLD_WINDOW_S = 1.0


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
    previous_set_value: float | None,
    set_value: float,
) -> str:
    """Infer an input-state label from the step value change.

    Works for every control mode: the step value (voltage, power or
    current) is compared with the previous step value.  The comparison is
    signed, exactly as in the original voltage-only version, so existing
    voltage-mode CSV labels do not change.

    IMPORTANT:
    이 state는 실제 thermal state가 아니라
    '입력 명령 변화 기준' 라벨이다.

    실제 heating / cooling 여부는 나중에 IR 데이터의
    dT/dt를 이용해 재분류하는 것이 더 정확하다.
    """

    if previous_set_value is None:

        if set_value == 0.0:
            return "initial_off"

        return "initial"

    if set_value > previous_set_value:
        return "heating"

    if set_value < previous_set_value:
        return "cooling"

    if set_value == 0.0:
        return "off_hold"

    return "hold"


# =============================================================================
# VALIDATION
# =============================================================================


def validate_settings() -> None:

    if use_serial_marker and not SERIAL_MARKER_PORT.strip():
        raise ValueError("Set SERIAL_MARKER_PORT to the Arduino COM port")

    if WIRE_MODE not in (2, 4):

        raise ValueError(
            "WIRE_MODE must be 2 or 4"
        )

    if not math.isfinite(CURRENT_LIMIT) or CURRENT_LIMIT <= 0:

        raise ValueError(
            "CURRENT_LIMIT must be positive"
        )

    if CONTROL_MODE not in MODES:

        raise ValueError(
            f"CONTROL_MODE must be one of {', '.join(MODES)}"
        )

    if CONTROL_MODE == "current" and (
        not math.isfinite(VOLTAGE_LIMIT) or VOLTAGE_LIMIT <= 0
    ):

        raise ValueError(
            "VOLTAGE_LIMIT must be positive"
        )

    if SAFETY_LIMITS is None:

        if CONTROL_MODE != "voltage":

            # Power / current mode are never run without software limits.
            raise ValueError(
                f"CONTROL_MODE={CONTROL_MODE!r} requires SAFETY_LIMITS"
            )

    else:

        validate_limits(CONTROL_MODE, SAFETY_LIMITS)

        hardware = {"I_lim": CURRENT_LIMIT, "V_lim": VOLTAGE_LIMIT}

        for key, value in hardware.items():

            if key in SAFETY_LIMITS and SAFETY_LIMITS[key] != value:

                raise ValueError(
                    f"SAFETY_LIMITS[{key!r}]={SAFETY_LIMITS[key]} does not match "
                    f"the hardware limit {value}"
                )

    if not math.isfinite(SAMPLE_INTERVAL_S) or SAMPLE_INTERVAL_S <= 0:

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

    if SAFETY_LIMITS is not None:

        # Protection level 1: refuse to start if any step breaks a limit.
        violations = profile_violations(
            CONTROL_MODE,
            [parse_profile_entry(entry)[0] for entry in PROFILE],
            SAFETY_LIMITS,
        )

        if violations:

            raise ValueError(
                "PROFILE exceeds safety limits:\n" + "\n".join(violations)
            )

    probe = normalize_probe(PROBE)

    if probe is not None and probe["enabled"]:

        if SAFETY_LIMITS is None:

            raise ValueError("PROBE requires SAFETY_LIMITS")

        validate_probe(
            CONTROL_MODE,
            probe,
            SAFETY_LIMITS,
            current_limit=CURRENT_LIMIT,
            voltage_limit=VOLTAGE_LIMIT,
        )


# =============================================================================
# MAIN EXPERIMENT
# =============================================================================


def connect_serial_marker():
    """Open the configured port, with two retries before aborting.

    This checks USB serial access, not Arduino firmware readiness (which would
    require a reply from the sketch). No start command is sent here.
    """
    import serial

    for attempt in range(1, 4):
        try:
            connection = serial.Serial(
                port=SERIAL_MARKER_PORT,
                baudrate=SERIAL_MARKER_BAUDRATE,
                timeout=1.0,
                write_timeout=1.0,
            )
        except (serial.SerialException, OSError) as exc:
            print(f"Arduino connection attempt {attempt}/3 failed: {exc}")
            if attempt == 3:
                raise RuntimeError(
                    "Arduino connection failed after 3 attempts; experiment aborted."
                ) from exc
            time.sleep(SERIAL_MARKER_RETRY_DELAY_S)
            continue

        try:
            # Opening the port may reset the Arduino; wait before the profile.
            time.sleep(SERIAL_MARKER_READY_DELAY_S)
            print(f"Arduino serial port connected: {SERIAL_MARKER_PORT}")
        except BaseException:
            connection.close()
            raise
        return connection


def _recent_mean(samples, window_s: float) -> float | str:
    """Mean R of (time, R) samples within *window_s* of the last one ("" if none)."""

    if not samples:
        return ""

    last_time = samples[-1][0]

    values = [r for t, r in samples if t >= last_time - window_s]

    return sum(values) / len(values)


def _emergency_output_off(smu: SourceMeasureUnit) -> str:
    """Zero the source and switch the output OFF immediately.

    Used when a software safety limit trips.  Communication errors are
    collected instead of raised so that the abort reason can still be written
    to the CSV; ``run_profile``'s ``finally`` block retries the shutdown.
    """

    errors = []

    try:
        smu.set_source_zero()
    except Exception as exc:  # noqa: BLE001 - must still try output OFF
        errors.append(f"0 출력 실패: {exc}")

    try:
        smu.output_off()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"출력 OFF 실패: {exc}")

    return "; ".join(errors)


def run_profile(
    smu: SourceMeasureUnit,
    output_path: Path,
    stop_event: Event | None = None,
    exclusive_output: bool = False,
) -> None:
    """Run PROFILE in CONTROL_MODE and log every sample to *output_path*.

    Raises SafetyAbort (after output 0 + OFF and a CSV row with
    ``abort_reason``) when a software limit in SAFETY_LIMITS is exceeded.
    """

    validate_settings()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    mode = CONTROL_MODE

    set_unit = MODES[mode].si_unit

    # Protection level 3: per-sample software checks (None = legacy CLI).
    monitor = (
        SafetyMonitor(mode, SAFETY_LIMITS)
        if SAFETY_LIMITS is not None
        else None
    )

    power_controller = (
        PowerController(SAFETY_LIMITS)
        if mode == "power"
        else None
    )

    hardware_limit = (
        {"V_lim": VOLTAGE_LIMIT}
        if mode == "current"
        else {"I_lim": CURRENT_LIMIT}
    )

    probe = normalize_probe(PROBE)

    probe_enabled = probe is not None and probe["enabled"]

    voltage_source = mode != "current"

    probe_controller = (
        ProbeController(probe["I_probe_A"], probe["V_probe_max_V"])
        if probe is not None
        else None
    )

    # R-jump check exclusion after a measurement-current regime change.
    jump_skip_samples = probe["R_jump_skip_samples"] if probe_enabled else 0

    extra_settings = {}

    if probe_enabled:
        extra_settings["probe"] = probe

    limit_settings = json.dumps(
        {"mode": mode, **hardware_limit, **(SAFETY_LIMITS or {}), **extra_settings},
        ensure_ascii=False,
        sort_keys=True,
    )

    csv_file: TextIO | None = None
    serial_marker = None
    marker_sent = False

    # -------------------------------------------------------------------------
    # Energy integration states
    # -------------------------------------------------------------------------

    total_energy = 0.0

    total_previous_time: float | None = None
    total_previous_power: float | None = None

    previous_sample_time: float | None = None

    previous_set_value: float | None = None

    sample_index = 0

    # Phase / cycle / probe bookkeeping
    seen_heating = False
    previous_was_heating = False
    previous_regime: str | None = None
    cycle_number = 0
    last_valid_resistance: float | None = None
    # (time, R) of valid samples since the last heating -> R_cold_before_heating
    cold_samples: deque = deque()
    r_cold_before_heating: float | str = ""
    r_ref_value: float | None = None


    # -------------------------------------------------------------------------
    # Connect Keithley
    # -------------------------------------------------------------------------

    if stop_event is not None and stop_event.is_set():
        return
    try:
        smu.connect()

        if use_serial_marker:
            serial_marker = connect_serial_marker()

        if stop_event is not None and stop_event.is_set():
            return

        # Protection level 2: hardware compliance.
        # CURRENT_LIMIT (ILIM) is set by Keithley2450.connect(); current mode
        # switches the source function here, while the output is still OFF.
        if mode == "current":
            smu.configure_current_source(
                WIRE_MODE,
                VOLTAGE_LIMIT,
            )
        else:
            smu.configure_voltage_measurement(
                WIRE_MODE
            )

        csv_file = output_path.open(
            "x" if exclusive_output else "w",
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

        smu.set_source_zero()

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

        print(f"Experiment ID : {EXPERIMENT_ID}")
        print(f"Sample ID     : {SAMPLE_ID}")
        print(f"Control mode  : {mode}")
        print(f"Wire mode     : {WIRE_MODE}-wire")

        if mode == "current":
            print(f"Voltage limit : {VOLTAGE_LIMIT:.6g} V")
        else:
            print(f"Current limit : {CURRENT_LIMIT:.6g} A")

        if SAFETY_LIMITS is not None:
            print(f"SW limits     : {limit_settings}")

        print(
            f"Sample interval target : "
            f"{SAMPLE_INTERVAL_S:.6g} s"
        )

        if probe_enabled:
            print(
                f"Probe current : {probe['I_probe_A'] * 1e3:.6g} mA in 0 steps "
                f"(V_probe_max {probe['V_probe_max_V']:.6g} V)"
            )

        print(f"Output CSV    : {output_path}")

        print()
        print("SYNC EVENT:")
        print("Experiment time origin established.")
        print("t = 0 s")
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
        # Profile steps
        # =====================================================================

        for step_index, entry in enumerate(
            PROFILE,
            start=1,
        ):
            if stop_event is not None and stop_event.is_set():
                return

            (
                set_value,
                duration_s,
                cycle_index,
                step_note,
            ) = parse_profile_entry(entry)


            state = infer_state(
                previous_set_value,
                set_value,
            )


            # Measurement regime of this step:
            #   heat  : value != 0 (heating / driving output)
            #   probe : value == 0 with the probe current
            #   zero  : value == 0, true 0 output (original behaviour)
            if set_value != 0.0:
                regime = "heat"
            elif probe_enabled:
                regime = "probe"
            else:
                regime = "zero"

            if regime == "heat":
                phase = "heating"
            elif seen_heating:
                phase = "cooling"
            else:
                phase = "idle"

            if regime == "heat":

                if not previous_was_heating:

                    cycle_number += 1

                    r_cold_before_heating = _recent_mean(
                        cold_samples, R_COLD_WINDOW_S
                    )

                seen_heating = True

            elif previous_was_heating:

                cold_samples.clear()

            previous_was_heating = regime == "heat"

            if (
                monitor is not None
                and jump_skip_samples
                and previous_regime is not None
                and regime != previous_regime
            ):
                # Auto range may switch between heating and probe current.
                monitor.skip_jump_check(jump_skip_samples)

            previous_regime = regime

            probe_v_max = (
                probe["V_probe_max_V"] if regime == "probe" else None
            )


            print(f"Step {step_index}/{len(PROFILE)}")

            if mode == "voltage":
                print(f"  Vset     : {set_value:.6g} V")
            elif mode == "power":
                print(f"  Pset     : {set_value:.6g} W")
            else:
                print(f"  Iset     : {set_value * 1e3:.6g} mA")

            print(f"  duration : {duration_s:.6g} s")
            print(f"  cycle    : {cycle_index}")
            print(f"  state    : {state}")

            if step_note:
                print(f"  note     : {step_note}")

            print()


            # -----------------------------------------------------------------
            # Apply step
            # -----------------------------------------------------------------

            command_voltage: float | None

            if regime == "probe":

                if voltage_source:

                    if power_controller is not None:
                        power_controller.start_step(0.0, keep_resistance=True)

                    command_voltage = probe_controller.start(last_valid_resistance)
                    smu.set_voltage(command_voltage)

                    if power_controller is not None:
                        power_controller.command = command_voltage

                    print(
                        f"  probe    : V = I_probe x R -> "
                        f"{command_voltage:.4g} V"
                    )

                else:

                    command_voltage = None
                    smu.set_current(probe["I_probe_A"])

                    print(
                        f"  probe    : {probe['I_probe_A'] * 1e3:.4g} mA"
                    )

            elif mode == "voltage":

                command_voltage = set_value
                smu.set_voltage(set_value)

            elif mode == "current":

                command_voltage = None
                smu.set_current(set_value)

            else:

                probing = (
                    set_value > 0.0
                    and power_controller.last_resistance is None
                )
                command_voltage = power_controller.start_step(set_value)
                smu.set_voltage(command_voltage)

                if probing:
                    print(
                        f"  power mode: no valid R yet -> probe "
                        f"{command_voltage:.4g} V"
                    )

            saturation_warned = False

            if (
                serial_marker is not None
                and not marker_sent
            ):
                # Mark the first profile command, including 0 output.
                written = serial_marker.write(SERIAL_MARKER_COMMAND)
                if written != len(SERIAL_MARKER_COMMAND):
                    raise IOError("Incomplete Arduino start command")
                serial_marker.flush()
                marker_sent = True
                print(
                    f"Arduino marker TX: {SERIAL_MARKER_COMMAND!r} "
                    f"on {SERIAL_MARKER_PORT}, step {step_index}, "
                    f"t={time.monotonic() - experiment_start:.3f} s"
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
                if stop_event is not None and stop_event.is_set():
                    return

                loop_start = time.monotonic()

                # The step ends after the sample that is decided to be its
                # last one (see "Step end decision" below), so the last row
                # of every step can carry the end reason.


                # -------------------------------------------------------------
                # Keithley measurement
                # -------------------------------------------------------------

                measure_start = time.monotonic()

                measured = smu.measure()

                measure_end = time.monotonic()


                # -------------------------------------------------------------
                # Safety checks (immediately after the measurement)
                # -------------------------------------------------------------

                abort_reason = ""

                if monitor is not None:
                    abort_reason = monitor.check(
                        set_value,
                        command_voltage,
                        measured,
                        probe_v_max,
                    )

                next_command = command_voltage

                if not abort_reason:

                    if regime == "probe":

                        if voltage_source:
                            next_command = probe_controller.update(measured)

                            if power_controller is not None:
                                power_controller.observe(
                                    measured, command_voltage
                                )

                    elif power_controller is not None:
                        next_command, abort_reason = power_controller.update(
                            measured
                        )

                r_valid = resistance_is_valid(measured)

                if r_valid:
                    last_valid_resistance = measured.resistance

                    if regime != "heat":
                        cold_samples.append((measure_end, measured.resistance))


                # -------------------------------------------------------------
                # Step end decision
                # -------------------------------------------------------------

                cooling_end_reason = ""

                cooling_duration: float | str = ""

                next_sample_start = max(
                    loop_start + SAMPLE_INTERVAL_S,
                    time.monotonic(),
                )

                last_sample = next_sample_start >= step_start + duration_s

                if last_sample and phase in ("cooling", "idle"):
                    cooling_end_reason = "fixed"
                    cooling_duration = duration_s

                if abort_reason:
                    communication_error = _emergency_output_off(smu)
                    if communication_error:
                        abort_reason += f" ({communication_error})"


                sample_time = measure_end

                sample_index += 1


                # -------------------------------------------------------------
                # Derived electrical values
                # -------------------------------------------------------------

                power = (
                    measured.voltage
                    * measured.current
                )


                if command_voltage is None:

                    voltage_error = ""

                else:

                    voltage_error = (
                        measured.voltage
                        - command_voltage
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


                if mode == "current":

                    # Compliance here is the voltage limit; the current-limit
                    # columns stay empty and near_current_limit flags V_lim.
                    current_limit_value = ""
                    current_limit_ratio = ""
                    near_current_limit = int(
                        abs(measured.voltage) / VOLTAGE_LIMIT >= 0.98
                    )

                else:

                    current_limit_value = CURRENT_LIMIT
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
                        "experiment_id": EXPERIMENT_ID,
                        "sample_id": SAMPLE_ID,
                        "operator": OPERATOR,
                        "sample_index": sample_index,

                        # time
                        "timestamp_local": now_local.isoformat(
                            timespec="milliseconds"
                        ),
                        "timestamp_utc": now_utc.isoformat(
                            timespec="milliseconds"
                        ),
                        "time_s": sample_time - experiment_start,
                        "dt_s": dt_s,
                        "measurement_duration_s": measure_end - measure_start,

                        # step / cycle
                        "step_index": step_index,
                        "cycle_index": cycle_index,
                        "state": state,
                        "step_note": step_note,
                        "step_elapsed_s": sample_time - step_start,
                        "step_duration_s": duration_s,

                        # measurement config
                        "wire_mode": WIRE_MODE,

                        # electrical raw data
                        # voltage mode: set voltage / power mode: command
                        # voltage of this sample / current mode: empty
                        "set_voltage_V": (
                            "" if command_voltage is None else command_voltage
                        ),
                        "measured_voltage_V": measured.voltage,
                        "voltage_error_V": voltage_error,
                        "current_A": measured.current,
                        "current_limit_A": current_limit_value,
                        "current_limit_ratio": current_limit_ratio,
                        "near_current_limit": near_current_limit,
                        "resistance_ohm": measured.resistance,
                        "conductance_S": conductance,
                        "power_W": power,

                        # energy
                        "energy_J": total_energy,
                        "step_energy_J": step_energy,

                        # experiment metadata
                        "preload_g": PRELOAD_G,
                        "initial_length_mm": INITIAL_LENGTH_MM,
                        "ambient_temp_C": AMBIENT_TEMP_C,
                        "ir_camera_id": IR_CAMERA_ID,
                        "sony_camera_id": SONY_CAMERA_ID,
                        "sync_note": SYNC_NOTE,
                        "general_note": GENERAL_NOTE,

                        # control mode / safety
                        "control_mode": mode,
                        "set_value": set_value,
                        "set_unit": set_unit,
                        "set_current_A": (
                            set_value if mode == "current" else ""
                        ),
                        "target_power_W": (
                            set_value if mode == "power" else ""
                        ),
                        "command_voltage_V": (
                            "" if command_voltage is None else command_voltage
                        ),
                        "limit_settings": limit_settings,
                        "abort_reason": abort_reason,

                        # probe / cooling / R_ref
                        "phase": phase,
                        "cycle_number": cycle_number,
                        "probe_active": int(regime == "probe"),
                        "R_ref_ohm": (
                            "" if r_ref_value is None else r_ref_value
                        ),
                        "R_cold_before_heating_ohm": (
                            r_cold_before_heating if regime == "heat" else ""
                        ),
                        "cooling_end_reason": cooling_end_reason,
                        "cooling_duration_s": cooling_duration,
                    }
                )

                # Immediately flush to reduce loss if experiment crashes.
                csv_file.flush()


                # -------------------------------------------------------------
                # Terminal output
                # -------------------------------------------------------------

                if regime == "probe":
                    setpoint = (
                        f"probe Vcmd={command_voltage:8.4f} V | "
                        if voltage_source
                        else f"probe Iset={probe['I_probe_A'] * 1e3:8.3f} mA | "
                    )
                elif mode == "voltage":
                    setpoint = f"Vset={set_value:8.4f} V | "
                elif mode == "power":
                    setpoint = (
                        f"Pset={set_value:8.4f} W | "
                        f"Vcmd={command_voltage:8.4f} V | "
                    )
                else:
                    setpoint = f"Iset={set_value * 1e3:8.3f} mA | "

                print(
                    f"t="
                    f"{sample_time - experiment_start:8.3f} s | "
                    f"{setpoint}"
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

                    if mode == "current":
                        print(
                            "WARNING: voltage is within "
                            "2% of VOLTAGE_LIMIT"
                        )
                    else:
                        print(
                            "WARNING: current is within "
                            "2% of CURRENT_LIMIT"
                        )


                if abort_reason:

                    print()
                    print(f"SAFETY ABORT: {abort_reason}")
                    print("Output set to 0 and switched OFF.")

                    raise SafetyAbort(abort_reason)


                # -------------------------------------------------------------
                # Power mode / probe: apply the next command voltage
                # -------------------------------------------------------------

                if power_controller is not None and regime == "heat":

                    if power_controller.saturated and not saturation_warned:
                        print(
                            "WARNING: power mode command voltage is clamped "
                            "at V_max; target power is not reached"
                        )
                        saturation_warned = True

                if (
                    command_voltage is not None
                    and next_command != command_voltage
                ):
                    smu.set_voltage(next_command)
                    command_voltage = next_command


                # -------------------------------------------------------------
                # Update integration states
                # -------------------------------------------------------------

                total_previous_time = sample_time
                total_previous_power = power
                step_previous_time = sample_time
                step_previous_power = power
                previous_sample_time = sample_time


                # -------------------------------------------------------------
                # Sampling timing control
                # -------------------------------------------------------------

                if last_sample:

                    # Fixed duration: hold the output until the step ends.
                    sleep_s = step_start + duration_s - time.monotonic()

                else:

                    sleep_s = SAMPLE_INTERVAL_S - (time.monotonic() - loop_start)

                if sleep_s > 0:
                    if stop_event is None:
                        time.sleep(sleep_s)
                    elif stop_event.wait(sleep_s):
                        return

                if last_sample:
                    break


            previous_set_value = (
                set_value
            )


    # =========================================================================
    # Safe shutdown
    # =========================================================================

    finally:

        if csv_file is not None:

            csv_file.close()


        try:

            # 0 V in voltage / power mode, 0 A in current mode.
            smu.set_source_zero()

        finally:

            try:

                smu.output_off()

            finally:

                try:
                    smu.close()
                finally:
                    if serial_marker is not None:
                        serial_marker.close()


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
            DummySourceMeasureUnit(
                current_limit=CURRENT_LIMIT,
            )
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

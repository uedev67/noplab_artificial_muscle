"""Control modes (voltage / power / current), safety limits and the power loop.

This module has no GUI or instrument dependency so it can be unit-tested and
shared by the GUI and the CLI recorder.

All values handled here are SI units (V, W, A).  The GUI converts mA <-> A.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# =============================================================================
# Mode definitions
# =============================================================================


@dataclass(frozen=True)
class ModeInfo:
    key: str
    label: str          # button text
    quantity: str       # table header word
    unit: str           # GUI unit
    si_unit: str        # unit stored in profiles / CSV
    scale: float        # GUI value * scale = SI value


MODES = {
    "voltage": ModeInfo("voltage", "전압 모드", "전압", "V", "V", 1.0),
    "power": ModeInfo("power", "전력 모드", "전력", "W", "W", 1.0),
    "current": ModeInfo("current", "전류 모드", "전류", "mA", "A", 1e-3),
}

MODE_KEYS = tuple(MODES)


def mode_info(mode: str) -> ModeInfo:
    try:
        return MODES[mode]
    except KeyError:
        raise ValueError(
            f"Unknown control mode: {mode!r} (choose one of {', '.join(MODES)})"
        ) from None


# =============================================================================
# Safety limit definitions
# =============================================================================


@dataclass(frozen=True)
class LimitField:
    key: str
    label: str
    unit: str           # GUI unit
    scale: float = 1.0  # GUI value * scale = stored value
    integer: bool = False
    hardware: bool = False  # instrument compliance (CURRENT_LIMIT / VOLTAGE_LIMIT)


I_LIM_FIELD = LimitField("I_lim", "전류 제한 I_lim · 장비 compliance", "A", hardware=True)

LIMIT_FIELDS = {
    "voltage": (
        I_LIM_FIELD,
        LimitField("V_max", "설정 전압 상한 V_max", "V"),
        LimitField("P_max", "측정 전력 상한 P_max", "W"),
    ),
    "current": (
        LimitField("V_lim", "전압 제한 V_lim · 장비 compliance", "V", hardware=True),
        LimitField("I_max", "설정 전류 상한 I_max", "mA", 1e-3),
        LimitField("P_max", "측정 전력 상한 P_max", "W"),
    ),
    "power": (
        I_LIM_FIELD,
        LimitField("P_max", "목표 전력 상한 P_max (측정 ≤ ×1.1)", "W"),
        LimitField("V_max", "명령 전압 상한 V_max", "V"),
    ),
    "common": (
        LimitField("R_jump_pct", "R 급변 한계 (샘플 간)", "%"),
    ),
    "power_control": (
        LimitField("probe_V", "탐침 전압", "V"),
        LimitField("max_step_V", "샘플당 최대 전압 증가", "V"),
        LimitField("max_R_failures", "R 연속 실패 허용", "회", integer=True),
    ),
}

DEFAULT_LIMITS = {
    "voltage": {"I_lim": 0.16, "V_max": 12.0, "P_max": 1.5},
    "current": {"V_lim": 12.0, "I_max": 0.16, "P_max": 1.5},
    "power": {"I_lim": 0.16, "P_max": 1.5, "V_max": 12.0},
    "common": {"R_jump_pct": 5.0},
    "power_control": {"probe_V": 0.1, "max_step_V": 1.0, "max_R_failures": 3},
}

# Keithley 2450 source ranges (front-panel spec: ±1.05 A, ±210 V).
HARDWARE_MAX_CURRENT_A = 1.05
HARDWARE_MAX_VOLTAGE_V = 210.0

# Measured power may exceed the power-mode target ceiling by this factor
# before the run is aborted (control error / noise margin).
POWER_MODE_MEASURED_MARGIN = 1.1

# A resistance reading is only trusted when at least this much current flows.
# At 0 V the instrument returns offset/noise current and R = V/I is meaningless.
# 0.1 mA keeps a 0.1 V probe valid up to 1 kOhm.  Adjust after checking the
# real instrument's noise floor (실장비 확인 필요).
R_VALID_MIN_CURRENT_A = 1e-4


def groups_for_mode(mode: str) -> tuple[str, ...]:
    """Limit groups used by a mode (in GUI / profile order)."""
    mode_info(mode)
    groups = (mode, "common")
    if mode == "power":
        groups += ("power_control",)
    return groups


def runtime_limits(mode: str, limits: dict) -> dict:
    """Flatten the groups used by *mode* into one dict for the recorder."""
    flat = {}
    for group in groups_for_mode(mode):
        flat.update(limits[group])
    return flat


def validate_limits(mode: str, flat: dict) -> None:
    """Raise ValueError if the flat limit dict for *mode* is unusable."""

    def require(key):
        if key not in flat:
            raise ValueError(f"안전 상한 '{key}' 값이 없습니다.")
        value = flat[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"안전 상한 '{key}' 값이 숫자가 아닙니다.")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"안전 상한 '{key}'은(는) 0보다 큰 유한한 숫자여야 합니다.")
        return value

    # Hardware compliance values travel separately (CURRENT_LIMIT /
    # VOLTAGE_LIMIT) and are only checked here when present.
    for group in groups_for_mode(mode):
        for field in LIMIT_FIELDS[group]:
            if not field.hardware or field.key in flat:
                require(field.key)

    if "I_lim" in flat and require("I_lim") > HARDWARE_MAX_CURRENT_A:
        raise ValueError(f"I_lim은 장비 최대 {HARDWARE_MAX_CURRENT_A} A 이하여야 합니다.")
    if "V_lim" in flat and require("V_lim") > HARDWARE_MAX_VOLTAGE_V:
        raise ValueError(f"V_lim은 장비 최대 {HARDWARE_MAX_VOLTAGE_V} V 이하여야 합니다.")
    if mode == "power":
        if require("probe_V") > require("V_max"):
            raise ValueError("탐침 전압은 명령 전압 상한 V_max 이하여야 합니다.")
        failures = flat["max_R_failures"]
        if float(failures) != int(failures) or int(failures) < 1:
            raise ValueError("R 연속 실패 허용 횟수는 1 이상의 정수여야 합니다.")


def profile_violations(mode: str, values, flat: dict) -> list[str]:
    """Pre-start check: one message per profile row that breaks a limit.

    *values* are SI step values in profile order (row numbers start at 1).
    """
    messages = []
    for row, value in enumerate(values, 1):
        if mode == "voltage":
            if abs(value) > flat["V_max"]:
                messages.append(
                    f"{row}행: 설정 전압 {value:g} V > V_max {flat['V_max']:g} V"
                )
        elif mode == "current":
            if abs(value) > flat["I_max"]:
                messages.append(
                    f"{row}행: 설정 전류 {value * 1e3:g} mA > "
                    f"I_max {flat['I_max'] * 1e3:g} mA"
                )
        elif mode == "power":
            if value < 0:
                messages.append(f"{row}행: 목표 전력은 음수일 수 없습니다 ({value:g} W)")
            elif value > flat["P_max"]:
                messages.append(
                    f"{row}행: 목표 전력 {value:g} W > P_max {flat['P_max']:g} W"
                )
        else:
            mode_info(mode)
    return messages


# =============================================================================
# Runtime checks
# =============================================================================


class SafetyAbort(RuntimeError):
    """Raised after the output was switched off because a limit was exceeded."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def resistance_is_valid(measured) -> bool:
    """True when R = V/I from this sample can be trusted."""
    return (
        math.isfinite(measured.voltage)
        and math.isfinite(measured.current)
        and abs(measured.current) >= R_VALID_MIN_CURRENT_A
        and math.isfinite(measured.resistance)
        and measured.resistance > 0
    )


class SafetyMonitor:
    """Per-sample software limit checks.  ``check`` returns an abort reason."""

    def __init__(self, mode: str, flat: dict):
        mode_info(mode)
        self.mode = mode
        self.limits = flat
        self.previous_resistance: float | None = None
        self.jump_skip = 0

    def skip_jump_check(self, samples: int) -> None:
        """Exclude the next *samples* samples from the R-jump check only.

        Used right after the measurement current regime changes (heating
        current <-> probe current), where the instrument's auto range may
        switch and R can glitch.  Every other limit is still checked.
        """
        self.jump_skip = max(int(samples), 0)
        self.previous_resistance = None

    def check(self, set_value: float, command_voltage: float | None, measured,
              probe_v_max: float | None = None) -> str:
        """Return an abort reason ("" if every limit holds).

        *probe_v_max* is given while the probe current is being sourced in a
        0-output step: the probe command voltage (voltage source) or the
        measured voltage (current source) must stay at or below it.
        """
        limits = self.limits
        if not (math.isfinite(measured.voltage) and math.isfinite(measured.current)):
            return (
                f"측정값이 유한하지 않음 (V={measured.voltage}, I={measured.current})"
            )
        power = abs(measured.voltage * measured.current)

        if probe_v_max is not None:
            if command_voltage is not None:
                if command_voltage > probe_v_max + 1e-9:
                    return (
                        f"측정 전류 구간 명령 전압 {command_voltage:.4g} V > "
                        f"V_probe_max {probe_v_max:g} V"
                    )
            elif abs(measured.voltage) > probe_v_max:
                return (
                    f"측정 전류 구간 측정 전압 {measured.voltage:.4g} V > "
                    f"V_probe_max {probe_v_max:g} V (접촉 불량/단선 의심)"
                )

        if self.mode == "voltage":
            if abs(set_value) > limits["V_max"]:
                return f"설정 전압 {set_value:g} V > V_max {limits['V_max']:g} V"
            if power > limits["P_max"]:
                return f"측정 전력 {power:.4g} W > P_max {limits['P_max']:g} W"
        elif self.mode == "current":
            if abs(set_value) > limits["I_max"]:
                return (
                    f"설정 전류 {set_value * 1e3:g} mA > "
                    f"I_max {limits['I_max'] * 1e3:g} mA"
                )
            if power > limits["P_max"]:
                return f"측정 전력 {power:.4g} W > P_max {limits['P_max']:g} W"
        else:  # power
            if set_value > limits["P_max"]:
                return f"목표 전력 {set_value:g} W > P_max {limits['P_max']:g} W"
            if command_voltage is not None and command_voltage > limits["V_max"] + 1e-9:
                return (
                    f"명령 전압 {command_voltage:.4g} V > V_max {limits['V_max']:g} V"
                )
            ceiling = limits["P_max"] * POWER_MODE_MEASURED_MARGIN
            if power > ceiling:
                return (
                    f"측정 전력 {power:.4g} W > P_max×{POWER_MODE_MEASURED_MARGIN:g} "
                    f"= {ceiling:.4g} W"
                )

        # Common: resistance must not jump between two consecutive valid samples.
        if self.jump_skip > 0:
            # Glitched samples must not become the reference either: the
            # first valid sample after the window starts a new comparison.
            self.jump_skip -= 1
            self.previous_resistance = None
        elif resistance_is_valid(measured):
            resistance = measured.resistance
            previous = self.previous_resistance
            self.previous_resistance = resistance
            if previous is not None:
                change_pct = abs(resistance - previous) / previous * 100.0
                if change_pct > limits["R_jump_pct"]:
                    return (
                        f"저항 급변 {previous:.4g} → {resistance:.4g} ohm "
                        f"({change_pct:.2f}% > {limits['R_jump_pct']:g}%)"
                    )
        else:
            self.previous_resistance = None
        return ""


class PowerController:
    """Software constant-power loop on top of a voltage source.

    Command voltage V = sqrt(P_target * R), with R = V_meas / I_meas from the
    latest valid sample.  Increases are rate-limited to ``max_step_V`` per
    sample; decreases are applied immediately (the safe direction).
    The command never exceeds ``V_max``.
    """

    def __init__(self, flat: dict):
        self.v_max = float(flat["V_max"])
        self.probe_v = float(flat["probe_V"])
        self.max_step_v = float(flat["max_step_V"])
        self.max_failures = int(flat["max_R_failures"])
        self.command = 0.0
        self.target = 0.0
        self.last_resistance: float | None = None
        self.failures = 0
        self.saturated = False

    def _desired(self, resistance: float) -> float:
        desired = math.sqrt(self.target * resistance)
        self.saturated = desired > self.v_max
        desired = min(desired, self.v_max)
        if desired > self.command + self.max_step_v:
            desired = self.command + self.max_step_v
        return max(desired, 0.0)

    def start_step(self, target_power: float, keep_resistance: bool = False) -> float:
        """Return the command voltage to apply at the start of a step.

        For a 0 W step the last R is forgotten (it must be re-probed before
        the next heating) unless *keep_resistance* is set: then the probe
        current keeps R up to date through ``observe`` and the next heating
        starts from the cooled-down R.
        """
        self.target = float(target_power)
        self.failures = 0
        self.saturated = False
        if self.target <= 0.0:
            if not keep_resistance:
                self.last_resistance = None   # cooled down: R must be re-probed
            self.command = 0.0
        elif self.last_resistance is None:
            self.command = min(self.probe_v, self.v_max)
        else:
            self.command = self._desired(self.last_resistance)
        return self.command

    def update(self, measured) -> tuple[float, str]:
        """Return (next command voltage, abort reason or "")."""
        if self.target <= 0.0:
            self.command = 0.0
            return 0.0, ""
        if resistance_is_valid(measured):
            self.last_resistance = measured.resistance
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= self.max_failures:
                return 0.0, (
                    f"전력 모드: 유효한 저항을 {self.failures}회 연속 얻지 못함 "
                    f"(V={measured.voltage:.4g} V, I={measured.current:.4g} A)"
                )
            if self.last_resistance is None:
                return self.command, ""   # keep probing
        self.command = self._desired(self.last_resistance)
        return self.command, ""

    def observe(self, measured, command_voltage: float) -> None:
        """0 W step with probe current: track R and the actual output.

        The next heating then starts from this (cold) R, rate-limited from
        the probe voltage actually applied.
        """
        if resistance_is_valid(measured):
            self.last_resistance = measured.resistance
        self.command = float(command_voltage)


# =============================================================================
# Probe current in 0-output steps
# =============================================================================

DEFAULT_PROBE = {
    "enabled": False,
    "I_probe_A": 1e-3,
    "V_probe_max_V": 0.2,
    "R_jump_skip_samples": 2,
}


def normalize_probe(probe) -> dict | None:
    """Fill defaults and check types.  None stays None (feature unused)."""
    if probe is None:
        return None
    if not isinstance(probe, dict):
        raise ValueError("측정 전류 설정 형식이 올바르지 않습니다.")
    result = dict(DEFAULT_PROBE)
    result.update({k: v for k, v in probe.items() if k in DEFAULT_PROBE})
    result["enabled"] = bool(result["enabled"])
    for key in ("I_probe_A", "V_probe_max_V"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ValueError(f"측정 전류 설정 '{key}'은(는) 0보다 큰 유한한 숫자여야 합니다.")
        result[key] = float(value)
    skip = result["R_jump_skip_samples"]
    if isinstance(skip, bool) or not isinstance(skip, (int, float)) \
            or skip != int(skip) or skip < 0:
        raise ValueError("R 급변 검사 제외 샘플 수는 0 이상의 정수여야 합니다.")
    result["R_jump_skip_samples"] = int(skip)
    return result


def validate_probe(mode: str, probe: dict, flat: dict,
                   current_limit: float | None = None,
                   voltage_limit: float | None = None) -> None:
    """Pre-start check of the probe settings against the mode's limits."""
    i_probe, v_probe = probe["I_probe_A"], probe["V_probe_max_V"]
    if i_probe < R_VALID_MIN_CURRENT_A:
        raise ValueError(
            f"측정 전류 {i_probe * 1e3:g} mA는 R 유효 기준 "
            f"{R_VALID_MIN_CURRENT_A * 1e3:g} mA 이상이어야 합니다."
        )
    if mode == "current":
        if i_probe > flat["I_max"]:
            raise ValueError(
                f"측정 전류 {i_probe * 1e3:g} mA > I_max {flat['I_max'] * 1e3:g} mA"
            )
        v_lim = flat.get("V_lim", voltage_limit)
        if v_lim is not None and v_probe > v_lim:
            raise ValueError(f"V_probe_max {v_probe:g} V > V_lim {v_lim:g} V")
    else:
        i_lim = flat.get("I_lim", current_limit)
        if i_lim is not None and i_probe > i_lim:
            raise ValueError(
                f"측정 전류 {i_probe * 1e3:g} mA > I_lim {i_lim * 1e3:g} mA"
            )
        if v_probe > flat["V_max"]:
            raise ValueError(f"V_probe_max {v_probe:g} V > V_max {flat['V_max']:g} V")


class ProbeController:
    """Small measurement current on a voltage source: V = I_probe * R.

    R is the latest valid resistance.  Without a valid R the first command
    is V_probe_max / 4 and it is doubled (up to V_probe_max) while too
    little current flows to trust R.  The command never exceeds V_probe_max.
    """

    def __init__(self, i_probe: float, v_probe_max: float):
        self.i_probe = float(i_probe)
        self.v_max = float(v_probe_max)
        self.last_resistance: float | None = None
        self.command = 0.0

    def _from_resistance(self) -> float:
        return min(self.i_probe * self.last_resistance, self.v_max)

    def start(self, resistance_hint: float | None = None) -> float:
        if resistance_hint is not None and math.isfinite(resistance_hint) \
                and resistance_hint > 0:
            self.last_resistance = float(resistance_hint)
        if self.last_resistance is not None:
            self.command = self._from_resistance()
        else:
            self.command = self.v_max / 4.0
        return self.command

    def update(self, measured) -> float:
        if resistance_is_valid(measured):
            self.last_resistance = measured.resistance
            self.command = self._from_resistance()
        elif not (math.isfinite(measured.current)
                  and abs(measured.current) >= R_VALID_MIN_CURRENT_A):
            # Too little current to measure R: raise the probe voltage.
            self.command = min(max(self.command, self.v_max / 4.0) * 2.0, self.v_max)
        return self.command


# =============================================================================
# Step options: conditional cooling end / R_ref measurement step
#
# Step option dict (same keys as a profile JSON step):
#   {}                                              fixed duration (default)
#   {"end": {"type": "rref", "t_min_s", "tol_pct", "k_samples"}}
#   {"end": {"type": "slope", "t_min_s", "window_s", "slope_pct_per_s"}}
#   {"kind": "rref", "rref": {"t_on_s", "t_off_s", "repeats"}}
# For a conditional step the step duration is t_max.
# =============================================================================

DEFAULT_T_MAX_S = 40.0

DEFAULT_END = {
    "rref": {"t_min_s": 10.0, "tol_pct": 0.5, "k_samples": 5},
    "slope": {"t_min_s": 10.0, "window_s": 2.0, "slope_pct_per_s": 0.05},
}

END_LABELS = {"rref": "R_ref 복귀", "slope": "R 안정(기울기)"}

DEFAULT_COOLING = {"timeout_abort": False, "timeout_abort_count": 2}


def _positive(value, what: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        bound = "0 이상" if allow_zero else "0보다 큰"
        raise ValueError(f"{what}은(는) {bound} 유한한 숫자여야 합니다.")
    return float(value)


def _count(value, what: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value != int(value) or value < minimum:
        raise ValueError(f"{what}은(는) {minimum} 이상의 정수여야 합니다.")
    return int(value)


def normalize_end(end) -> dict | None:
    """Conditional end spec with defaults filled in.  None / fixed -> None."""
    if end is None:
        return None
    if not isinstance(end, dict):
        raise ValueError("냉각 종료 조건 형식이 올바르지 않습니다.")
    kind = end.get("type", "fixed")
    if kind == "fixed":
        return None
    if kind not in DEFAULT_END:
        raise ValueError(f"알 수 없는 냉각 종료 조건: {kind!r}")
    result = {"type": kind, **DEFAULT_END[kind]}
    result.update({k: v for k, v in end.items() if k in DEFAULT_END[kind]})
    result["t_min_s"] = _positive(result["t_min_s"], "최소 시간 t_min", allow_zero=True)
    if kind == "rref":
        result["tol_pct"] = _positive(result["tol_pct"], "R_ref 허용 오차")
        result["k_samples"] = _count(result["k_samples"], "연속 샘플 수 k")
    else:
        result["window_s"] = _positive(result["window_s"], "기울기 창 w")
        result["slope_pct_per_s"] = _positive(result["slope_pct_per_s"], "기울기 기준 s")
    return result


def normalize_step_options(options) -> dict:
    """Validate a step option dict (see above) and fill defaults."""
    if not options:
        return {}
    if not isinstance(options, dict):
        raise ValueError("단계 옵션 형식이 올바르지 않습니다.")
    result = {}
    if options.get("kind", "normal") == "rref":
        result["kind"] = "rref"
        result["rref"] = normalize_rref_step(options.get("rref"))
    elif options.get("kind", "normal") != "normal":
        raise ValueError(f"알 수 없는 단계 종류: {options.get('kind')!r}")
    end = normalize_end(options.get("end"))
    if end is not None:
        result["end"] = end
    return result


def validate_step_plan(steps, probe: dict | None, manual_r_ref: float | None) -> list[str]:
    """Pre-start check of step options.  *steps* = [(value, duration, options)].

    Returns one message per problem (row numbers start at 1).
    """
    messages = []
    probe_enabled = probe is not None and probe.get("enabled")
    r_ref_known = manual_r_ref is not None
    for row, (value, duration, options) in enumerate(steps, 1):
        try:
            options = normalize_step_options(options)
        except ValueError as exc:
            messages.append(f"{row}행: {exc}")
            continue
        end = options.get("end")
        if options.get("kind") == "rref":
            spec = options["rref"]
            if value != 0:
                messages.append(f"{row}행: R_ref 측정 단계의 값은 0이어야 합니다.")
            if probe is None:
                messages.append(f"{row}행: R_ref 측정 단계에는 측정 전류 설정이 필요합니다.")
            planned = rref_step_duration(spec)
            if abs(duration - planned) > 1e-6:
                messages.append(
                    f"{row}행: R_ref 측정 단계 시간 {duration:g} s ≠ "
                    f"반복 {spec['repeats']} × (on {spec['t_on_s']:g} + off {spec['t_off_s']:g}) "
                    f"= {planned:g} s"
                )
            if end is not None:
                messages.append(f"{row}행: R_ref 측정 단계에는 조건부 종료를 쓸 수 없습니다.")
            r_ref_known = True
            continue
        if end is None:
            continue
        if value != 0:
            messages.append(f"{row}행: 조건부 종료는 값이 0인 단계에서만 쓸 수 있습니다.")
        if not probe_enabled:
            messages.append(f"{row}행: 조건부 종료는 냉각 중 측정 전류가 켜져 있어야 합니다.")
        if duration < end["t_min_s"]:
            messages.append(
                f"{row}행: 최대 시간 {duration:g} s < 최소 시간 t_min {end['t_min_s']:g} s"
            )
        if end["type"] == "rref" and not r_ref_known:
            messages.append(
                f"{row}행: R_ref 복귀 조건에 쓸 R_ref가 없습니다 "
                "(앞쪽에 R_ref 측정 단계를 넣거나 R_ref를 직접 입력하세요)."
            )
    return messages


class CoolingCondition:
    """Conditional end of a 0-output (probe current) step.

    rref : |R - R_ref| / R_ref <= tol for k consecutive valid samples.
    slope: least-squares |dR/dt| / mean(R) <= s over the last w seconds of
           valid samples (the window must span w seconds).
    Never ends before t_min.  An invalid sample restarts the count/window.
    """

    def __init__(self, end: dict, r_ref: float | None):
        self.end = end
        self.kind = end["type"]
        self.r_ref = r_ref
        self.consecutive = 0
        self.window: list[tuple[float, float]] = []
        self.metric: float | None = None   # deviation % or slope %/s

    def update(self, elapsed: float, measured) -> bool:
        if not resistance_is_valid(measured):
            self.consecutive = 0
            self.window.clear()
            self.metric = None
            return False
        resistance = measured.resistance
        if self.kind == "rref":
            if self.r_ref is None:
                return False
            self.metric = abs(resistance - self.r_ref) / self.r_ref * 100.0
            self.consecutive = self.consecutive + 1 if self.metric <= self.end["tol_pct"] else 0
            met = self.consecutive >= self.end["k_samples"]
        else:
            window_s = self.end["window_s"]
            self.window.append((elapsed, resistance))
            # Keep the newest sample that is at least w old as the window start.
            while len(self.window) > 2 and self.window[1][0] <= elapsed - window_s:
                self.window.pop(0)
            met = False
            span = self.window[-1][0] - self.window[0][0]
            if len(self.window) >= 3 and span >= window_s - 1e-9:
                slope = _slope(self.window)
                mean = sum(r for _, r in self.window) / len(self.window)
                self.metric = abs(slope) / mean * 100.0
                met = self.metric <= self.end["slope_pct_per_s"]
        return met and elapsed >= self.end["t_min_s"]


def _slope(points) -> float:
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_r = sum(r for _, r in points) / n
    num = sum((t - mean_t) * (r - mean_r) for t, r in points)
    den = sum((t - mean_t) ** 2 for t, _ in points)
    return num / den if den > 0 else 0.0


# =============================================================================
# R_ref measurement step
# =============================================================================

DEFAULT_RREF_STEP = {"t_on_s": 5.0, "t_off_s": 5.0, "repeats": 3}

# R_ref = mean over on-segments of the mean R in the last RREF_AVERAGE_S of
# each on-segment (the whole on-segment when it is shorter).
RREF_AVERAGE_S = 3.0


def normalize_rref_step(spec) -> dict:
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise ValueError("R_ref 측정 단계 형식이 올바르지 않습니다.")
    result = dict(DEFAULT_RREF_STEP)
    result.update({k: v for k, v in spec.items() if k in DEFAULT_RREF_STEP})
    result["t_on_s"] = _positive(result["t_on_s"], "R_ref on 시간")
    result["t_off_s"] = _positive(result["t_off_s"], "R_ref off 시간", allow_zero=True)
    result["repeats"] = _count(result["repeats"], "R_ref 반복 횟수")
    return result


def rref_step_duration(spec: dict) -> float:
    return float(f"{spec['repeats'] * (spec['t_on_s'] + spec['t_off_s']):.12g}")

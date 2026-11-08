"""Profile step generators for the GUI: repeat blocks and reference-return sweeps.

No GUI or instrument dependency.  Steps use the profile JSON step format
(see profile_store): {"value", "duration_s", "note"} plus optional
"end" (conditional cooling) or "kind"/"rref" (R_ref measurement step).
Values are SI (V, W, A); notes show the GUI unit (V, W, mA).

Cooling spec (one per block / sweep):
    {"type": "fixed", "duration_s": 15}
    {"type": "rref",  "t_max_s": 40, "t_min_s": 10, "tol_pct": 0.5, "k_samples": 5}
    {"type": "slope", "t_max_s": 40, "t_min_s": 10, "window_s": 2,
     "slope_pct_per_s": 0.05}
"""

from __future__ import annotations

import math
import re

import control_modes


def _clean(value: float) -> float:
    """Drop binary noise: 0.30000000000000004 -> 0.3."""
    return float(f"{value:.12g}")


def value_label(value: float, mode: str) -> str:
    """'0.3W', '2V', '20mA' (GUI unit, SI value in)."""
    info = control_modes.mode_info(mode)
    return f"{_clean(value / info.scale):g}{info.unit}"


def _positive(value, what: str, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: 숫자를 입력하세요.") from None
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{what}: {'0 이상' if allow_zero else '0보다 큰'} 숫자를 입력하세요.")
    return number


def _count(value, what: str, minimum: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: 정수를 입력하세요.") from None
    if not math.isfinite(number) or number != int(number) or number < minimum:
        raise ValueError(f"{what}: {minimum} 이상의 정수를 입력하세요.")
    return int(number)


def normalize_cooling(cooling: dict) -> dict:
    kind = cooling.get("type", "fixed")
    if kind == "fixed":
        return {"type": "fixed",
                "duration_s": _positive(cooling.get("duration_s"), "냉각 시간")}
    end = control_modes.normalize_end(
        {k: v for k, v in cooling.items() if k not in ("t_max_s",)})
    t_max = _positive(cooling.get("t_max_s", control_modes.DEFAULT_T_MAX_S), "최대 시간 t_max")
    if t_max < end["t_min_s"]:
        raise ValueError(f"최대 시간 t_max {t_max:g} s < 최소 시간 t_min {end['t_min_s']:g} s")
    return {**end, "t_max_s": t_max}


def cooling_step(cooling: dict, note: str) -> dict:
    cooling = normalize_cooling(cooling)
    if cooling["type"] == "fixed":
        return {"value": 0.0, "duration_s": cooling["duration_s"], "note": note}
    end = {k: v for k, v in cooling.items() if k != "t_max_s"}
    return {"value": 0.0, "duration_s": cooling["t_max_s"], "note": note, "end": end}


def rref_step(spec: dict | None = None, note: str = "R_ref measure") -> dict:
    spec = control_modes.normalize_rref_step(spec)
    return {"value": 0.0, "duration_s": control_modes.rref_step_duration(spec),
            "note": note, "kind": "rref", "rref": spec}


def cycle_steps(value: float, heat_s: float, cooling: dict, label: str) -> list[dict]:
    """One cycle = [heat, cool] with notes '<label> heat' / '<label> cool'."""
    return [{"value": _clean(value), "duration_s": float(heat_s), "note": f"{label} heat"},
            cooling_step(cooling, f"{label} cool")]


def block_steps(mode: str, value: float, heat_s: float, cooling: dict,
                repeats: int, prefix: str = "") -> list[dict]:
    """[heat, cool] x repeats, notes '<prefix> #i heat' / '<prefix> #i cool'.

    An empty prefix becomes the value label (e.g. '0.5W').
    """
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value == 0:
        raise ValueError("가열 값은 0이 아닌 숫자여야 합니다.")
    heat_s = _positive(heat_s, "가열 시간")
    repeats = _count(repeats, "반복 횟수", 1)
    prefix = prefix.strip() or value_label(value, mode)
    steps = []
    for index in range(1, repeats + 1):
        steps += cycle_steps(value, heat_s, cooling, f"{prefix} #{index}")
    return steps


def parse_value_list(text: str) -> list[float]:
    """'0.4, 0.5,0.6' -> [0.4, 0.5, 0.6] (GUI units)."""
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if not parts:
        raise ValueError("시험값을 하나 이상 입력하세요.")
    values = []
    for part in parts:
        try:
            values.append(float(part))
        except ValueError:
            raise ValueError(f"시험값 '{part}'은(는) 숫자가 아닙니다.") from None
    return values


def value_range(start: float, end: float, step: float) -> list[float]:
    """start, start+step, ..., end (computed by index, no accumulated error)."""
    if not step > 0:
        raise ValueError("간격은 0보다 커야 합니다.")
    if end < start:
        raise ValueError("끝 값은 시작 값 이상이어야 합니다.")
    count = round((end - start) / step)
    if abs(start + count * step - end) > 1e-9 * max(1.0, abs(end)):
        raise ValueError(f"끝 값 {end:g}이(가) 시작 {start:g} + 간격 {step:g}에 맞지 않습니다.")
    return [_clean(start + i * step) for i in range(count + 1)]


def sweep_steps(mode: str, *, ref_value: float, test_values, heat_s: float,
                cooling: dict, ref_initial: int = 2, test_repeats: int = 3,
                ref_between: int = 1, idle_s: float = 5.0,
                include_rref: bool = False, rref_spec: dict | None = None) -> list[dict]:
    """Reference-return sweep (SI values).

        [0 step idle_s] [R_ref step] ref x ref_initial
        -> [test_1 x test_repeats -> ref x ref_between] -> ... -> [test_n ...]

    Notes: 'idle', 'R_ref measure', 'ref 0.3W #1 heat', 'test 0.5W #2 cool'.
    """
    if not isinstance(ref_value, (int, float)) or not math.isfinite(ref_value) \
            or ref_value == 0:
        raise ValueError("기준값은 0이 아닌 숫자여야 합니다.")
    test_values = list(test_values)
    if not test_values:
        raise ValueError("시험값을 하나 이상 입력하세요.")
    for value in test_values:
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value == 0:
            raise ValueError(f"시험값 {value!r}: 0이 아닌 숫자여야 합니다.")
    heat_s = _positive(heat_s, "가열 시간")
    idle_s = _positive(idle_s, "맨 앞 0 단계 시간", allow_zero=True)
    ref_initial = _count(ref_initial, "시작 기준 반복 수", 0)
    test_repeats = _count(test_repeats, "시험값당 반복 수", 1)
    ref_between = _count(ref_between, "시험값 사이 기준 반복 수", 0)
    cooling = normalize_cooling(cooling)

    ref_label = f"ref {value_label(ref_value, mode)}"
    steps = []
    if idle_s > 0:
        steps.append({"value": 0.0, "duration_s": idle_s, "note": "idle"})
    if include_rref:
        steps.append(rref_step(rref_spec))
    for index in range(1, ref_initial + 1):
        steps += cycle_steps(ref_value, heat_s, cooling, f"{ref_label} #{index}")
    for value in test_values:
        label = f"test {value_label(value, mode)}"
        for index in range(1, test_repeats + 1):
            steps += cycle_steps(value, heat_s, cooling, f"{label} #{index}")
        for index in range(1, ref_between + 1):
            steps += cycle_steps(ref_value, heat_s, cooling, f"{ref_label} #{index}")
    return steps


def summarize(steps: list[dict], mode: str, flat_limits: dict | None = None) -> dict:
    """Preview numbers: steps, cycles, time range, max value, limit violations."""
    total_max = sum(step["duration_s"] for step in steps)
    total_min = sum(step["end"]["t_min_s"] if "end" in step else step["duration_s"]
                    for step in steps)
    cycles, heating = 0, False
    for step in steps:
        if step["value"] != 0 and not heating:
            cycles += 1
        heating = step["value"] != 0
    values = [step["value"] for step in steps]
    peak = max(values, key=abs) if values else 0.0
    violations = (control_modes.profile_violations(mode, values, flat_limits)
                  if flat_limits is not None else [])
    return {"steps": len(steps), "cycles": cycles, "total_min_s": total_min,
            "total_max_s": total_max, "max_value": peak, "violations": violations}

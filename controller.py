"""향후 폐루프 제어 실험에 사용할 P/PI/PID 제어기."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class PIDController:
    """출력 제한과 적분 포화를 고려한 재사용 가능한 PID 제어기."""

    kp: float
    ki: float = 0.0
    kd: float = 0.0
    output_limits: Tuple[Optional[float], Optional[float]] = (None, None)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._integral = 0.0
        self._previous_error: Optional[float] = None

    def update(self, setpoint: float, measurement: float, dt: float) -> float:
        if dt <= 0.0:
            raise ValueError("dt는 0보다 커야 합니다.")

        error = setpoint - measurement
        derivative = 0.0 if self._previous_error is None else (error - self._previous_error) / dt
        candidate_integral = self._integral + error * dt
        unclamped = self.kp * error + self.ki * candidate_integral + self.kd * derivative
        output = self._clamp(unclamped)

        # 출력 포화 중 적분기가 계속 커지는 현상을 간단히 방지한다.
        if output == unclamped:
            self._integral = candidate_integral
        self._previous_error = error
        return output

    def _clamp(self, value: float) -> float:
        lower, upper = self.output_limits
        if lower is not None:
            value = max(lower, value)
        if upper is not None:
            value = min(upper, value)
        return value


def create_p_controller(kp: float, output_limits=(None, None)) -> PIDController:
    return PIDController(kp=kp, output_limits=output_limits)


def create_pi_controller(kp: float, ki: float, output_limits=(None, None)) -> PIDController:
    return PIDController(kp=kp, ki=ki, output_limits=output_limits)

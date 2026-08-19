"""레이저 변위 센서용 공통 인터페이스와 시험용 더미 센서."""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod


class DisplacementSensor(ABC):
    """변위 센서 구현이 따라야 하는 인터페이스."""

    @abstractmethod
    def connect(self) -> None:
        """센서에 연결한다."""

    @abstractmethod
    def read_displacement(self) -> float:
        """기준점 대비 수축 변위(mm)를 반환한다."""

    def update_excitation(self, voltage: float) -> None:
        """더미 모델 입력용 훅. 실제 센서에서는 아무 동작도 하지 않는다."""

    @abstractmethod
    def close(self) -> None:
        """센서 연결을 닫는다."""


class DummyDisplacementSensor(DisplacementSensor):
    """전압에 따라 지수적으로 수축하는 시험용 변위 센서."""

    def __init__(
        self,
        displacement_per_volt: float = 0.08,
        response_time_s: float = 0.3,
        noise_mm: float = 0.002,
    ) -> None:
        self.displacement_per_volt = displacement_per_volt
        self.response_time_s = response_time_s
        self.noise_mm = noise_mm
        self._target = 0.0
        self._value = 0.0
        self._last_update = time.monotonic()
        self._connected = False

    def connect(self) -> None:
        self._connected = True
        self._last_update = time.monotonic()

    def update_excitation(self, voltage: float) -> None:
        self._target = max(0.0, abs(voltage) * self.displacement_per_volt)

    def read_displacement(self) -> float:
        if not self._connected:
            raise RuntimeError("Dummy 변위 센서가 연결되지 않았습니다.")
        now = time.monotonic()
        elapsed = now - self._last_update
        self._last_update = now
        # 1차 지연 모델로 액추에이터의 느린 응답을 모사한다.
        alpha = 1.0 if self.response_time_s <= 0.0 else min(1.0, elapsed / self.response_time_s)
        self._value += (self._target - self._value) * alpha
        return self._value + random.gauss(0.0, self.noise_mm)

    def close(self) -> None:
        self._connected = False


class LaserDisplacementSensor(DisplacementSensor):
    """추후 실제 레이저 센서 프로토콜을 구현하기 위한 자리표시자."""

    def __init__(self, resource_name: str) -> None:
        self.resource_name = resource_name

    def connect(self) -> None:
        raise NotImplementedError("사용할 레이저 센서의 통신 프로토콜을 구현해야 합니다.")

    def read_displacement(self) -> float:
        raise NotImplementedError("레이저 센서의 변위 읽기 명령을 구현해야 합니다.")

    def close(self) -> None:
        pass

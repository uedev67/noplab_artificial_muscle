"""Keithley SourceMeter 제어 모듈.

실장비 구현과 더미 구현이 같은 인터페이스를 사용하므로, 추후 2460 드라이버를
추가할 때 ``SourceMeasureUnit``을 상속한 클래스만 교체하면 된다.
"""

from __future__ import annotations

import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Measurement:
    """한 시점의 전기 측정값."""

    voltage: float
    current: float
    resistance: float


class SourceMeasureUnit(ABC):
    """SourceMeter 공통 인터페이스."""

    @abstractmethod
    def connect(self) -> None:
        """장비에 연결한다."""

    @abstractmethod
    def set_voltage(self, voltage: float) -> None:
        """출력 전압을 설정한다."""

    @abstractmethod
    def output_on(self) -> None:
        """출력을 켠다."""

    @abstractmethod
    def output_off(self) -> None:
        """출력을 끈다."""

    @abstractmethod
    def measure(self) -> Measurement:
        """전압, 전류, 저항을 측정한다."""

    @abstractmethod
    def close(self) -> None:
        """연결을 안전하게 종료한다."""

    def set_current(self, current: float) -> None:
        """출력 전류를 설정한다 (전류 소스 지원 장비만)."""
        raise NotImplementedError("이 장비는 전류 소스를 지원하지 않습니다.")

    def configure_current_source(self, wire_mode: int, voltage_limit: float) -> None:
        """전류 소스 / 전압 측정으로 설정한다 (출력 OFF 상태에서만)."""
        raise NotImplementedError("이 장비는 전류 소스를 지원하지 않습니다.")

    def set_source_zero(self) -> None:
        """현재 소스 함수의 출력값을 0으로 내린다."""
        self.set_voltage(0.0)

    def safe_shutdown(self) -> None:
        """오류 여부와 관계없이 0 출력 및 출력 OFF를 시도한다."""

        try:
            self.set_source_zero()
        finally:
            self.output_off()

    def configure_voltage_measurement(self, wire_mode: int) -> None:
        """Configure local (2-wire) or remote (4-wire) voltage sensing."""
        if wire_mode not in (2, 4):
            raise ValueError("wire_mode must be 2 or 4")

    def __enter__(self) -> "SourceMeasureUnit":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            self.safe_shutdown()
        finally:
            self.close()


class Keithley2450(SourceMeasureUnit):
    """SCPI 명령을 사용하는 Keithley 2450 드라이버."""

    def __init__(
        self,
        resource_name: str,
        current_limit: float = 0.1,
        timeout_ms: int = 5000,
        backend: Optional[str] = None,
    ) -> None:
        self.resource_name = resource_name
        self.current_limit = current_limit
        self.timeout_ms = timeout_ms
        self.backend = backend
        self._resource_manager: Any = None
        self._instrument: Any = None
        self._set_voltage = 0.0
        self._set_current = 0.0
        self._source_function = "voltage"
        self._output_enabled = False
        self._reading_index = 0

    def connect(self) -> None:
        # 더미 모드에서는 PyVISA가 없어도 되도록 실장비 연결 시점에 import한다.
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError(
                "PyVISA가 설치되지 않았습니다. 'pip install -r requirements.txt'를 실행하세요."
            ) from exc

        self._resource_manager = pyvisa.ResourceManager(self.backend or "")
        self._instrument = self._resource_manager.open_resource(self.resource_name)
        self._instrument.timeout = self.timeout_ms
        self._instrument.write_termination = "\n"
        self._instrument.read_termination = "\n"

        # 2450을 전압 소스/전류 측정 모드로 초기화한다.
        self._instrument.write("*RST")
        self._instrument.write(':SOUR:FUNC VOLT')
        self._instrument.write(':SOUR:VOLT 0')
        self._instrument.write(':SENS:FUNC "CURR"')
        self._instrument.write(f":SOUR:VOLT:ILIM {self.current_limit}")
        self._source_function = "voltage"
        self._output_enabled = False  # *RST turns the output off.

    def _require_connection(self) -> Any:
        if self._instrument is None:
            raise RuntimeError("Keithley 장비가 연결되지 않았습니다.")
        return self._instrument

    def identify(self) -> str:
        """장비 식별 문자열을 반환한다."""

        return str(self._require_connection().query("*IDN?")).strip()

    def configure_two_wire_resistance(
        self, source_current: float, voltage_limit: float
    ) -> None:
        """전류를 소싱하고 전압을 측정하는 2선 저항 모드로 설정한다."""

        if source_current <= 0.0:
            raise ValueError("source_current는 0보다 커야 합니다.")
        if voltage_limit <= 0.0:
            raise ValueError("voltage_limit은 0보다 커야 합니다.")

        instrument = self._require_connection()
        instrument.write("*RST")
        instrument.write(':SENS:FUNC "VOLT"')
        instrument.write(":SENS:VOLT:RANG:AUTO ON")
        instrument.write(":SENS:VOLT:RSEN OFF")
        instrument.write(":SOUR:FUNC CURR")
        instrument.write(":SOUR:CURR 0")
        instrument.write(f":SOUR:CURR:VLIM {float(voltage_limit):.12g}")
        instrument.write(f":SOUR:CURR {float(source_current):.12g}")
        self._set_voltage = 0.0
        self._set_current = float(source_current)
        self._source_function = "current"
        self._output_enabled = False

    def measure_two_wire_resistance(self, source_current: float) -> Measurement:
        """측정 전압과 소스 전류로 2선 저항을 계산한다."""

        if source_current <= 0.0:
            raise ValueError("source_current는 0보다 커야 합니다.")
        voltage = float(self._require_connection().query(":MEAS:VOLT?"))
        resistance = abs(voltage / source_current)
        return Measurement(
            voltage=voltage,
            current=source_current,
            resistance=resistance,
        )

    def configure_current_source(self, wire_mode: int, voltage_limit: float) -> None:
        """전류 소스 / 전압 측정 모드로 전환한다.

        안전: 출력이 켜진 상태에서는 소스 기능을 바꾸지 않는다.

        실장비 확인 필요 (Keithley 2450 Reference Manual 기준으로 작성,
        실장비에서 아직 검증하지 않음):
          :SOUR:FUNC CURR, :SOUR:CURR:VLIM, :SENS:FUNC "VOLT",
          :SENS:VOLT:RSEN, :SOUR:CURR:READ:BACK
        """
        super().configure_voltage_measurement(wire_mode)
        if not math.isfinite(voltage_limit) or voltage_limit <= 0.0:
            raise ValueError("voltage_limit은 0보다 큰 유한한 숫자여야 합니다.")
        if self._output_enabled:
            raise RuntimeError("출력이 켜진 상태에서는 소스 기능을 변경할 수 없습니다.")
        instrument = self._require_connection()
        instrument.write(":SOUR:FUNC CURR")
        instrument.write(":SOUR:CURR 0")
        instrument.write(f":SOUR:CURR:VLIM {float(voltage_limit):.12g}")
        instrument.write(':SENS:FUNC "VOLT"')
        instrument.write(":SENS:VOLT:RANG:AUTO ON")
        instrument.write(":SENS:VOLT:RSEN " + ("ON" if wire_mode == 4 else "OFF"))
        # Readback stores the actual sourced current beside each voltage reading
        # in defbuffer1 (READ? returns the measured voltage in this mode).
        instrument.write(":SOUR:CURR:READ:BACK ON")
        instrument.write(':TRAC:CLE "defbuffer1"')
        self._reading_index = 0
        self._set_current = 0.0
        self._source_function = "current"

    def set_voltage(self, voltage: float) -> None:
        if self._source_function != "voltage":
            raise RuntimeError("전류 소스 상태에서는 전압을 설정할 수 없습니다.")
        instrument = self._require_connection()
        instrument.write(f":SOUR:VOLT {float(voltage):.12g}")
        self._set_voltage = float(voltage)

    def set_current(self, current: float) -> None:
        if self._source_function != "current":
            raise RuntimeError("전압 소스 상태에서는 전류를 설정할 수 없습니다.")
        instrument = self._require_connection()
        instrument.write(f":SOUR:CURR {float(current):.12g}")
        self._set_current = float(current)

    def set_source_zero(self) -> None:
        if self._source_function == "current":
            self.set_current(0.0)
        else:
            self.set_voltage(0.0)

    def configure_voltage_measurement(self, wire_mode: int) -> None:
        super().configure_voltage_measurement(wire_mode)
        instrument = self._require_connection()
        instrument.write(':SENS:CURR:RSEN ' + ('ON' if wire_mode == 4 else 'OFF'))
        # Source readback stores the actual sourced voltage beside each current
        # reading in defbuffer1.  READ? itself returns only the current on the
        # 2450, so the voltage is retrieved from the matching buffer record.
        instrument.write(":SOUR:VOLT:READ:BACK ON")
        instrument.write(':TRAC:CLE "defbuffer1"')
        self._reading_index = 0

    def safe_shutdown(self) -> None:
        """현재 소스 함수의 출력을 0으로 내리고 출력을 끈다."""

        try:
            if self._instrument is not None:
                self.set_source_zero()
        finally:
            self.output_off()

    def output_on(self) -> None:
        self._require_connection().write(":OUTP ON")
        self._output_enabled = True

    def output_off(self) -> None:
        if self._instrument is not None:
            self._instrument.write(":OUTP OFF")
            self._output_enabled = False

    def measure(self) -> Measurement:
        instrument = self._require_connection()
        # Voltage source: READ? = measured current, buffer SOUR = voltage readback.
        # Current source: READ? = measured voltage, buffer SOUR = current readback
        # (current-source branch: 실장비 확인 필요).
        reading = float(instrument.query(':READ? "defbuffer1"'))
        self._reading_index += 1
        source = float(
            instrument.query(
                f':TRAC:DATA? {self._reading_index},{self._reading_index},'
                '"defbuffer1",SOUR'
            )
        )
        if self._source_function == "current":
            voltage, current = reading, source
        else:
            voltage, current = source, reading
        resistance = abs(voltage / current) if abs(current) > 1e-15 else float("inf")
        return Measurement(voltage=voltage, current=current, resistance=resistance)

    def close(self) -> None:
        if self._instrument is not None:
            self._instrument.close()
            self._instrument = None
        if self._resource_manager is not None:
            self._resource_manager.close()
            self._resource_manager = None


class DummySourceMeasureUnit(SourceMeasureUnit):
    """장비 없이 전체 실험 흐름을 확인하기 위한 저항성 부하 모델.

    CNT+PDMS 시료처럼 전력에 따라 저항이 올라가는(PTC) 1차 지연 열 모델:
        dT/dt = (P * thermal_resistance - dT) / time_constant
        R = base_resistance * (1 + alpha * dT)
    장비 compliance(전압 소스의 전류 제한, 전류 소스의 전압 제한)도 흉내 낸다.
    """

    def __init__(
        self,
        base_resistance: float = 100.0,
        noise_ratio: float = 0.002,
        current_limit: float | None = None,
        thermal_resistance_k_per_w: float = 60.0,
        alpha_per_k: float = 0.005,
        time_constant_s: float = 2.0,
    ) -> None:
        self.base_resistance = base_resistance
        self.noise_ratio = noise_ratio
        self.current_limit = current_limit
        self.voltage_limit: float | None = None
        self.thermal_resistance = thermal_resistance_k_per_w
        self.alpha = alpha_per_k
        self.time_constant = time_constant_s
        self.temperature_rise = 0.0
        self._source_function = "voltage"
        self._voltage = 0.0
        self._current = 0.0
        self._connected = False
        self._output_enabled = False
        self._last_update: float | None = None

    def connect(self) -> None:
        self._connected = True
        self._source_function = "voltage"
        self._last_update = time.monotonic()

    def _check_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Dummy SourceMeter가 연결되지 않았습니다.")

    def configure_current_source(self, wire_mode: int, voltage_limit: float) -> None:
        super().configure_voltage_measurement(wire_mode)
        self._check_connected()
        if self._output_enabled:
            raise RuntimeError("출력이 켜진 상태에서는 소스 기능을 변경할 수 없습니다.")
        self.voltage_limit = float(voltage_limit)
        self._source_function = "current"
        self._current = 0.0

    def set_voltage(self, voltage: float) -> None:
        self._check_connected()
        if self._source_function != "voltage":
            raise RuntimeError("전류 소스 상태에서는 전압을 설정할 수 없습니다.")
        self._advance_thermal()
        self._voltage = float(voltage)

    def set_current(self, current: float) -> None:
        self._check_connected()
        if self._source_function != "current":
            raise RuntimeError("전압 소스 상태에서는 전류를 설정할 수 없습니다.")
        self._advance_thermal()
        self._current = float(current)

    def set_source_zero(self) -> None:
        if self._source_function == "current":
            self.set_current(0.0)
        else:
            self.set_voltage(0.0)

    def output_on(self) -> None:
        self._check_connected()
        self._advance_thermal()
        self._output_enabled = True

    def output_off(self) -> None:
        self._advance_thermal()
        self._output_enabled = False

    def resistance(self) -> float:
        return self.base_resistance * (1.0 + self.alpha * self.temperature_rise)

    def _electrical(self, resistance: float) -> tuple[float, float]:
        """Terminal (V, I) for the present source setting, with compliance."""
        if not self._output_enabled:
            return 0.0, 0.0
        if self._source_function == "current":
            current = self._current
            voltage = current * resistance
            if self.voltage_limit is not None and abs(voltage) > self.voltage_limit:
                voltage = math.copysign(self.voltage_limit, voltage)
                current = voltage / resistance
            return voltage, current
        voltage = self._voltage
        current = voltage / resistance
        if self.current_limit is not None and abs(current) > self.current_limit:
            current = math.copysign(self.current_limit, current)
            voltage = current * resistance
        return voltage, current

    def _advance_thermal(self) -> None:
        now = time.monotonic()
        if self._last_update is None:
            self._last_update = now
            return
        dt = now - self._last_update
        self._last_update = now
        voltage, current = self._electrical(self.resistance())
        steady = abs(voltage * current) * self.thermal_resistance
        self.temperature_rise += (steady - self.temperature_rise) * (
            1.0 - math.exp(-dt / self.time_constant)
        )

    def measure(self) -> Measurement:
        self._check_connected()
        self._advance_thermal()
        voltage, current = self._electrical(self.resistance())
        # Measurement noise on the current reading.
        current *= 1.0 + random.gauss(0.0, self.noise_ratio)
        resistance = abs(voltage / current) if abs(current) > 1e-15 else float("inf")
        time.sleep(0.002)  # 실제 통신 지연과 비슷한 흐름을 흉내 낸다.
        return Measurement(voltage=voltage, current=current, resistance=resistance)

    def close(self) -> None:
        self._output_enabled = False
        self._connected = False

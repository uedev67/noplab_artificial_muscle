"""전압 sweep 기반 인공근육 실험 실행 프로그램."""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from data_logger import CSVDataLogger, ExperimentRecord
from displacement_sensor import DisplacementSensor, DummyDisplacementSensor
from keithley_driver import DummySourceMeasureUnit, Keithley2450, SourceMeasureUnit


@dataclass
class SweepConfig:
    start_voltage: float = 0.0
    stop_voltage: float = 2.0
    step_voltage: float = 0.5
    dwell_s: float = 0.5
    sample_interval_s: float = 0.1
    initial_length_mm: float = 20.0

    def validate(self) -> None:
        if self.step_voltage == 0.0:
            raise ValueError("step_voltage는 0일 수 없습니다.")
        if (self.stop_voltage - self.start_voltage) * self.step_voltage < 0.0:
            raise ValueError("step_voltage의 부호가 sweep 방향과 맞지 않습니다.")
        if self.dwell_s <= 0.0 or self.sample_interval_s <= 0.0:
            raise ValueError("dwell_s와 sample_interval_s는 0보다 커야 합니다.")
        if self.initial_length_mm <= 0.0:
            raise ValueError("initial_length_mm는 0보다 커야 합니다.")


def voltage_points(start: float, stop: float, step: float) -> Iterable[float]:
    """부동소수점 오차에 강한, 끝점을 포함하는 sweep 값을 만든다."""

    if step == 0.0 or (stop - start) * step < 0.0:
        raise ValueError("유효하지 않은 sweep 범위입니다.")
    count = int(math.floor(abs((stop - start) / step) + 1e-12))
    for index in range(count + 1):
        yield start + index * step
    last = start + count * step
    if not math.isclose(last, stop, rel_tol=1e-12, abs_tol=1e-12):
        yield stop


class ExperimentRunner:
    """SMU와 변위 센서를 같은 단조 시간축으로 샘플링한다."""

    def __init__(
        self,
        smu: SourceMeasureUnit,
        sensor: DisplacementSensor,
        logger: CSVDataLogger,
        config: SweepConfig,
    ) -> None:
        self.smu = smu
        self.sensor = sensor
        self.logger = logger
        self.config = config

    def run(self) -> None:
        self.config.validate()
        start_time = time.monotonic()
        baseline_resistance: Optional[float] = None

        self.smu.connect()
        try:
            self.sensor.connect()
            try:
                self.logger.open()
                try:
                    self.smu.set_voltage(0.0)
                    self.smu.output_on()

                    for voltage in voltage_points(
                        self.config.start_voltage,
                        self.config.stop_voltage,
                        self.config.step_voltage,
                    ):
                        self.smu.set_voltage(voltage)
                        self.sensor.update_excitation(voltage)
                        step_end = time.monotonic() + self.config.dwell_s

                        while time.monotonic() < step_end:
                            sample_start = time.monotonic()
                            electrical = self.smu.measure()
                            displacement = self.sensor.read_displacement()

                            if baseline_resistance is None and math.isfinite(electrical.resistance):
                                baseline_resistance = electrical.resistance
                            contraction = displacement / self.config.initial_length_mm * 100.0
                            resistance_change = self._resistance_change(
                                electrical.resistance, baseline_resistance
                            )
                            self.logger.log(
                                ExperimentRecord(
                                    time=sample_start - start_time,
                                    voltage=electrical.voltage,
                                    current=electrical.current,
                                    resistance=electrical.resistance,
                                    displacement=displacement,
                                    contraction_ratio=contraction,
                                    resistance_change_ratio=resistance_change,
                                )
                            )

                            remaining = self.config.sample_interval_s - (
                                time.monotonic() - sample_start
                            )
                            if remaining > 0.0:
                                time.sleep(remaining)
                finally:
                    self.logger.close()
            finally:
                self.sensor.close()
        finally:
            # 어떤 단계에서 예외가 발생해도 우선 0 V, 그다음 출력 OFF를 시도한다.
            try:
                self.smu.safe_shutdown()
            finally:
                self.smu.close()

    @staticmethod
    def _resistance_change(value: float, baseline: Optional[float]) -> float:
        if baseline is None or baseline == 0.0 or not math.isfinite(value):
            return float("nan")
        return (value - baseline) / baseline * 100.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="인공근육 전압 sweep 실험")
    parser.add_argument("--real", action="store_true", help="더미 대신 실제 Keithley 사용")
    parser.add_argument("--resource", default="USB0::0x05E6::0x2450::INSTR")
    parser.add_argument("--output", type=Path, default=Path("data/experiment.csv"))
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--stop", type=float, default=2.0)
    parser.add_argument("--step", type=float, default=0.5)
    parser.add_argument("--dwell", type=float, default=0.5)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--length", type=float, default=20.0, help="초기 길이(mm)")
    parser.add_argument("--current-limit", type=float, default=0.1, help="전류 제한(A)")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    smu: SourceMeasureUnit
    if args.real:
        smu = Keithley2450(args.resource, current_limit=args.current_limit)
    else:
        smu = DummySourceMeasureUnit()

    sensor = DummyDisplacementSensor()
    config = SweepConfig(
        start_voltage=args.start,
        stop_voltage=args.stop,
        step_voltage=args.step,
        dwell_s=args.dwell,
        sample_interval_s=args.interval,
        initial_length_mm=args.length,
    )
    runner = ExperimentRunner(smu, sensor, CSVDataLogger(args.output), config)

    mode = "실장비" if args.real else "더미"
    print(f"{mode} 모드 실험을 시작합니다: {args.output}")
    try:
        runner.run()
    except KeyboardInterrupt:
        print("사용자가 실험을 중단했습니다. 출력을 안전하게 종료했습니다.")
    except Exception as exc:
        print(f"실험 오류: {exc}")
        raise
    else:
        print("실험이 완료되었고 출력을 안전하게 종료했습니다.")


if __name__ == "__main__":
    main()

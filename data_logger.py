"""인공근육 실험 결과를 CSV로 저장하는 모듈."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Optional, Union


@dataclass
class ExperimentRecord:
    time: float
    voltage: float
    current: float
    resistance: float
    displacement: float
    contraction_ratio: float
    resistance_change_ratio: float


class CSVDataLogger:
    """레코드를 즉시 flush하여 비정상 종료 시 데이터 손실을 줄인다."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        self._file: Optional[IO[str]] = None
        self._writer: Optional[csv.DictWriter] = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._file, fieldnames=list(ExperimentRecord.__annotations__))
        self._writer.writeheader()
        self._file.flush()

    def log(self, record: ExperimentRecord) -> None:
        if self._writer is None or self._file is None:
            raise RuntimeError("CSV logger가 열리지 않았습니다.")
        self._writer.writerow(asdict(record))
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None

    def __enter__(self) -> "CSVDataLogger":
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

"""실험 CSV를 읽어 주요 관계를 그래프로 출력한다."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List


REQUIRED_COLUMNS = {
    "time",
    "resistance",
    "contraction_ratio",
    "resistance_change_ratio",
}


def load_csv(path: Path) -> Dict[str, List[float]]:
    columns = {name: [] for name in REQUIRED_COLUMNS}
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV에 필요한 열이 없습니다: {sorted(missing)}")
        for row in reader:
            for name in REQUIRED_COLUMNS:
                columns[name].append(float(row[name]))
    if not columns["time"]:
        raise ValueError("CSV에 데이터가 없습니다.")
    return columns


def plot_experiment(csv_path: Path, output_path: Path | None = None, show: bool = True) -> None:
    # matplotlib는 그래프가 필요할 때만 import한다.
    import matplotlib.pyplot as plt

    data = load_csv(csv_path)
    figure, axes = plt.subplots(3, 1, figsize=(9, 11))

    axes[0].plot(data["time"], data["resistance"])
    axes[0].set(xlabel="Time (s)", ylabel="Resistance (ohm)", title="Time - Resistance")

    axes[1].plot(data["time"], data["contraction_ratio"])
    axes[1].set(xlabel="Time (s)", ylabel="Contraction ratio (%)", title="Time - Contraction")

    axes[2].scatter(
        data["resistance_change_ratio"], data["contraction_ratio"], s=12
    )
    axes[2].set(
        xlabel="Resistance change ratio (%)",
        ylabel="Contraction ratio (%)",
        title="Resistance change - Contraction",
    )

    for axis in axes:
        axis.grid(True, alpha=0.3)
    figure.tight_layout()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output_path, dpi=150)
        print(f"그래프를 저장했습니다: {output_path}")
    if show:
        plt.show()
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description="인공근육 실험 CSV 그래프")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--save", type=Path, help="PNG 등 그래프 저장 경로")
    parser.add_argument("--no-show", action="store_true", help="그래프 창을 열지 않음")
    args = parser.parse_args()
    plot_experiment(args.csv, args.save, not args.no_show)


if __name__ == "__main__":
    main()

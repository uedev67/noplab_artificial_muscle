"""Generate a power-mode "safe range" profile in profiles/ (profile_store format).

Structure (defaults in brackets):
    0 W start step [5 s]  (Arduino start marker)
    P_ref cycles [2]
    for P in start..end by step [0.4 .. 1.1 W, +0.1 W]:
        P test cycles [3]  ->  P_ref cycle [1]

One cycle = heat step (target power, [8 s]) + cool step (0 W, [15 s]).
Power values are rounded to --decimals [1] so no 0.30000000004-style values
are stored.  The profile is written with profile_store.save_profile(), so an
existing profile with the same name is never overwritten (error instead).

Usage:
    python tools/make_safe_range_profile.py
    python tools/make_safe_range_profile.py --p-start 0.4 --p-end 0.8 --name my_profile
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

import control_modes  # noqa: E402
import profile_store  # noqa: E402

DEFAULT_NAME = "safe_range_46_P0.3to1.1_h8c15"


def fmt(value: float) -> str:
    return f"{value:g}"


def power_levels(p_start: float, p_end: float, p_step: float, decimals: int) -> list[float]:
    """p_start, p_start + p_step, ... p_end, computed by index (no accumulation)."""
    if p_step <= 0:
        raise ValueError("증가 폭은 0보다 커야 합니다.")
    if p_end < p_start:
        raise ValueError("끝 전력은 시작 전력 이상이어야 합니다.")
    count = round((p_end - p_start) / p_step)
    if abs(p_start + count * p_step - p_end) > 1e-9:
        raise ValueError(f"끝 전력 {fmt(p_end)} W가 시작 {fmt(p_start)} W + "
                         f"{fmt(p_step)} W 간격에 맞지 않습니다.")
    return [round(p_start + i * p_step, decimals) for i in range(count + 1)]


def build_steps(p_ref, levels, heat_s, cool_s, test_repeats, ref_initial,
                ref_between, start_s, decimals):
    p_ref = round(p_ref, decimals)
    steps = [{"value": 0.0, "duration_s": float(start_s), "note": "start marker 0W"}]

    def cycle(power, label):
        steps.append({"value": power, "duration_s": float(heat_s), "note": f"{label} heat"})
        steps.append({"value": 0.0, "duration_s": float(cool_s), "note": f"{label} cool"})

    for _ in range(ref_initial):
        cycle(p_ref, f"ref {fmt(p_ref)}W")
    for power in levels:
        for repeat in range(1, test_repeats + 1):
            cycle(power, f"test {fmt(power)}W #{repeat}")
        for _ in range(ref_between):
            cycle(p_ref, f"ref {fmt(p_ref)}W")
    return steps


def build_profile(args) -> dict:
    levels = power_levels(args.p_start, args.p_end, args.p_step, args.decimals)
    steps = build_steps(args.p_ref, levels, args.heat_s, args.cool_s, args.test_repeats,
                        args.ref_initial, args.ref_between, args.start_s, args.decimals)
    limits = {
        "power": {"I_lim": args.i_lim, "P_max": args.p_max, "V_max": args.v_max},
        "common": {"R_jump_pct": args.r_jump_pct},
        "power_control": {"probe_V": args.probe_v, "max_step_V": args.max_step_v,
                          "max_R_failures": args.max_r_failures},
    }
    description = (
        f"전력 모드 safe range: 0W {fmt(args.start_s)}s(시작 마커) → "
        f"ref {fmt(args.p_ref)}W ×{args.ref_initial} → "
        f"[{fmt(levels[0])}~{fmt(levels[-1])}W +{fmt(args.p_step)}W: "
        f"test ×{args.test_repeats} → ref ×{args.ref_between}], "
        f"가열 {fmt(args.heat_s)}s / 냉각 {fmt(args.cool_s)}s"
    )
    return {"name": args.name, "mode": "power", "steps": steps,
            "limits": limits, "description": description}


def check_limits(profile: dict) -> list[str]:
    """Same checks the GUI runs before start (validate_limits + profile_violations)."""
    flat = control_modes.runtime_limits("power", profile["limits"])
    control_modes.validate_limits("power", flat)
    return control_modes.profile_violations(
        "power", [step["value"] for step in profile["steps"]], flat)


def summarize(profile: dict) -> str:
    steps = profile["steps"]
    heats = [s["value"] for s in steps if s["value"] > 0]
    total = sum(s["duration_s"] for s in steps)
    lines = [f"단계 수: {len(steps)}", f"사이클 수: {len(heats)}",
             f"총 소요 시간: {fmt(total)} s ({total / 60:.2f} 분)",
             "전력별 사이클 수:"]
    for power, count in sorted(Counter(heats).items()):
        lines.append(f"  {fmt(power)} W : {count}회")
    return "\n".join(lines)


def parse_args(argv=None):
    d_ctrl = control_modes.DEFAULT_LIMITS["power_control"]
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--name", default=DEFAULT_NAME, help="프로파일 이름 (파일명)")
    p.add_argument("--p-ref", type=float, default=0.3, help="기준 전력 P_ref (W)")
    p.add_argument("--p-start", type=float, default=0.4, help="시험 시작 전력 (W)")
    p.add_argument("--p-end", type=float, default=1.1, help="시험 끝 전력 (W)")
    p.add_argument("--p-step", type=float, default=0.1, help="시험 전력 증가 폭 (W)")
    p.add_argument("--heat-s", type=float, default=8.0, help="가열 시간 (s)")
    p.add_argument("--cool-s", type=float, default=15.0, help="냉각 시간 (s)")
    p.add_argument("--test-repeats", type=int, default=3, help="전력별 시험 반복 횟수")
    p.add_argument("--ref-initial", type=int, default=2, help="맨 앞 P_ref 사이클 수")
    p.add_argument("--ref-between", type=int, default=1, help="전력별 시험 뒤 P_ref 사이클 수")
    p.add_argument("--start-s", type=float, default=5.0, help="맨 앞 0 W 단계 시간 (s)")
    p.add_argument("--decimals", type=int, default=1, help="전력 값 반올림 자릿수")
    p.add_argument("--i-lim", type=float, default=0.25, help="전류 제한 I_lim (A)")
    p.add_argument("--v-max", type=float, default=6.0, help="명령 전압 상한 V_max (V)")
    p.add_argument("--p-max", type=float, default=1.1, help="목표 전력 상한 P_max (W)")
    p.add_argument("--r-jump-pct", type=float, default=10.0, help="R 급변 기준 (%%)")
    p.add_argument("--probe-v", type=float, default=d_ctrl["probe_V"], help="탐침 전압 (V)")
    p.add_argument("--max-step-v", type=float, default=d_ctrl["max_step_V"],
                   help="샘플당 최대 전압 증가 (V)")
    p.add_argument("--max-r-failures", type=int, default=d_ctrl["max_R_failures"],
                   help="R 연속 실패 허용 횟수")
    p.add_argument("--profile-dir", type=Path, default=profile_store.PROFILE_DIR,
                   help="저장 폴더 (기본: profiles/)")
    p.add_argument("--dry-run", action="store_true", help="저장하지 않고 요약만 출력")
    args = p.parse_args(argv)
    for key in ("test_repeats", "ref_initial", "ref_between"):
        if getattr(args, key) < 0:
            p.error(f"--{key.replace('_', '-')}는 0 이상이어야 합니다.")
    for key in ("heat_s", "cool_s", "start_s"):
        if getattr(args, key) <= 0:
            p.error(f"--{key.replace('_', '-')}는 0보다 커야 합니다.")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        profile = build_profile(args)
        violations = check_limits(profile)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    print(summarize(profile))
    if violations:
        print("안전 상한 초과 — 저장하지 않습니다:", file=sys.stderr)
        for message in violations:
            print(f"  {message}", file=sys.stderr)
        return 1
    print("안전 상한 검사: 모든 단계 통과")
    if args.dry_run:
        return 0
    try:
        path = profile_store.save_profile(profile, args.profile_dir, overwrite=False)
    except (FileExistsError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    print(f"저장됨: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

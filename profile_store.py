"""Control profile storage (profiles/*.json) and last-used GUI settings.

One JSON file per profile:

    {
      "format_version": 1,
      "name": "0.5W 가열 10초",
      "mode": "power",                     # voltage | power | current
      "value_unit": "W",                   # SI unit of step values
      "steps": [{"value": 0.5, "duration_s": 10.0, "note": ""}, ...],
      "limits": {"power": {...}, "common": {...}, "power_control": {...}},
      "description": "",
      "created_at": "2026-11-08T12:00:00+09:00",
      "updated_at": "2026-11-08T12:00:00+09:00"
    }

Step values are SI (current mode stores A even though the GUI shows mA).
The read-only voltage profiles in voltage_profiles.PROFILES are listed as
built-in profiles.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import control_modes
from voltage_profiles import PROFILES as BUILTIN_PROFILES

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "profiles"
SETTINGS_PATH = BASE_DIR / "settings" / "last_settings.json"
FORMAT_VERSION = 1

MAX_NAME_LENGTH = 100
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_builtin_name(name: str) -> bool:
    return name.casefold() in {key.casefold() for key in BUILTIN_PROFILES}


def validate_name(name: str) -> str:
    """Return the cleaned profile name or raise ValueError."""
    name = (name or "").strip()
    if not name:
        raise ValueError("프로파일 이름을 입력하세요.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"프로파일 이름은 {MAX_NAME_LENGTH}자 이하여야 합니다.")
    bad = sorted(set(_INVALID_CHARS.findall(name)))
    if bad:
        shown = " ".join(repr(c) if c.isprintable() else "제어문자" for c in bad)
        raise ValueError(f"프로파일 이름에 파일명으로 쓸 수 없는 문자가 있습니다: {shown}")
    if name.endswith((".", " ")):
        raise ValueError("프로파일 이름은 점(.)이나 공백으로 끝날 수 없습니다.")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        raise ValueError(f"'{name}'은(는) Windows 예약 이름이라 사용할 수 없습니다.")
    if is_builtin_name(name):
        raise ValueError(f"'{name}'은(는) 기본 제공 프로파일 이름입니다. 다른 이름을 쓰세요.")
    return name


def profile_path(name: str, directory: Path | None = None) -> Path:
    return Path(directory or PROFILE_DIR) / f"{validate_name(name)}.json"


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what}: 숫자가 아닙니다 ({value!r}).")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{what}: 유한한 숫자가 아닙니다.")
    return value


def normalize_profile(data: dict) -> dict:
    """Validate a profile dict (from disk or the GUI) and return a clean copy."""
    if not isinstance(data, dict):
        raise ValueError("프로파일 형식이 올바르지 않습니다.")
    mode = data.get("mode")
    info = control_modes.mode_info(mode)
    steps = []
    for index, step in enumerate(data.get("steps") or [], 1):
        if not isinstance(step, dict):
            raise ValueError(f"{index}단계 형식이 올바르지 않습니다.")
        value = _number(step.get("value"), f"{index}단계 값")
        duration = _number(step.get("duration_s"), f"{index}단계 시간")
        if duration <= 0:
            raise ValueError(f"{index}단계 시간은 0보다 커야 합니다.")
        steps.append({"value": value, "duration_s": duration,
                      "note": str(step.get("note", ""))})
    if not steps:
        raise ValueError("프로파일에 단계가 없습니다.")

    limits = {}
    for group, values in (data.get("limits") or {}).items():
        if group not in control_modes.LIMIT_FIELDS or not isinstance(values, dict):
            continue
        known = {field.key for field in control_modes.LIMIT_FIELDS[group]}
        limits[group] = {
            key: _number(value, f"안전 상한 {key}")
            for key, value in values.items() if key in known
        }
    return {
        "format_version": FORMAT_VERSION,
        "name": str(data.get("name", "")),
        "mode": mode,
        "value_unit": info.si_unit,
        "steps": steps,
        "limits": limits,
        "description": str(data.get("description", "")),
        "created_at": str(data.get("created_at", "")),
        "updated_at": str(data.get("updated_at", "")),
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def save_profile(data: dict, directory: Path | None = None,
                 overwrite: bool = False) -> Path:
    """Save *data* as <name>.json.  Raises FileExistsError unless overwrite."""
    name = validate_name(data.get("name", ""))
    path = profile_path(name, directory)
    profile = normalize_profile(dict(data, name=name))
    created = ""
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"같은 이름의 프로파일이 이미 있습니다: {name}")
        try:
            created = load_profile(path).get("created_at", "")
        except (OSError, ValueError):
            created = ""
    stamp = now_iso()
    profile["created_at"] = created or stamp
    profile["updated_at"] = stamp
    _write_json(path, profile)
    return path


def load_profile(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 형식 오류: {exc}") from exc
    profile = normalize_profile(data)
    profile["name"] = profile["name"] or Path(path).stem
    return profile


def delete_profile(name: str, directory: Path | None = None) -> None:
    profile_path(name, directory).unlink()


def builtin_profile(name: str) -> dict:
    """A voltage_profiles.PROFILES entry as a read-only voltage profile."""
    steps = []
    for entry in BUILTIN_PROFILES[name]:
        voltage, duration = entry[0], entry[1]
        note = entry[3] if len(entry) == 4 else ""
        steps.append({"value": float(voltage), "duration_s": float(duration),
                      "note": str(note)})
    return {
        "format_version": FORMAT_VERSION,
        "name": name,
        "mode": "voltage",
        "value_unit": "V",
        "steps": steps,
        "limits": {},
        "description": "기본 제공 (voltage_profiles.py, 읽기 전용)",
        "created_at": "",
        "updated_at": "",
    }


def _summary(profile: dict, builtin: bool, path: Path | None) -> dict:
    return {
        "name": profile["name"],
        "mode": profile["mode"],
        "steps": len(profile["steps"]),
        "total_s": sum(step["duration_s"] for step in profile["steps"]),
        "updated_at": profile["updated_at"],
        "description": profile["description"],
        "builtin": builtin,
        "path": path,
    }


def list_profiles(directory: Path | None = None, include_builtin: bool = True):
    """Return (summaries, errors).  User profiles first, then built-ins."""
    directory = Path(directory or PROFILE_DIR)
    summaries, errors = [], []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stem.casefold()):
            try:
                summaries.append(_summary(load_profile(path), False, path))
            except (OSError, ValueError) as exc:
                errors.append(f"{path.name}: {exc}")
    if include_builtin:
        for name in BUILTIN_PROFILES:
            try:
                summaries.append(_summary(builtin_profile(name), True, None))
            except (TypeError, ValueError, IndexError) as exc:
                errors.append(f"[기본] {name}: {exc}")
    return summaries, errors


# =============================================================================
# Last-used settings (local only, git-ignored)
# =============================================================================


def load_last_settings(path: Path | None = None) -> dict:
    path = Path(path or SETTINGS_PATH)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_last_settings(data: dict, path: Path | None = None) -> None:
    _write_json(Path(path or SETTINGS_PATH), dict(data, saved_at=now_iso()))

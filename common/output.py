from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.time import now_shanghai, sort_key


SIMPLE_KEYS = ("公司", "岗位", "岗位上新时间", "JD", "投递方式", "原始链接")


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _save_meta(path: Path, metadata: dict[str, Any]) -> None:
    payload = dict(metadata)
    payload.setdefault("collected_at", now_shanghai().strftime("%Y-%m-%d %H:%M:%S"))
    meta_path = path.with_name(f"{path.stem}.meta.json")
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_raw_json(path: Path, content: bytes | str, metadata: dict[str, Any]) -> None:
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    _write_bytes(path, data)
    _save_meta(path, metadata)


def save_raw_html(path: Path, content: bytes | str, metadata: dict[str, Any]) -> None:
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    _write_bytes(path, data)
    _save_meta(path, metadata)


def save_error_meta(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_meta(path, metadata)


def normalize_simple(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    deduplicated: dict[str, dict[str, str]] = {}
    for item in items:
        normalized = {key: str(item.get(key, "") or "") for key in SIMPLE_KEYS}
        link = normalized["原始链接"]
        if link and link not in deduplicated:
            deduplicated[link] = normalized
    return sorted(
        deduplicated.values(),
        key=lambda item: sort_key(item["岗位上新时间"]),
        reverse=True,
    )


def save_simple_json(path: Path, items: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized = normalize_simple(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized

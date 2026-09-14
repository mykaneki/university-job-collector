from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
ACCEPTED_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_cli_datetime(value: str, *, is_end: bool = False) -> datetime:
    value = value.strip()
    for fmt in ACCEPTED_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and is_end:
            parsed = datetime.combine(parsed.date(), time.max.replace(microsecond=0))
        return parsed.replace(tzinfo=SHANGHAI)
    raise ValueError(f"无法解析时间 {value!r}，支持 YYYY-MM-DD[ HH:MM[:SS]]")


def parse_source_datetime(value: str) -> datetime:
    cleaned = value.strip().replace(".", "-").replace("/", "-")
    for fmt in ACCEPTED_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    raise ValueError(f"无法解析来源时间 {value!r}")


def in_range(value: str, start: datetime, end: datetime) -> bool:
    parsed = parse_source_datetime(value)
    return start <= parsed <= end


def sort_key(value: str) -> datetime:
    try:
        return parse_source_datetime(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=SHANGHAI)


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)

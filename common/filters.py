from __future__ import annotations

from collections.abc import Iterable


COMMON_FILTERS = {
    "company",
    "position",
    "keyword",
    "location",
    "company_nature",
    "industry",
    "education",
    "position_type",
    "employment_type",
    "student_type",
}


def contains(text: object, keyword: str) -> bool:
    return keyword.casefold() in str(text or "").casefold()


def equals_normalized(left: object, right: object) -> bool:
    normalize = lambda value: "".join(str(value or "").split()).casefold()
    return normalize(left) == normalize(right)


def parse_filter_args(values: Iterable[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"筛选条件必须是 key=value：{raw!r}")
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ValueError(f"筛选条件的 key 和 value 不能为空：{raw!r}")
        if key not in COMMON_FILTERS:
            raise ValueError(f"未知统一筛选字段：{key}")
        if key in parsed:
            raise ValueError(f"同一筛选字段不能重复：{key}")
        parsed[key] = value
    return parsed


def validate_supported(filters: dict[str, str], capabilities: dict[str, str], school: str) -> None:
    unsupported = [key for key in filters if capabilities.get(key, "unsupported") == "unsupported"]
    if unsupported:
        rendered = ", ".join(f"{key}={filters[key]}" for key in unsupported)
        raise ValueError(f"{school}不支持筛选：{rendered}")

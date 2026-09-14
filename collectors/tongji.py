from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "同济大学", "TONGJI"
BASE_URL = "https://tj91.tongji.edu.cn"
LIST_API = f"{BASE_URL}/f/recruitmentinfo/ajax_frontRecruitinfo"
DETAIL_API = f"{BASE_URL}/f/recruitmentinfo/ajax_show"
SEARCH_API = f"{BASE_URL}/f/recruitmentinfo/ajax_search"
PAGE_SIZE, MAX_PAGES = 15, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "native", "keyword": "native",
    "location": "unsupported", "company_nature": "native", "industry": "native",
    "education": "local", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _native_value(options: dict[str, object], key: str, label: str) -> str:
    rows = options.get(key) or []
    mapping = {str(row.get("label")): str(row.get("value")) for row in rows if isinstance(row, dict)}
    if label not in mapping:
        raise ValueError(f"{SCHOOL_NAME} {key} 不识别 {label!r}，可用值：{'、'.join(mapping)}")
    return mapping[label]


def _company(record: dict[str, object], detail: dict[str, object] | None = None) -> str:
    for source in (record.get("corporationinfo"), (detail or {}).get("corporationinfo")):
        if isinstance(source, dict) and source.get("name"):
            return str(source["name"])
    return str(record.get("corporationName") or (detail or {}).get("corporationName") or "")


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=json_api", TAG)
    matched: list[dict[str, object]] = []
    pages = older_pages = detail_success = detail_failed = 0
    with HttpClient() as client:
        options: dict[str, object] = {}
        if filters.get("company_nature") or filters.get("industry"):
            response = client.post(SEARCH_API, data={"positionType": "1"})
            save_raw_json(raw_dir / "filter_options.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("state") != 1:
                raise RuntimeError(f"{SCHOOL_NAME}筛选字典 API 返回失败")
            options = payload.get("object") or {}
        title = filters.get("position", "")
        keyword = filters.get("keyword", "")
        if title and keyword and title != keyword:
            raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 title 参数，不能同时传入不同值")
        for page in range(1, MAX_PAGES + 1):
            data = {
                "pageNo": page, "pageSize": PAGE_SIZE, "positionType": "1", "title": title or keyword,
                "corporationNature": _native_value(options, "corporationNature", filters["company_nature"]) if filters.get("company_nature") else "",
                "corporationinfo.industry": _native_value(options, "industry", filters["industry"]) if filters.get("industry") else "",
            }
            response = client.post(LIST_API, data=data)
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("state") != 1:
                raise RuntimeError(f"{SCHOOL_NAME}列表 API 返回失败")
            obj = payload.get("object") or {}
            records = obj.get("list") or []
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            normal_dates = []
            for record in records:
                published = str(record.get("startTime") or "")
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    continue
                if str(record.get("topFlag") or record.get("isTop") or "0") != "1":
                    normal_dates.append(parsed)
                if not in_range(published, start, end):
                    continue
                if filters.get("company") and not contains(_company(record), filters["company"]):
                    continue
                if filters.get("education") and not contains(record.get("education"), filters["education"]):
                    continue
                matched.append(record)
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2 or bool(obj.get("lastPage")):
                break
        results = []
        for record in matched:
            source_id = str(record.get("id") or "unknown")
            detail: dict[str, object] = {}
            try:
                response = client.post(DETAIL_API, data={"recruitmentId": source_id})
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="POST"))
                payload = response.json()
                if payload.get("state") != 1:
                    raise RuntimeError(str(payload.get("msg") or "detail state != 1"))
                detail = (payload.get("object") or {}).get("recruitmentinfo") or {}
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                LOGGER.error("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
            descriptions = []
            for position in detail.get("recruitmentPositionList") or []:
                if isinstance(position, dict) and position.get("positionDescription"):
                    descriptions.append(str(position["positionDescription"]))
            content = "\n\n".join(descriptions)
            original_url = str(record.get("url") or detail.get("url") or "")
            results.append({
                "公司": _company(record, detail),
                "岗位": str(record.get("title") or detail.get("title") or ""),
                "岗位上新时间": str(record.get("startTime") or detail.get("startTime") or ""),
                "JD": html_to_text(content),
                "投递方式": extract_application_methods(content, explicit_email=str(detail.get("resumeReceiveEmail") or ""), explicit_url=str(detail.get("onlineApplicationUrl") or "")),
                "原始链接": urljoin(BASE_URL, original_url),
            })
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

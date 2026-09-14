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
SCHOOL_NAME, TAG = "武汉大学", "WHU"
BASE_URL = "https://xsjy.whu.edu.cn"
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
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, object]] = []
    LOGGER.info("[%s] strategy=json_api", TAG)
    with HttpClient() as client:
        options: dict[str, object] = {}
        if filters.get("company_nature") or filters.get("industry"):
            response = client.post(SEARCH_API, data={"positionType": "1"})
            save_raw_json(raw_dir / "filter_options.json", response.content, response_metadata(response, method="POST"))
            options = response.json().get("object") or {}
        title = filters.get("position", "")
        keyword = filters.get("keyword", "")
        if title and keyword and title != keyword:
            raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 title 参数，不能同时传入不同值")
        for page in range(1, MAX_PAGES + 1):
            data = {"pageNo": page, "pageSize": PAGE_SIZE, "positionType": "1", "title": title or keyword,
                    "corporationNature": _native_value(options, "corporationNature", filters["company_nature"]) if filters.get("company_nature") else "",
                    "corporationinfo.industry": _native_value(options, "industry", filters["industry"]) if filters.get("industry") else ""}
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
            dates = []
            for record in records:
                published = str(record.get("startTime") or "")
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    continue
                if str(record.get("topFlag") or "0") != "1":
                    dates.append(parsed)
                if in_range(published, start, end) and (not filters.get("company") or contains(_company(record), filters["company"])) and (not filters.get("education") or contains(record.get("education"), filters["education"])):
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2 or bool(obj.get("lastPage")):
                break
        results = []
        # 详情 API 存在偶发的长时间无字节响应；单条失败不应拖住整批列表。
        detail_client = HttpClient(timeout=5.0, retries=0)
        for record in matched:
            source_id = str(record.get("id") or "unknown")
            detail: dict[str, object] = {}
            try:
                response = detail_client.post(DETAIL_API, data={"recruitmentId": source_id})
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="POST"))
                payload = response.json()
                if payload.get("state") != 1:
                    raise RuntimeError(str(payload.get("msg") or "detail state != 1"))
                detail = (payload.get("object") or {}).get("recruitmentinfo") or {}
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
            descriptions = [str(p.get("positionDescription")) for p in detail.get("recruitmentPositionList") or [] if isinstance(p, dict) and p.get("positionDescription")]
            content = "\n\n".join(descriptions)
            results.append({"\u516c\u53f8": _company(record, detail), "\u5c97\u4f4d": str(record.get("title") or detail.get("title") or ""), "\u5c97\u4f4d\u4e0a\u65b0\u65f6\u95f4": str(record.get("startTime") or detail.get("startTime") or ""), "JD": html_to_text(content), "\u6295\u9012\u65b9\u5f0f": extract_application_methods(content, explicit_email=str(detail.get("resumeReceiveEmail") or ""), explicit_url=str(detail.get("onlineApplicationUrl") or "")), "\u539f\u59cb\u94fe\u63a5": urljoin(BASE_URL, str(record.get("url") or detail.get("url") or ""))})
        detail_client.close()
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

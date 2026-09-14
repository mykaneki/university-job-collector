from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "西安交通大学", "XJTU"
BASE_URL = "https://job.xjtu.edu.cn"
LIST_API = f"{BASE_URL}/f/recruitmentinfo/ajax_frontRecruitinfo"
DETAIL_API = f"{BASE_URL}/f/recruitmentinfo/ajax_show"
SEARCH_API = f"{BASE_URL}/f/recruitmentinfo/ajax_search"
PAGE_SIZE, MAX_PAGES = 15, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "native", "keyword": "native", "location": "unsupported",
    "company_nature": "native", "industry": "native", "education": "unsupported",
    "position_type": "unsupported", "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _native(options: dict[str, object], key: str, label: str) -> str:
    mapping = {str(x.get("label")): str(x.get("value")) for x in options.get(key) or [] if isinstance(x, dict)}
    if label not in mapping:
        raise ValueError(f"{SCHOOL_NAME} {key} 不识别 {label!r}，可用值：{'、'.join(mapping)}")
    return mapping[label]


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    matched = []
    pages = older_pages = detail_success = detail_failed = 0
    with HttpClient() as client:
        options: dict[str, object] = {}
        if filters.get("company_nature") or filters.get("industry"):
            r = client.post(SEARCH_API, data={"positionType": "1"})
            save_raw_json(raw_dir / "filter_options.json", r.content, response_metadata(r, method="POST"))
            options = r.json().get("object") or {}
        title = filters.get("position") or filters.get("keyword") or ""
        if filters.get("position") and filters.get("keyword") and filters["position"] != filters["keyword"]:
            raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 title 参数，不能传入不同值")
        for page in range(1, MAX_PAGES + 1):
            data = {"pageNo": page, "pageSize": PAGE_SIZE, "positionType": "1", "title": title,
                    "corporationNature": _native(options, "corporationNature", filters["company_nature"]) if filters.get("company_nature") else "",
                    "corporationinfo.industry": _native(options, "industry", filters["industry"]) if filters.get("industry") else ""}
            r = client.post(LIST_API, data=data)
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", r.content, response_metadata(r, method="POST"))
            obj = r.json().get("object") or {}; records = obj.get("list") or []; pages += 1
            if not records: break
            dates = []
            for record in records:
                published = str(record.get("startTime") or "")
                try: dates.append(parse_source_datetime(published))
                except ValueError: continue
                if in_range(published, start, end) and (not filters.get("company") or contains(record.get("corporationName") or (record.get("corporationinfo") or {}).get("name"), filters["company"])):
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(x < start for x in dates) else 0
            if older_pages >= 2 or obj.get("lastPage"): break
        results = []
        for record in matched:
            source_id = str(record.get("id") or "unknown"); detail = {}
            try:
                r = client.post(DETAIL_API, data={"recruitmentId": source_id})
                save_raw_json(raw_dir / f"detail_{source_id}.json", r.content, response_metadata(r, method="POST"))
                detail = (r.json().get("object") or {}).get("recruitmentinfo") or {}; detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
            content = str(detail.get("content") or "")
            company = str(record.get("corporationName") or (record.get("corporationinfo") or {}).get("name") or detail.get("corporationName") or "")
            school_detail_url = f"{BASE_URL}/f/recruitmentinfo/show?recruitmentId={source_id}"
            results.append({"公司": company, "岗位": str(record.get("title") or detail.get("title") or ""), "岗位上新时间": str(record.get("startTime") or detail.get("startTime") or ""), "JD": html_to_text(content), "投递方式": extract_application_methods(content, explicit_email=str(detail.get("resumeReceiveEmail") or ""), explicit_url=str(detail.get("onlineApplicationUrl") or "")), "原始链接": school_detail_url})
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

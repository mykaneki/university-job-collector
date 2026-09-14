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
SCHOOL_NAME, TAG = "电子科技大学", "UESTC"
BASE_URL = "https://jiuye.uestc.edu.cn/career"
LIST_API = f"{BASE_URL}/api/home/recruitmentList"
DETAIL_API = f"{BASE_URL}/api/home/recruitment/{{source_id}}"
PAGE_SIZE, MAX_PAGES = 20, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "local", "company_nature": "unsupported", "industry": "unsupported",
    "education": "local", "position_type": "local", "employment_type": "unsupported",
    "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _matches(record: dict[str, object], filters: dict[str, str]) -> bool:
    content = str(record.get("recruitmentContent") or "")
    fields = {
        "company": str(record.get("companyName") or ""),
        "position": str(record.get("title") or record.get("companyName") or ""),
        "keyword": f"{record.get('title') or ''} {record.get('companyName') or ''} {html_to_text(content)}",
        "location": str(record.get("workLocation") or record.get("recruitmentLocation") or ""),
        "education": " ".join(str(x) for x in record.get("educationRequirementLabel") or []),
        "position_type": " ".join(str(x) for x in record.get("occupationCategoryLabel") or []),
    }
    return all(not filters.get(k) or contains(fields[k], filters[k]) for k in fields)


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    matched: list[dict[str, object]] = []
    pages = older_pages = detail_success = detail_failed = 0
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            request = {"type": "ONLINE_RECRUITMENT", "timeRange": "ALL", "pageIndex": page,
                       "pageSize": PAGE_SIZE, "isSearchPage": False, "searchParams": []}
            response = client.post(LIST_API, json=request)
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            records = payload.get("data") or []
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            dates = []
            for record in records:
                published = str(record.get("publishTime") or record.get("recruitmentDate") or "")
                try:
                    dates.append(parse_source_datetime(published))
                except ValueError:
                    continue
                if in_range(published, start, end):
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2 or page >= int((payload.get("page") or {}).get("totalPage") or 1):
                break
        results = []
        for record in matched:
            source_id = str(record.get("id") or "unknown")
            detail = record
            try:
                response = client.get(DETAIL_API.format(source_id=source_id))
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="GET"))
                detail = response.json().get("data") or record
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API.format(source_id=source_id), "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            if not _matches(detail, filters):
                continue
            content = str(detail.get("recruitmentContent") or "")
            title = str(detail.get("title") or detail.get("companyName") or "")
            results.append({
                "公司": str(detail.get("companyName") or ""), "岗位": title,
                "岗位上新时间": str(detail.get("publishTime") or detail.get("recruitmentDate") or ""),
                "JD": html_to_text(content),
                "投递方式": extract_application_methods(content, explicit_email=str(detail.get("contactEmail") or ""), explicit_url=str(detail.get("resumeSubmissionMethod") or detail.get("onlineUrl") or "")),
                "原始链接": f"{BASE_URL}/recruitment/online/{source_id}",
            })
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from common.filters import contains, validate_supported
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "中国人民大学", "RUC"
BASE_URL = "https://career.ruc.edu.cn"
LIST_API = f"{BASE_URL}/open/api/recruitment/user/positions"
PAGE_SIZE, MAX_PAGES = 50, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "local", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _matches(record: dict[str, object], filters: dict[str, str]) -> bool:
    checks = {
        "company": record.get("employerName"),
        "position": record.get("positionName"),
        "location": record.get("workLocation"),
        "company_nature": record.get("corporationNature"),
        "industry": record.get("industry"),
        "education": record.get("educationName"),
    }
    for key, value in filters.items():
        if key == "keyword":
            if not contains(f"{record.get('positionName', '')} {record.get('employerName', '')}", value):
                return False
        elif key in checks and not contains(checks[key], value):
            return False
    return True


def _url(record: dict[str, object]) -> str:
    query = urlencode({
        "positionId": str(record.get("positionId") or ""),
        "recruitmentId": str(record.get("recruitmentId") or ""),
        "employerId": str(record.get("employerId") or ""),
    })
    return f"{BASE_URL}/stu/#/recruit-detail?{query}"


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=json_api_list_restricted_detail", TAG)
    results: list[dict[str, str]] = []
    pages = older_pages = 0
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.post(
                LIST_API,
                json={"current": page, "size": PAGE_SIZE, "formatType": 2},
                headers={"Referer": f"{BASE_URL}/"},
            )
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"人大列表 API 返回失败：{payload.get('msg', payload)}")
            records = (payload.get("data") or {}).get("records") or []
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            normal_dates = []
            for record in records:
                published = str(record.get("createTime") or "")
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    continue
                normal_dates.append(parsed)
                if in_range(published, start, end) and _matches(record, filters):
                    link = _url(record)
                    results.append({
                        "公司": str(record.get("employerName") or ""),
                        "岗位": str(record.get("positionName") or ""),
                        "岗位上新时间": published,
                        "JD": "",
                        "投递方式": "",
                        "原始链接": link,
                    })
                    source_id = str(record.get("positionId") or "unknown")
                    save_error_meta(raw_dir / f"detail_{source_id}.error", {
                        "url": link, "method": "GET", "status": "login_required",
                        "reason": "公开主页可查列表，详情路由进入学生登录态；未绕过权限",
                    })
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2 or len(records) < PAGE_SIZE:
                break
    LAST_STATS = {"strategy": "json_api_list_restricted_detail", "pages": pages, "matched": len(results), "details_success": 0, "details_restricted": len(results)}
    return results

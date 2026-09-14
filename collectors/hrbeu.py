from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "哈尔滨工程大学", "HRBEU"
BASE_URL = "http://job.hrbeu.edu.cn"
ENTRY_URL = f"{BASE_URL}/frontpage/hrbeu/html/recruitmentinfoList.html?type=1"
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


def _native_payload(filters: dict[str, str], options: dict[str, object], page: int) -> dict[str, str | int]:
    position, keyword = filters.get("position", ""), filters.get("keyword", "")
    if position and keyword and position != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 title 参数，不能同时传入不同值")
    return {
        "pageNo": page, "pageSize": PAGE_SIZE, "positionType": "1",
        "title": position or keyword,
        "corporationNature": _native_value(options, "corporationNature", filters["company_nature"])
        if filters.get("company_nature") else "",
        "corporationinfo.industry": _native_value(options, "industry", filters["industry"])
        if filters.get("industry") else "",
    }


def _local_match(record: dict[str, object], filters: dict[str, str]) -> bool:
    return (
        (not filters.get("company") or contains(record.get("corporationName"), filters["company"]))
        and (not filters.get("education") or contains(record.get("education"), filters["education"]))
    )


def _simple_item(record: dict[str, object]) -> dict[str, str]:
    return {
        "公司": str(record.get("corporationName") or ""),
        "岗位": str(record.get("title") or ""),
        "岗位上新时间": str(record.get("startTime") or ""),
        "JD": "", "投递方式": "",
        "原始链接": urljoin(BASE_URL, str(record.get("url") or "")),
    }


def _is_login_page(url: str, html: str) -> bool:
    if urlparse(url).path.rstrip("/") == "/a/login":
        return True
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return bool(
        soup.select_one(".login")
        and (
            "登录" in title
            or soup.select_one("a[href*='/a/login_student'], a[href*='/a/login_teacher'], form[action*='/a/login']")
        )
    )


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=json_api_login_restricted_detail", TAG)
    pages = older_pages = detail_restricted = detail_failed = 0
    matched: list[dict[str, object]] = []
    with HttpClient() as client:
        options: dict[str, object] = {}
        if filters.get("company_nature") or filters.get("industry"):
            response = client.post(SEARCH_API, data={"positionType": "1"})
            save_raw_json(raw_dir / "filter_options.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("state") != 1:
                raise RuntimeError(f"{SCHOOL_NAME}筛选选项 API 返回失败")
            options = payload.get("object") or {}
        for page in range(1, MAX_PAGES + 1):
            response = client.post(LIST_API, data=_native_payload(filters, options, page))
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("state") != 1:
                raise RuntimeError(f"{SCHOOL_NAME}列表 API 返回失败：{payload.get('msg', payload)}")
            obj, records = payload.get("object") or {}, (payload.get("object") or {}).get("list") or []
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
                    LOGGER.warning("[%s] invalid publish time id=%s value=%r", TAG, record.get("id"), published)
                    continue
                if str(record.get("topFlag") or "0") != "1":
                    normal_dates.append(parsed)
                if in_range(published, start, end) and _local_match(record, filters):
                    matched.append(record)
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2 or bool(obj.get("lastPage")):
                break
        for record in matched:
            source_id = str(record.get("id") or "unknown")
            try:
                response = client.post(DETAIL_API, data={"recruitmentId": source_id})
                final_url, content_type = str(response.url), response.headers.get("content-type", "")
                if _is_login_page(final_url, response.text):
                    save_raw_html(
                        raw_dir / f"detail_{source_id}.restricted.html", response.content,
                        response_metadata(response, method="POST") | {"source_url": DETAIL_API, "access": "login_required"},
                    )
                    detail_restricted += 1
                    LOGGER.info("[%s] detail login_required id=%s", TAG, source_id)
                else:
                    if "text/html" in content_type.lower():
                        save_raw_html(
                            raw_dir / f"detail_{source_id}.html", response.content,
                            response_metadata(response, method="POST") | {"source_url": DETAIL_API, "access": "unexpected_html"},
                        )
                    else:
                        save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="POST"))
                    detail_failed += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
    results = [_simple_item(record) for record in matched]
    LAST_STATS = {
        "strategy": "json_api_login_restricted_detail", "pages": pages, "matched": len(results),
        "details_success": 0, "details_restricted": detail_restricted, "details_failed": detail_failed,
    }
    return results

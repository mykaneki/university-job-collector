from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import SHANGHAI, in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "武汉理工大学", "WUT"
BASE_URL = "https://scc.whut.edu.cn"
LOCK_API = f"{BASE_URL}/mobile.php/Wx/getselock"
LIST_API = f"{BASE_URL}/mobile.php/enrollment/getlist"
DETAIL_API = f"{BASE_URL}/mobile.php/enrollment/detail"
SCHOOL_ID = "b525083d-b83c-4c7e-892f-29909421d961"
PAGE_SIZE, MAX_PAGES = 10, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "native", "keyword": "native", "location": "local",
    "company_nature": "unsupported", "industry": "unsupported", "education": "unsupported",
    "position_type": "unsupported", "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _published(record: dict[str, object]) -> str:
    return datetime.fromtimestamp(int(record.get("addtime") or 0), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _common() -> dict[str, object]:
    return {"login_user_id": 1, "login_admin_school_code": 10497, "school_id": SCHOOL_ID, "login_admin_school_id": SCHOOL_ID}


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    title, keyword = filters.get("position", ""), filters.get("keyword", "")
    if title and keyword and title != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 keywords 参数")
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    candidates: list[dict[str, object]] = []
    with HttpClient() as client:
        lock_response = client.get(LOCK_API, params={"login_user_id": 1, "login_admin_school_id": SCHOOL_ID, "login_admin_school_code": 10497})
        save_raw_json(raw_dir / "session_bootstrap.json", lock_response.content, response_metadata(lock_response, method="GET"))
        lock_payload = lock_response.json()
        if lock_payload.get("code") != 0 or not (lock_payload.get("data") or {}).get("lock"):
            raise RuntimeError(f"{SCHOOL_NAME}动态 session auth 获取失败：{lock_payload.get('msg')}")
        headers = {"auth": str(lock_payload["data"]["lock"])}
        for page in range(1, MAX_PAGES + 1):
            response = client.post(LIST_API, json={"keywords": title or keyword, "type": "", "page": page, "size": PAGE_SIZE} | _common(), headers=headers)
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"{SCHOOL_NAME}列表 API 失败：{payload.get('msg')}")
            data = payload.get("data") or {}
            records = data.get("list") or []
            pages += 1
            if not records:
                break
            dates = []
            for record in records:
                published = _published(record)
                if str(record.get("istop") or "2") != "1":
                    dates.append(parse_source_datetime(published))
                if in_range(published, start, end):
                    candidates.append(record)
            older_pages = older_pages + 1 if dates and all(d < start for d in dates) else 0
            if older_pages >= 2 or page >= int(data.get("allpage") or MAX_PAGES):
                break
        results = []
        for record in candidates:
            source_id = str(record.get("id") or "unknown")
            detail = record
            try:
                response = client.post(DETAIL_API, json={"id": source_id} | _common(), headers=headers)
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="POST"))
                payload = response.json()
                if payload.get("code") != 0:
                    raise RuntimeError(str(payload.get("msg") or "detail code != 0"))
                detail = payload.get("data") or record
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
            company_info = detail.get("com_id_info") or {}
            company = str(detail.get("com_id_name") or (company_info.get("name") if isinstance(company_info, dict) else "") or "")
            location = " ".join(str(detail.get(k) or "") for k in ("province_id_name", "city_id_name", "address"))
            if filters.get("company") and not contains(company, filters["company"]):
                continue
            if filters.get("location") and not contains(location, filters["location"]):
                continue
            body = str(detail.get("remarks") or "")
            url = f"{BASE_URL}/#/recruitmentInformation/notice/noticeDetail/{source_id}"
            results.append({"\u516c\u53f8": company, "\u5c97\u4f4d": str(detail.get("title") or record.get("title") or ""), "\u5c97\u4f4d\u4e0a\u65b0\u65f6\u95f4": _published(record), "JD": html_to_text(body), "\u6295\u9012\u65b9\u5f0f": extract_application_methods(body, explicit_email=str(detail.get("send_email") or ""), explicit_url=str(detail.get("httpurl") or "")), "\u539f\u59cb\u94fe\u63a5": url})
    LAST_STATS = {"strategy": "http_session_json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

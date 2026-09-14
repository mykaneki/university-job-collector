from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from common.filters import validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "浙江大学", "ZJU"
BASE_URL = "https://www.career.zju.edu.cn"
LIST_API = f"{BASE_URL}/web/wsjysc/lbxq/getTzggPageList"
DETAIL_API = f"{BASE_URL}/web/wsjysc/lbxq/getTzggxq/{{source_id}}"
LMID = "A188D7448980110BE055134CA39C9BD7"
PAGE_SIZE, MAX_PAGES = 15, 200
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "native", "keyword": "native",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    title = filters.get("position", "")
    keyword = filters.get("keyword", "")
    if title and keyword and title != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 keyword 参数，不能同时传入不同值")
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = detail_success = detail_failed = 0
    records: list[dict[str, object]] = []
    with HttpClient() as client:
        total_pages = 1
        for page in range(1, MAX_PAGES + 1):
            body = {
                "current": page, "jbdd": "", "jssj": end.strftime("%Y-%m-%d"),
                "keyword": title or keyword, "kssj": start.strftime("%Y-%m-%d"),
                "lmid": LMID, "size": PAGE_SIZE,
            }
            response = client.post(LIST_API, json=body)
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("code") != 200:
                raise RuntimeError(f"{SCHOOL_NAME}列表 API 失败：{payload.get('message')}")
            result = payload.get("result") or {}
            rows = result.get("records") or []
            records.extend(row for row in rows if isinstance(row, dict))
            total_pages = min(int(result.get("pages") or 1), MAX_PAGES)
            pages += 1
            LOGGER.info("[%s] page=%d/%d records=%d", TAG, page, total_pages, len(rows))
            if page >= total_pages:
                break
        simple = []
        for row in records:
            source_id = str(row.get("xwid") or "unknown")
            detail = row
            try:
                response = client.get(DETAIL_API.format(source_id=source_id))
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="GET"))
                payload = response.json()
                if payload.get("code") != 200:
                    raise RuntimeError(str(payload.get("message") or "detail code != 200"))
                detail = payload.get("result") or row
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API.format(source_id=source_id), "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            content = str(detail.get("xwnr") or "")
            simple.append({
                "公司": "", "岗位": str(detail.get("xwbt") or row.get("xwbt") or ""),
                "岗位上新时间": str(detail.get("fbsj") or row.get("fbsj") or ""),
                "JD": html_to_text(content), "投递方式": extract_application_methods(content),
                "原始链接": f"{BASE_URL}/notification/detail?xwid={source_id}",
            })
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(simple), "details_success": detail_success, "details_failed": detail_failed}
    return simple

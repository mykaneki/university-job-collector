from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "哈尔滨工业大学", "HIT"
BASE_URL = "https://career.hit.edu.cn"
LIST_API = f"{BASE_URL}/zhxy-xszyfzpt/zpxx/getZpxxList"
DETAIL_API = f"{BASE_URL}/zhxy-xszyfzpt/zpxx/getZpxxXqAndSetLlcsById"
DETAIL_PAGE = f"{BASE_URL}/zhxy-xszyfzpt/zpxx/zpxxxq"
PAGE_SIZE, MAX_PAGES = 20, 200
SUPPORTED_FILTERS = {
    "company": "native", "position": "native", "keyword": "native",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "local", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _query(page: int, filters: dict[str, str]) -> dict[str, object]:
    position, keyword = filters.get("position", ""), filters.get("keyword", "")
    if position and keyword and position != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 zpxxmc 参数，不能同时传入不同值")
    return {
        "zpxxmc": position or keyword, "dwmc": filters.get("company", ""),
        "xz": "", "dwxz": "", "dwhy": "", "sheng": "", "shi": "", "qu": "", "fbsj": "",
        "bkxlyq": "", "ssxlyq": "", "bsxlyq": "", "zplx": "", "dwgm": "",
        "sfcxcy": "", "sfwbqqy": "", "sfgzwssyq": "", "sfgfdw": "",
        "lm1": "", "lm2": "", "lm3": "", "lm4": "", "lm5": "", "lm6": "",
        "lx": 1, "page": page, "pageSize": PAGE_SIZE,
        "take": PAGE_SIZE, "skip": (page - 1) * PAGE_SIZE, "sort": [],
    }


def _detail_text(detail: dict[str, object], positions: list[dict[str, object]]) -> str:
    blocks = [str(detail.get(key) or "") for key in ("zpyq", "zwyq")]
    for row in positions:
        name = str(row.get("gwmc") or "")
        description = str(row.get("zwyq") or row.get("zpyq") or "")
        if name or description:
            blocks.append(f"<h3>{name}</h3>{description}")
    return "\n".join(value for value in blocks if value)


def _matches(record: dict[str, object], detail: dict[str, object], filters: dict[str, str]) -> bool:
    fields = {
        "location": f"{record.get('gzdd') or ''} {detail.get('gzdd') or ''} {detail.get('gzddmc') or ''}",
        "company_nature": detail.get("dwxz") or record.get("dwxz") or "",
        "industry": detail.get("dwhy") or record.get("dwhy") or "",
        "education": " ".join(str(detail.get(key) or "") for key in ("xlyq", "bkxlyq", "ssxlyq", "bsxlyq")),
    }
    return all(not filters.get(key) or contains(value, filters[key]) for key, value in fields.items())


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=json_api", TAG)
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, object]] = []
    results: list[dict[str, str]] = []
    with HttpClient() as client:
        total = None
        for page in range(1, MAX_PAGES + 1):
            response = client.post(LIST_API, data={"info": json.dumps(_query(page, filters), ensure_ascii=False)})
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            if payload.get("isSuccess") is not True:
                raise RuntimeError(str(payload.get("msg") or f"{SCHOOL_NAME}列表 API 返回失败"))
            module = payload.get("module") or {}
            records = module.get("data") or []
            total = int(module.get("total") or 0)
            pages += 1
            LOGGER.info("[%s] page=%d records=%d total=%d", TAG, page, len(records), total)
            if not records:
                break
            dates = []
            for record in records:
                published = str(record.get("fbsj") or "")
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    continue
                dates.append(parsed)
                if in_range(published, start, end):
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2 or page * PAGE_SIZE >= total:
                break
        for record in matched:
            source_id = str(record.get("zpxxid") or "unknown")
            detail: dict[str, object] = {}
            positions: list[dict[str, object]] = []
            try:
                response = client.post(DETAIL_API, data={"info": json.dumps({"zpxxid": source_id})})
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="POST"))
                payload = response.json()
                if payload.get("isSuccess") is not True:
                    raise RuntimeError(str(payload.get("msg") or "detail isSuccess != true"))
                module = payload.get("module") or {}
                detail = module.get("zpxx_xq") or {}
                positions = [row for row in module.get("zpxx_zpzw") or [] if isinstance(row, dict)]
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
            if not _matches(record, detail, filters):
                continue
            content = _detail_text(detail, positions)
            encoded = base64.b64encode(source_id.encode()).decode()
            explicit = str(detail.get("jltdfs") or record.get("jltdfs") or "")
            published = str(detail.get("fbsj") or record.get("fbsj") or "")
            if not in_range(published, start, end):
                continue
            results.append({
                "公司": str(detail.get("dwmc") or record.get("dwmc") or ""),
                "岗位": str(detail.get("zpxxmc") or record.get("zpxxmc") or ""),
                "岗位上新时间": published,
                "JD": html_to_text(content),
                "投递方式": extract_application_methods(content, explicit_email=explicit if "@" in explicit else "", explicit_url=explicit if explicit.startswith(("http://", "https://")) else ""),
                "原始链接": f"{DETAIL_PAGE}?{urlencode({'id': encoded})}",
            })
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

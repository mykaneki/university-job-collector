from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from Crypto.Cipher import AES

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import SHANGHAI, in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "对外经济贸易大学", "UIBE"
BASE_URL = "https://career.uibe.edu.cn"
ENTRY_URL = f"{BASE_URL}/front/channel.jspa?channelId=764&parentId=625"
LIST_API = f"{BASE_URL}/front/zp_query/zpxxQuery.do"
PAGE_SIZE, MAX_PAGES = 20, 200
KEY, IV = b"abcdef0123456789", b"0123456789abcdef"
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local", "location": "local",
    "company_nature": "unsupported", "industry": "unsupported", "education": "unsupported",
    "position_type": "unsupported", "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _decrypt(content: bytes) -> dict[str, object]:
    ciphertext = base64.b64decode(b"".join(content.split()))
    plaintext = AES.new(KEY, AES.MODE_CBC, IV).decrypt(ciphertext).rstrip(b"\x00")
    return json.loads(plaintext.decode("utf-8"))


def _published(record: dict[str, object]) -> str:
    timestamp_ms = int(record.get("updateTime") or record.get("createTime") or 0)
    return datetime.fromtimestamp(timestamp_ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _matches(record: dict[str, object], filters: dict[str, str]) -> bool:
    positions = " ".join(str(item.get("zwmc") or item.get("name") or "") for item in (record.get("xqzwList") or []) if isinstance(item, dict))
    title = f"{record.get('title', '')} {positions}"
    return (
        (not filters.get("company") or contains(record.get("dwmc"), filters["company"]))
        and
        (not filters.get("position") or contains(title, filters["position"]))
        and (not filters.get("keyword") or contains(f"{title} {record.get('dwmc', '')}", filters["keyword"]))
        and (not filters.get("location") or contains(record.get("dwszddm"), filters["location"]))
    )


def _detail_fields(html: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "lxml")
    content_node = soup.select_one(".explain .post")
    content_html = content_node.decode_contents() if content_node else ""
    unit = soup.select_one(".recruit .unit")
    company = unit.get_text(" ", strip=True).replace("招聘单位：", "", 1).strip() if unit else ""
    return company, html_to_text(content_html), extract_application_methods(content_html)


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=encrypted_json_api", TAG)
    matched: list[dict[str, object]] = []
    pages = older_pages = detail_success = detail_failed = 0
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.post(
                LIST_API,
                data={"xxlx": "1", "curPage": page, "gzcs": "", "dwhydm": "", "dwxzdm": "", "dwmc": ""},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": ENTRY_URL},
            )
            save_raw_json(raw_dir / f"list_page_{page:03d}.txt", response.content, response_metadata(response, method="POST"))
            payload = _decrypt(response.content)
            save_raw_json(
                raw_dir / f"list_page_{page:03d}.decoded.json",
                json.dumps(payload, ensure_ascii=False).encode(),
                {"url": str(response.url), "method": "DERIVED", "source": f"list_page_{page:03d}.txt", "content_type": "application/json"},
            )
            if payload.get("msg") != "Y":
                raise RuntimeError(f"外经贸列表 API 返回失败：{payload.get('msg')}")
            records = payload.get("data") or []
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            dates = []
            for record in records:
                try:
                    published = _published(record)
                    dates.append(parse_source_datetime(published))
                except (TypeError, ValueError, OSError):
                    continue
                if in_range(published, start, end) and _matches(record, filters):
                    record = dict(record)
                    record["_published"] = published
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2 or page >= int(payload.get("pageCount") or 1):
                break
        results = []
        for record in matched:
            source_id = str(record.get("tid") or "unknown")
            detail_url = f"{BASE_URL}/front/zpxx.jspa?tid={source_id}"
            company, jd, application = str(record.get("dwmc") or ""), "", ""
            try:
                response = client.get(detail_url)
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                detail_company, jd, application = _detail_fields(response.text)
                company = detail_company or company
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                LOGGER.error("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": detail_url, "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            results.append({"公司": company, "岗位": str(record.get("title") or ""), "岗位上新时间": str(record.get("_published") or ""), "JD": jd, "投递方式": application, "原始链接": detail_url})
    LAST_STATS = {"strategy": "encrypted_json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

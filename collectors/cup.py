from __future__ import annotations

import base64
import logging
import re
import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "中国石油大学（北京）", "CUP"
BASE_URL = "https://career.cup.edu.cn"
ENTRY_URL = f"{BASE_URL}/campus"
PAGE_URL = f"{BASE_URL}/campus/index/domain/cup/city//page/{{page}}"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}
BLOCK_RE = re.compile(
    r'unzip\("([A-Za-z0-9+/=]+)"\)\.substr\((\d+)\)\)\.substr\((\d+)\)'
)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")


def _decoded_blocks(html: str) -> list[str]:
    blocks = []
    for encoded, inflated_offset, decoded_offset in BLOCK_RE.findall(html):
        inflated = zlib.decompress(base64.b64decode(encoded)).decode("utf-8")
        decoded = base64.b64decode(inflated[int(inflated_offset):]).decode("utf-8")
        blocks.append(decoded[int(decoded_offset):])
    return blocks


def _list_records(html: str) -> list[dict[str, object]]:
    blocks = _decoded_blocks(html)
    soup = BeautifulSoup("\n".join(blocks), "lxml")
    records = []
    for row in soup.select("ul.infoList"):
        link = row.select_one("a[href*='/campus/view/id/']")
        match = DATE_RE.search(row.get_text(" ", strip=True))
        if link and match:
            records.append({
                "title": link.get_text(" ", strip=True),
                "url": urljoin(BASE_URL, str(link.get("href") or "")),
                "published": match.group(0),
                "is_top": bool(row.select_one(".status-ding")),
            })
    return records


def _detail_fields(html: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "lxml")
    company_node = soup.select_one("a.name")
    company = company_node.get_text(" ", strip=True) if company_node else ""
    blocks = _decoded_blocks(html)
    content_html = "\n".join(blocks)
    return company, html_to_text(content_html), extract_application_methods(content_html)


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=get_html_embedded", TAG)
    matched: list[dict[str, object]] = []
    pages = older_pages = detail_success = detail_failed = 0
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            url = ENTRY_URL if page == 1 else PAGE_URL.format(page=page)
            response = client.get(url)
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET"))
            records = _list_records(response.text)
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            normal_dates = []
            for record in records:
                published = str(record["published"])
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    continue
                if not record["is_top"]:
                    normal_dates.append(parsed)
                title = str(record["title"])
                if in_range(published, start, end) and (not filters.get("position") or contains(title, filters["position"])) and (not filters.get("keyword") or contains(title, filters["keyword"])):
                    matched.append(record)
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2:
                break
        results = []
        for record in matched:
            detail_url = str(record["url"])
            source_id_match = re.search(r"/id/(\d+)", detail_url)
            source_id = source_id_match.group(1) if source_id_match else "unknown"
            company = jd = application = ""
            try:
                response = client.get(detail_url)
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                company, jd, application = _detail_fields(response.text)
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                LOGGER.error("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": detail_url, "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            if filters.get("company") and not contains(company, filters["company"]):
                continue
            results.append({"公司": company, "岗位": str(record["title"]), "岗位上新时间": str(record["published"]), "JD": jd, "投递方式": application, "原始链接": detail_url})
    LAST_STATS = {"strategy": "get_html_embedded", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

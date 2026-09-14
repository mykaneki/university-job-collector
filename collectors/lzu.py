from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from common.browser import ChromeCdpSession
from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "兰州大学", "LZU"
BASE_URL = "https://job.lzu.edu.cn"
LIST_URL = f"{BASE_URL}/html/74/article/list/list_{{page}}.html"
ENTRY_URL = LIST_URL.format(page=1)
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "local", "keyword": "local",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _parse_list(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(".lmain_item_con > ul")
    if root is None:
        return []
    records = []
    for row in root.find_all("li", recursive=False):
        anchor = row.find("a", href=True)
        date_node = row.find("span")
        if not isinstance(anchor, Tag) or not isinstance(date_node, Tag):
            continue
        published = date_node.get_text(" ", strip=True)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
            continue
        records.append({
            "title": anchor.get_text(" ", strip=True),
            "published": published,
            "url": urljoin(BASE_URL, str(anchor.get("href") or "")),
        })
    return records


def _detail_content(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    if url.startswith(BASE_URL):
        node = soup.select_one("#news #content") or soup.select_one("#content")
    else:
        node = soup.select_one("#js_content") or soup.select_one(".rich_media_content") or soup.select_one("article")
    return str(node) if isinstance(node, Tag) else ""


def _is_school_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == "job.lzu.edu.cn" and parsed.path.startswith("/html/74/article/")


def _local_match(record: dict[str, str], filters: dict[str, str]) -> bool:
    position, keyword = filters.get("position", ""), filters.get("keyword", "")
    return (not position or contains(record["title"], position)) and (not keyword or contains(record["title"], keyword))


def _raw_id(url: str) -> str:
    match = re.search(r"/(\d+)\.html(?:\?|$)", url)
    return match.group(1) if match else hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _browser_metadata(response: object, requested_url: str, final_url: str) -> dict[str, object]:
    status = getattr(response, "status", 0) if response is not None else 0
    headers = getattr(response, "headers", {}) if response is not None else {}
    return {
        "url": final_url,
        "requested_url": requested_url,
        "method": "GET",
        "status_code": status,
        "content_type": headers.get("content-type", "") if isinstance(headers, dict) else "",
        "transport": "visible_chrome_cdp",
        "raw_representation": "rendered_dom",
    }


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=visible_chrome_cdp_html", TAG)
    pages = older_pages = detail_success = detail_failed = details_with_jd = skipped_external_links = 0
    matched: list[dict[str, str]] = []
    with ChromeCdpSession() as chrome:
        page = chrome.new_page()
        for page_no in range(1, MAX_PAGES + 1):
            url = LIST_URL.format(page=page_no)
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector(".lmain_item_con > ul", timeout=20_000)
            rendered = page.content()
            save_raw_html(
                raw_dir / f"list_page_{page_no:03d}.html",
                rendered,
                _browser_metadata(response, url, page.url),
            )
            records = _parse_list(rendered)
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page_no, len(records))
            if not records:
                break
            parsed_dates = []
            for record in records:
                try:
                    parsed_dates.append(parse_source_datetime(record["published"]))
                except ValueError:
                    LOGGER.warning("[%s] invalid publish time url=%s value=%r", TAG, record["url"], record["published"])
                    continue
                if in_range(record["published"], start, end) and _local_match(record, filters):
                    if _is_school_detail_url(record["url"]):
                        matched.append(record)
                    else:
                        skipped_external_links += 1
                        LOGGER.info("[%s] skip external detail url=%s", TAG, record["url"])
            older_pages = older_pages + 1 if parsed_dates and all(value < start for value in parsed_dates) else 0
            if older_pages >= 2:
                break

        results = []
        for record in matched:
            source_id = _raw_id(record["url"])
            content = ""
            try:
                response = page.goto(record["url"], wait_until="domcontentloaded", timeout=30_000)
                rendered = page.content()
                save_raw_html(
                    raw_dir / f"detail_{source_id}.html",
                    rendered,
                    _browser_metadata(response, record["url"], page.url),
                )
                content = _detail_content(rendered, page.url)
                if not content:
                    raise RuntimeError("公开详情页未找到可确定正文")
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                content = ""
                save_error_meta(
                    raw_dir / f"detail_{source_id}.error",
                    {"url": record["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}", "transport": "visible_chrome_cdp"},
                )
                LOGGER.warning("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
            text = html_to_text(content)
            if text:
                details_with_jd += 1
            results.append({
                "公司": "", "岗位": record["title"], "岗位上新时间": record["published"],
                "JD": text, "投递方式": extract_application_methods(content),
                "原始链接": record["url"],
            })
    LAST_STATS = {
        "strategy": "visible_chrome_cdp_html", "pages": pages, "matched": len(results),
        "details_success": detail_success, "details_with_jd": details_with_jd,
        "details_failed": detail_failed, "skipped_external_links": skipped_external_links,
    }
    return results

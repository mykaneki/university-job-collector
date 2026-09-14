from __future__ import annotations

import logging
import re
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
SCHOOL_NAME, TAG = "南开大学", "Nankai"
BASE_URL = "https://career.nankai.edu.cn"
ENTRY_URL = f"{BASE_URL}/correcruit/index.html"
PAGE_URL = f"{BASE_URL}/correcruit/index/p/{{page}}.html"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "native", "location": "native",
    "company_nature": "unsupported", "industry": "unsupported", "education": "local",
    "position_type": "native", "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _option_map(html: str, name: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    return {
        option.get_text(" ", strip=True): str(option.get("value") or "")
        for option in soup.select(f"select[name='{name}'] option[value]")
        if str(option.get("value") or "") != "0"
    }


def _native_form(entry_html: str, filters: dict[str, str]) -> dict[str, str]:
    location_map = _option_map(entry_html, "sel_area")
    category_map = _option_map(entry_html, "sel_cate")

    def mapped(key: str, options: dict[str, str]) -> str:
        value = filters.get(key, "")
        if not value:
            return "0"
        if value not in options:
            choices = "、".join(options)
            raise ValueError(f"南开大学{key} 不存在站内选项 {value!r}，可用值：{choices}")
        return options[value]

    return {
        "type": "0",
        "keywords": filters.get("keyword", ""),
        "sel_area": mapped("location", location_map),
        "sel_cate": mapped("position_type", category_map),
    }


def _list_records(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    records = []
    for row in soup.select(".newslist.zhuanlan .zl1 > .content > ul > li"):
        link = row.select_one(".title1 a[href*='/correcruit/content/id/']")
        year_month = row.select_one(".date .year")
        day = row.select_one(".date .day")
        if not (link and year_month and day):
            continue
        company_link = row.select_one(".company a[href*='/company/index/id/']")
        company = company_link.get_text(" ", strip=True).strip("【】") if company_link else ""
        company_block = row.select_one(".company")
        metadata = company_block.get_text(" ", strip=True) if company_block else ""
        records.append({
            "title": link.get_text(" ", strip=True),
            "company": company,
            "metadata": metadata,
            "published": f"{year_month.get_text(strip=True)}.{day.get_text(strip=True)}".replace(".", "-"),
            "url": urljoin(BASE_URL, str(link.get("href") or "")),
        })
    return records


def _matches(record: dict[str, str], filters: dict[str, str]) -> bool:
    return (
        (not filters.get("company") or contains(record["company"], filters["company"]))
        and (not filters.get("position") or contains(record["title"], filters["position"]))
        and (not filters.get("education") or contains(record["metadata"], filters["education"]))
    )


def _detail_fields(html: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one(".newslist.zhuanlan .zl1 > .content")
    if not content:
        return "", "", ""
    company_node = content.select_one(".top .company a")
    company = company_node.get_text(" ", strip=True) if company_node else ""
    body = content.select_one(".zpnr")
    jd = html_to_text(body.decode_contents()) if body else ""
    top = content.select_one(".top")
    application = extract_application_methods(top.decode_contents() if top else "")
    return company, jd, application


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=post_html", TAG)
    matched: list[dict[str, str]] = []
    pages = older_pages = detail_success = detail_failed = 0
    with HttpClient() as client:
        entry = client.get(ENTRY_URL)
        form = _native_form(entry.text, filters)
        for page in range(1, MAX_PAGES + 1):
            url = ENTRY_URL if page == 1 else PAGE_URL.format(page=page)
            response = client.post(url, data=form, headers={"Referer": ENTRY_URL})
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="POST"))
            records = _list_records(response.text)
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            dates = []
            for record in records:
                try:
                    parsed = parse_source_datetime(record["published"])
                except ValueError:
                    continue
                dates.append(parsed)
                if in_range(record["published"], start, end) and _matches(record, filters):
                    matched.append(record)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2:
                break

        results = []
        for record in matched:
            detail_url = record["url"]
            match = re.search(r"/id/(\d+)", detail_url)
            source_id = match.group(1) if match else "unknown"
            company, jd, application = record["company"], "", ""
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
            results.append({"公司": company, "岗位": record["title"], "岗位上新时间": record["published"], "JD": jd, "投递方式": application, "原始链接": detail_url})
    LAST_STATS = {"strategy": "post_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

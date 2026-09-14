from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html
from common.time import SHANGHAI

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "吉林大学", "JLU"
BASE_URL = "https://jdjyw.jlu.edu.cn"
LIST_URL = f"{BASE_URL}/portal/jyzp/recruit/list"
PAGE_SIZE, MAX_PAGES = 12, 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "native", "keyword": "native",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _source_datetime(value: str) -> datetime:
    cleaned = " ".join(value.replace("\xa0", " ").split())
    for fmt in ("%b %d, %Y %I:%M:%S %p", "%b %d, %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            pass
    raise ValueError(f"无法解析吉大时间 {value!r}")


def _field(container: Tag | None, label: str) -> str:
    if container is None:
        return ""
    for cell in container.find_all(["th", "td", "span", "div"]):
        if cell.get_text(" ", strip=True).rstrip("：:") == label:
            sibling = cell.find_next_sibling()
            if sibling:
                return sibling.get_text(" ", strip=True)
    text = container.get_text(" | ", strip=True)
    match = re.search(rf"{re.escape(label)}\s*[|:：]\s*([^|]+)", text)
    return match.group(1).strip() if match else ""


def _list_records(content: bytes) -> list[dict[str, str]]:
    soup = BeautifulSoup(content, "lxml")
    records = []
    for anchor in soup.select("a[href*='/portal/jyzp/recruit/details?id=']"):
        item = anchor.find_parent("li")
        if item is None:
            continue
        text = item.get_text(" | ", strip=True)
        date_match = re.search(r"创建时间：([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}|\d{4}-\d{1,2}-\d{1,2})", text)
        if not date_match:
            continue
        records.append({
            "title": anchor.get_text(" ", strip=True).removeprefix("【置顶】").removeprefix("[置顶]").strip(),
            "url": urljoin(BASE_URL, str(anchor.get("href") or "")),
            "published": date_match.group(1),
            "top": "1" if "置顶" in anchor.get_text() else "0",
            "company": _field(item, "单位名称"),
            "company_nature": _field(item, "单位性质"),
            "industry": _field(item, "单位行业"),
            "location": _field(item, "单位地址"),
        })
    return records


def _detail(content: bytes) -> dict[str, str]:
    soup = BeautifulSoup(content, "lxml")
    body = soup.select_one(".m-detail-content__container")
    info = soup.select_one(".m-detail-container__info")
    published = ""
    if info:
        match = re.search(r"发布时间：(.+?)(?:\s+点击量|$)", info.get_text(" ", strip=True))
        published = match.group(1).strip() if match else ""
    company_table = next((table for table in soup.find_all("table") if "单位名称" in table.get_text()), None)
    content_html = str(body) if body else ""
    return {
        "title": soup.select_one(".m-detail-container__title").get_text(" ", strip=True) if soup.select_one(".m-detail-container__title") else "",
        "published": published,
        "company": _field(company_table, "单位名称"),
        "location": _field(company_table, "单位地址"),
        "company_nature": _field(company_table, "单位性质"),
        "industry": _field(company_table, "单位行业"),
        "content": content_html,
        "application": extract_application_methods(content_html),
    }


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=get_html", TAG)
    title, keyword = filters.get("position", ""), filters.get("keyword", "")
    if title and keyword and title != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 keyword 参数，不能同时传入不同值")
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, str]] = []
    results: list[dict[str, str]] = []
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.get(LIST_URL, params={"type": "2", "pageNo": page, "pageSize": PAGE_SIZE, "keyword": title or keyword})
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET"))
            records = _list_records(response.content)
            pages += 1
            LOGGER.info("[%s] page=%d records=%d", TAG, page, len(records))
            if not records:
                break
            normal_dates = [_source_datetime(row["published"]) for row in records if row["top"] != "1"]
            for row in records:
                published = _source_datetime(row["published"])
                if not start <= published <= end:
                    continue
                if any(filters.get(key) and not contains(row.get(key), filters[key]) for key in ("company", "location", "company_nature", "industry")):
                    continue
                matched.append(row)
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2:
                break
        for row in matched:
            source_id = parse_qs(urlparse(row["url"]).query).get("id", ["unknown"])[0]
            detail: dict[str, str] = {}
            try:
                response = client.get(row["url"])
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                detail = _detail(response.content)
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": row["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            published = detail.get("published") or row["published"]
            if detail.get("published"):
                try:
                    detailed_time = _source_datetime(detail["published"])
                except ValueError:
                    published = row["published"]
                else:
                    if not start <= detailed_time <= end:
                        continue
            results.append({
                "公司": detail.get("company") or row["company"],
                "岗位": detail.get("title") or row["title"],
                "岗位上新时间": published,
                "JD": html_to_text(detail.get("content", "")),
                "投递方式": detail.get("application", ""),
                "原始链接": row["url"],
            })
    LAST_STATS = {"strategy": "get_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

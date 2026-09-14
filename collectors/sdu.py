from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "山东大学", "SDU"
BASE_URL = "https://jobcareer.sdu.edu.cn"
LIST_URL = f"{BASE_URL}/eweb/jygl/index.so?modcode=null&subsyscode=zpfw&type=ssoSearchZxzp&xxlb=5100"
DETAIL_URL = f"{BASE_URL}/eweb/jygl/index.so?modcode=jygl_zpxxck&subsyscode=zpfw&rklx=jyw&lmxhV=0402&type=ssoZxzpView&id={{source_id}}"
MAX_PAGES = 100
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "local", "keyword": "local",
    "location": "local", "company_nature": "local", "industry": "unsupported",
    "education": "local", "position_type": "local", "employment_type": "local",
    "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    result = []
    for li in soup.select(".moreListR-long > ul > li"):
        link = li.select_one("a[onclick*='viewZpxx']")
        if not isinstance(link, Tag):
            continue
        match = re.search(r"viewZpxx\('([^']+)'", str(link.get("onclick") or ""))
        cells = [a.get_text(" ", strip=True) for a in li.select("a")]
        if match and len(cells) >= 3:
            result.append({"id": match.group(1), "date": cells[0], "title": cells[1], "nature": cells[2]})
    return result


def _field(soup: BeautifulSoup, label: str) -> str:
    cell = soup.find("td", string=lambda value: bool(value and label in value.strip()))
    sibling = cell.find_next_sibling("td") if isinstance(cell, Tag) else None
    return sibling.get_text(" ", strip=True) if isinstance(sibling, Tag) else ""


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, str]] = []
    with HttpClient() as client:
        next_url = LIST_URL
        for page in range(1, MAX_PAGES + 1):
            response = client.get(next_url)
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET"))
            html = response.text
            rows = _rows(html)
            pages += 1
            normal_dates = []
            for row in rows:
                try:
                    parsed = parse_source_datetime(row["date"])
                except ValueError:
                    continue
                normal_dates.append(parsed)
                if in_range(row["date"], start, end):
                    matched.append(row)
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            soup = BeautifulSoup(html, "lxml")
            nxt = soup.find("a", string=lambda value: bool(value and "下一页" in value))
            if older_pages >= 2 or not isinstance(nxt, Tag) or not nxt.get("href"):
                break
            next_url = urljoin(BASE_URL, str(nxt["href"]))
        simple = []
        for row in matched:
            source_id = row["id"]
            try:
                response = client.get(DETAIL_URL.format(source_id=source_id))
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                soup = BeautifulSoup(response.text, "lxml")
                body = soup.select_one(".moreNewListR")
                content = str(body or "")
                text = html_to_text(content)
                fields = {
                    "position": row["title"], "keyword": f"{row['title']} {text}",
                    "location": text, "company_nature": row["nature"], "education": text,
                    "position_type": text, "employment_type": "全职",
                }
                if any(filters.get(k) and not contains(fields[k], filters[k]) for k in fields):
                    detail_success += 1
                    continue
                explicit_url, explicit_email = _field(soup, "应聘网址"), _field(soup, "简历投递邮箱")
                simple.append({
                    "公司": "", "岗位": row["title"], "岗位上新时间": row["date"], "JD": text,
                    "投递方式": extract_application_methods(content, explicit_email=explicit_email, explicit_url=explicit_url),
                    "原始链接": DETAIL_URL.format(source_id=source_id),
                })
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_URL.format(source_id=source_id), "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
        LAST_STATS = {"strategy": "get_html_session", "pages": pages, "matched": len(simple), "details_success": detail_success, "details_failed": detail_failed}
        return simple

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
from common.time import in_range

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "厦门大学", "XMU"
BASE_URL = "https://jy.xmu.edu.cn"
LIST_URL = f"{BASE_URL}/campus/index"
MAX_PAGES = 200
SUPPORTED_FILTERS = {"company": "local", "position": "local", "keyword": "local", **{k: "unsupported" for k in ("location", "company_nature", "industry", "education", "position_type", "employment_type", "student_type")}}
LAST_STATS: dict[str, int | str] = {}
_PACKED = re.compile(r'Base64\.decode\(unzip\("([A-Za-z0-9+/=]+)"\)\.substr\((\d+)\)\)\.substr\((\d+)\)')


def _unpack(text: str) -> str:
    match = _PACKED.search(text)
    if not match:
        return ""
    stage1 = zlib.decompress(base64.b64decode(match.group(1))).decode()[int(match.group(2)):]
    return base64.b64decode(stage1).decode()[int(match.group(3)):]


def _records(content: bytes) -> list[dict[str, str]]:
    soup = BeautifulSoup(_unpack(content.decode("utf-8", errors="replace")), "lxml")
    result = []
    for row in soup.select("ul.infoList"):
        link = row.select_one('a[href*="/campus/view/id/"]')
        cells = row.select("li")
        if link and cells:
            result.append({"title": link.get_text(" ", strip=True), "date": cells[-1].get_text(" ", strip=True), "url": urljoin(BASE_URL, link.get("href", ""))})
    return result


def _detail(content: bytes) -> tuple[str, str]:
    text = content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    company_link = soup.select_one('a[href*="/company/view/id/"]')
    body = _unpack(text) or str(soup.select_one(".aContent") or "")
    return (company_link.get_text(" ", strip=True) if company_link else "", body)


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    title, keyword = filters.get("position", ""), filters.get("keyword", "")
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = detail_success = detail_failed = 0
    candidates: list[dict[str, str]] = []
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            # 站内 keyword 搜索会跳统一认证，公开采集只使用原生日期条件。
            response = client.get(LIST_URL, params={"keyword": "", "starttime": start.strftime("%Y-%m-%d"), "endtime": end.strftime("%Y-%m-%d"), "page": page})
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET"))
            records = _records(response.content)
            pages += 1
            if not records:
                break
            candidates.extend(r for r in records if in_range(r["date"], start, end) and (not title or contains(r["title"], title)) and (not keyword or contains(r["title"], keyword)))
            if len(records) < 20:
                break
        results = []
        for record in candidates:
            source_id = record["url"].rstrip("/").split("/")[-1]
            company = body = ""
            try:
                response = client.get(record["url"])
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                company, body = _detail(response.content)
                if html_to_text(body):
                    detail_success += 1
                else:
                    detail_failed += 1
                    save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "error": "详情正文为空或已过期"})
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            if filters.get("company") and not contains(company, filters["company"]):
                continue
            results.append({"\u516c\u53f8": company, "\u5c97\u4f4d": record["title"], "\u5c97\u4f4d\u4e0a\u65b0\u65f6\u95f4": record["date"], "JD": html_to_text(body), "\u6295\u9012\u65b9\u5f0f": extract_application_methods(body), "\u539f\u59cb\u94fe\u63a5": record["url"]})
    LAST_STATS = {"strategy": "get_html_packed", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

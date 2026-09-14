from __future__ import annotations

import base64, logging, re, zlib
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
SCHOOL_NAME, TAG = "西安电子科技大学", "XIDIAN"
BASE_URL = "https://job.xidian.edu.cn"; ENTRY_URL = f"{BASE_URL}/campus"; PAGE_URL = f"{BASE_URL}/campus/index/do1/job.xidian.edu.cn/domain/xidian/city//page/{{page}}"
MAX_PAGES = 200
SUPPORTED_FILTERS = {"company": "local", "position": "local", "keyword": "native", "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported", "education": "unsupported", "position_type": "unsupported", "employment_type": "unsupported", "student_type": "unsupported"}
LAST_STATS: dict[str, int | str] = {}
BLOCK_RE = re.compile(r'unzip\("([A-Za-z0-9+/=]+)"\)\.substr\((\d+)\)\)\.substr\((\d+)\)')
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")


def _blocks(html: str) -> str:
    out = []
    for encoded, a, b in BLOCK_RE.findall(html):
        inflated = zlib.decompress(base64.b64decode(encoded)).decode(); decoded = base64.b64decode(inflated[int(a):]).decode(); out.append(decoded[int(b):])
    return "\n".join(out)


def _list(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(_blocks(html), "lxml"); out = []
    for a in soup.select("a[href*='/campus/view/id/']"):
        row = a.find_parent("ul")
        match = DATE_RE.search(row.get_text(" ", strip=True) if row else "")
        if match: out.append({"title": a.get_text(" ", strip=True), "published": match.group(), "url": urljoin(BASE_URL, str(a.get("href") or ""))})
    return out


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME); raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0; matched = []
    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            url = ENTRY_URL if page == 1 else PAGE_URL.format(page=page); response = client.get(url, params={"keyword": filters.get("keyword", "")})
            save_raw_html(raw_dir / f"list_page_{page:03d}.html", response.content, response_metadata(response, method="GET")); rows = _list(response.text); pages += 1
            if not rows: break
            dates = [parse_source_datetime(x["published"]) for x in rows]
            matched.extend(x for x in rows if in_range(x["published"], start, end) and (not filters.get("position") or contains(x["title"], filters["position"])))
            older_pages = older_pages + 1 if dates and all(x < start for x in dates) else 0
            if older_pages >= 2: break
        results = []
        for record in matched:
            source_id = record["url"].rsplit("/", 1)[-1]; company = content = ""
            try:
                response = client.get(record["url"], headers={"Referer": ENTRY_URL}); save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET")); soup = BeautifulSoup(response.text, "lxml"); node = soup.select_one("a.name.text-primary"); company = node.get_text(" ", strip=True) if node else ""; content = _blocks(response.text); detail_success += 1
            except Exception as exc:
                detail_failed += 1; save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            if filters.get("company") and not contains(company, filters["company"]): continue
            results.append({"公司": company, "岗位": record["title"], "岗位上新时间": record["published"], "JD": html_to_text(content), "投递方式": extract_application_methods(content), "原始链接": record["url"]})
    LAST_STATS = {"strategy": "get_html_embedded", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

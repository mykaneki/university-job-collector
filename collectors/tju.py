from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.http import HttpClient, response_metadata
from common.output import save_raw_html
from common.time import in_range, now_shanghai

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "天津大学", "TJU"
BASE_URL = "https://job.tju.edu.cn"
ENTRY_URL = f"{BASE_URL}/"
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _published(month_day: str, reference: datetime | None = None) -> str:
    reference = reference or now_shanghai()
    month, day = (int(value) for value in month_day.strip().split(".", 1))
    candidate = datetime(reference.year, month, day, tzinfo=reference.tzinfo)
    if candidate.date() > reference.date():
        candidate = candidate.replace(year=candidate.year - 1)
    return candidate.strftime("%Y-%m-%d")


def _list_records(html: str, reference: datetime | None = None) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    records = []
    # 只采集公开首页中“招聘简章”标签的可见列表；公职、实习和军工是独立栏目。
    for row in soup.select("ul.ullistcenter.list3 > li"):
        link = row.select_one("a[href*='/correcruit/content/id/']")
        date_node = row.select_one(".date")
        title_node = row.select_one(".zpname p")
        company_node = row.select_one(".zpname span")
        if not (link and date_node and title_node):
            continue
        records.append({
            "title": title_node.get_text(" ", strip=True),
            "company": company_node.get_text(" ", strip=True) if company_node else "",
            "published": _published(date_node.get_text(" ", strip=True), reference),
            "url": urljoin(BASE_URL, str(link.get("href") or "")),
        })
    return records


def _matches(record: dict[str, str], filters: dict[str, str]) -> bool:
    return (
        (not filters.get("company") or contains(record["company"], filters["company"]))
        and (not filters.get("position") or contains(record["title"], filters["position"]))
        and (not filters.get("keyword") or contains(f"{record['title']} {record['company']}", filters["keyword"]))
    )


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.warning("[%s] strategy=public_homepage_html coverage=partial; full list and detail require login", TAG)
    detail_restricted = 0
    with HttpClient() as client:
        response = client.get(ENTRY_URL)
        save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET") | {"coverage": "partial_public_homepage"})
        records = [record for record in _list_records(response.text) if in_range(record["published"], start, end) and _matches(record, filters)]
        LOGGER.info("[%s] page=1 public_records=%d matched=%d", TAG, len(_list_records(response.text)), len(records))
        results = []
        for record in records:
            source_id_match = re.search(r"/id/(\d+)", record["url"])
            source_id = source_id_match.group(1) if source_id_match else "unknown"
            detail = client.get(record["url"])
            save_raw_html(raw_dir / f"detail_{source_id}.html", detail.content, response_metadata(detail, method="GET") | {"access": "login_required"})
            restricted = "请先登录系统后再查看" in detail.text or "跳转提示" in detail.text
            if restricted:
                detail_restricted += 1
                LOGGER.info("[%s] detail login_required id=%s", TAG, source_id)
            results.append({"公司": record["company"], "岗位": record["title"], "岗位上新时间": record["published"], "JD": "", "投递方式": "", "原始链接": record["url"]})
    LAST_STATS = {
        "strategy": "public_homepage_html", "coverage": "partial", "pages": 1,
        "matched": len(results), "details_success": 0, "details_restricted": detail_restricted,
    }
    return results

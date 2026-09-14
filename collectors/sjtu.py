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
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "上海交通大学", "SJTU"
BASE_URL = "https://www.job.sjtu.edu.cn"
ENTRY_URL = f"{BASE_URL}/career/index"
DETAIL_API = f"{BASE_URL}/career/zpxx/data/zpxx/{{source_id}}"
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "local", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _list_records(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    sections = soup.select(".inviteTypeCon")
    if not sections:
        return []
    records = []
    # 首页第一个招聘区是“招聘公告”；后续区块为实习等其他栏目。
    for row in sections[0].select(".inviteTypeConListLeft"):
        onclick = str(row.get("onclick") or "").replace("\\/", "/")
        link_match = re.search(r'windowOpen\(["\']([^"\']+)', onclick)
        year_node = row.select_one(".sxhrq")
        month_day_node = row.select_one(".sxhsj")
        values = row.select(".inviteAddress .inviteUnit")
        if not (link_match and year_node and month_day_node and values):
            continue
        records.append({
            "source_id": link_match.group(1).rstrip("/").split("/")[-1],
            "url": urljoin(BASE_URL, link_match.group(1)),
            "title": values[0].get_text(" ", strip=True),
            "company": values[1].get_text(" ", strip=True) if len(values) > 1 else "",
            "published": f"{year_node.get_text(strip=True)}-{month_day_node.get_text(strip=True)}",
        })
    return records


def _matches(record: dict[str, str], detail: dict[str, object], filters: dict[str, str]) -> bool:
    company = str(detail.get("dwmc") or record["company"])
    title = str(detail.get("zpzt") or record["title"])
    fields = {
        "company": company,
        "position": title,
        "keyword": f"{title} {company}",
        "location": f"{detail.get('szxmc') or ''} {detail.get('gzdz') or ''}",
        "company_nature": str(detail.get("xzyjmc") or ""),
        "industry": str(detail.get("hyyjmc") or ""),
        "education": str(detail.get("xqxlmc") or ""),
    }
    return all(not filters.get(key) or contains(fields[key], filters[key]) for key in fields)


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.warning("[%s] strategy=public_homepage_json_detail coverage=partial; complete list API requires login", TAG)
    detail_success = detail_failed = 0
    results = []
    with HttpClient() as client:
        response = client.get(ENTRY_URL)
        save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET") | {"coverage": "partial_public_homepage"})
        public_records = _list_records(response.text)
        candidates = [record for record in public_records if in_range(record["published"], start, end)]
        LOGGER.info("[%s] page=1 public_records=%d date_candidates=%d", TAG, len(public_records), len(candidates))
        for record in candidates:
            source_id = record["source_id"]
            detail: dict[str, object] = {}
            try:
                response = client.post(DETAIL_API.format(source_id=source_id))
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="POST"))
                payload = response.json()
                if payload.get("code") != 200:
                    raise RuntimeError(str(payload.get("message") or "detail code != 200"))
                detail = payload.get("data") or {}
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                LOGGER.error("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API.format(source_id=source_id), "method": "POST", "error": f"{type(exc).__name__}: {exc}"})
            if not _matches(record, detail, filters):
                continue
            content = str(detail.get("zpxxEditor") or "")
            results.append({
                "公司": str(detail.get("dwmc") or record["company"]),
                "岗位": str(detail.get("zpzt") or record["title"]),
                "岗位上新时间": str(detail.get("fbrq") or record["published"]),
                "JD": html_to_text(content),
                "投递方式": extract_application_methods(content, explicit_url=str(detail.get("zpxxwz") or detail.get("ypwz") or "")),
                "原始链接": record["url"],
            })
    LAST_STATS = {"strategy": "public_homepage_json_detail", "coverage": "partial", "pages": 1, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

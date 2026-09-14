from __future__ import annotations

import logging
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
SCHOOL_NAME, TAG = "华东师范大学", "ECNU"
BASE_URL = "https://career.ecnu.edu.cn"
ENTRY_URL = f"{BASE_URL}/career/index"
DETAIL_API = f"{BASE_URL}/career/zwxx/data/{{source_id}}"
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "local", "position_type": "local", "employment_type": "local",
    "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _list_records(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    grid = soup.select_one(".jobs-grid.jobs-grid--active")
    if not grid:
        return []
    records = []
    for card in grid.select(".job-card"):
        body = card.select_one(".job-card__body[data-url]")
        title = card.select_one(".job-card__title")
        company = card.select_one(".job-card__company")
        published = card.select_one(".job-card__date")
        if not (body and title and published):
            continue
        path = str(body.get("data-url") or "")
        records.append({
            "source_id": path.rstrip("/").split("/")[-1],
            "url": urljoin(BASE_URL, path),
            "title": title.get_text(" ", strip=True),
            "company": company.get_text(" ", strip=True) if company else "",
            "published": published.get_text(" ", strip=True),
        })
    return records


def _matches(record: dict[str, str], detail: dict[str, object], filters: dict[str, str]) -> bool:
    company = str(detail.get("dwmc") or record["company"])
    title = str(detail.get("zwmc") or record["title"])
    fields = {
        "company": company,
        "position": title,
        "keyword": f"{title} {company}",
        "location": f"{detail.get('gzszxmc') or ''} {detail.get('gzdzxx') or ''}",
        "company_nature": str(detail.get("xzyjmc") or ""),
        "industry": str(detail.get("hyyjmc") or ""),
        "education": str(detail.get("xlyqmc") or ""),
        "position_type": str(detail.get("zwlbymc") or ""),
        "employment_type": str(detail.get("gzlxmc") or ""),
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
                response = client.get(DETAIL_API.format(source_id=source_id))
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="GET"))
                payload = response.json()
                if payload.get("code") != 200:
                    raise RuntimeError(str(payload.get("message") or "detail code != 200"))
                detail = payload.get("data") or {}
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                LOGGER.error("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API.format(source_id=source_id), "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            if not _matches(record, detail, filters):
                continue
            content = str(detail.get("zwms") or "")
            results.append({
                "公司": str(detail.get("dwmc") or record["company"]),
                "岗位": str(detail.get("zwmc") or record["title"]),
                "岗位上新时间": str(detail.get("fbrq") or record["published"]),
                "JD": html_to_text(content),
                "投递方式": extract_application_methods(content),
                "原始链接": record["url"],
            })
    LAST_STATS = {"strategy": "public_homepage_json_detail", "coverage": "partial", "pages": 1, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

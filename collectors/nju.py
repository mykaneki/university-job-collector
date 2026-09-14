from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from common.filters import contains, equals_normalized, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import in_range

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "南京大学", "NJU"
BASE_URL = "https://job.nju.edu.cn"
LIST_API = f"{BASE_URL}/api/career/job/recruitments"
DETAIL_API = f"{LIST_API}/{{source_id}}"
CONFIG_API = f"{BASE_URL}/api/career/configs"
PAGE_SIZE, MAX_PAGES = 50, 100
SUPPORTED_FILTERS = {
    "company": "local", "position": "local", "keyword": "local",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "local", "position_type": "unsupported", "employment_type": "local",
    "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _positions(record: dict[str, object]) -> list[dict[str, object]]:
    return [row for row in record.get("positions") or [] if isinstance(row, dict)]


def _is_full_time(record: dict[str, object]) -> bool:
    return any((row.get("position") or {}).get("fullTime") is True for row in _positions(record))


def _region_text(record: dict[str, object]) -> str:
    values = []
    for row in _positions(record):
        region = row.get("region") or {}
        if isinstance(region, dict):
            values.extend(str(region.get(key) or "") for key in ("province", "city", "district"))
    return " ".join(values)


def _position_text(record: dict[str, object]) -> str:
    values = [str(record.get("theme") or "")]
    for row in _positions(record):
        position = row.get("position") or {}
        if isinstance(position, dict):
            values.extend(str(position.get(key) or "") for key in ("name", "intro", "major"))
    return " ".join(values)


def _config_maps(payload: dict[str, object]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for key in ("company_industry", "company_property"):
        group = payload.get(key) or {}
        rows = group.get("value") or [] if isinstance(group, dict) else []
        result[key] = {
            str(row.get("code")): str(row.get("name"))
            for row in rows if isinstance(row, dict)
        }
    return result


def _company_meta(record: dict[str, object], key: str, mappings: dict[str, dict[str, str]]) -> str:
    company = record.get("company") or {}
    value = company.get(key) or {} if isinstance(company, dict) else {}
    code = str(value.get("value") or "") if isinstance(value, dict) else ""
    config_key = "company_property" if key == "property" else "company_industry"
    return mappings.get(config_key, {}).get(code, code)


def _matches(record: dict[str, object], filters: dict[str, str], mappings: dict[str, dict[str, str]]) -> bool:
    company_data = record.get("company") or {}
    company = str(company_data.get("name") or record.get("companyName") or "") if isinstance(company_data, dict) else str(record.get("companyName") or "")
    positions = _positions(record)
    education = " ".join(str((row.get("position") or {}).get("education") or "") for row in positions)
    employment_type = "全职" if _is_full_time(record) else "实习"
    position_text = _position_text(record)
    fields = {
        "company": company,
        "position": position_text,
        "keyword": f"{position_text} {company} {html_to_text(str(record.get('intro') or ''))}",
        "location": _region_text(record),
        "company_nature": _company_meta(record, "property", mappings),
        "industry": _company_meta(record, "industry", mappings),
        "education": education,
        "employment_type": employment_type,
    }
    return all(
        not filters.get(key)
        or (equals_normalized(fields[key], filters[key]) if key == "employment_type" else contains(fields[key], filters[key]))
        for key in fields
    )


def _detail_content(record: dict[str, object]) -> str:
    blocks = [str(record.get("intro") or "")]
    for row in _positions(record):
        position = row.get("position") or {}
        if isinstance(position, dict):
            name = str(position.get("name") or "")
            intro = str(position.get("intro") or "")
            if name or intro:
                blocks.append(f"<h3>{name}</h3>{intro}")
        for key in ("recruitmentCondition", "otherRequirement", "welfare"):
            if row.get(key):
                blocks.append(str(row[key]))
    return "\n".join(blocks)


def _simple(record: dict[str, object]) -> dict[str, str]:
    company_data = record.get("company") or {}
    company = str(company_data.get("name") or record.get("companyName") or "") if isinstance(company_data, dict) else str(record.get("companyName") or "")
    source_id = str(record.get("id") or "")
    content = _detail_content(record)
    website = str(record.get("website") or "")
    explicit_url = website if website.lower().startswith(("http://", "https://")) else ""
    link = f"{BASE_URL}/career/jobs-v2?{urlencode({'fullTime': 'true', 'recruitmentId': source_id})}"
    return {
        "公司": company,
        "岗位": str(record.get("theme") or ""),
        "岗位上新时间": str(record.get("applyAt") or ""),
        "JD": html_to_text(content),
        "投递方式": extract_application_methods(content, explicit_email=str(record.get("email") or ""), explicit_url=explicit_url),
        "原始链接": link,
    }


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[%s] strategy=json_api", TAG)
    mappings: dict[str, dict[str, str]] = {}
    pages = detail_success = detail_failed = 0
    matched: list[dict[str, object]] = []
    with HttpClient() as client:
        if filters.get("company_nature") or filters.get("industry"):
            response = client.get(CONFIG_API, params={"metaKeys": "company_industry,company_property"})
            save_raw_json(raw_dir / "filter_options.json", response.content, response_metadata(response, method="GET"))
            mappings = _config_maps(response.json())
        total_pages = 1
        for page in range(MAX_PAGES):
            response = client.get(LIST_API, params={"page": page, "size": PAGE_SIZE})
            save_raw_json(raw_dir / f"list_page_{page + 1:03d}.json", response.content, response_metadata(response, method="GET"))
            payload = response.json()
            records = payload.get("content") or []
            page_info = payload.get("page") or {}
            total_pages = min(int(page_info.get("totalPages") or 1), MAX_PAGES)
            pages += 1
            LOGGER.info("[%s] page=%d/%d records=%d", TAG, page + 1, total_pages, len(records))
            for record in records:
                published = str(record.get("applyAt") or "")
                if _is_full_time(record) and in_range(published, start, end) and _matches(record, filters, mappings):
                    matched.append(record)
            if page + 1 >= total_pages:
                break
        results = []
        for list_record in matched:
            source_id = str(list_record.get("id") or "unknown")
            record = list_record
            try:
                response = client.get(DETAIL_API.format(source_id=source_id))
                save_raw_json(raw_dir / f"detail_{source_id}.json", response.content, response_metadata(response, method="GET"))
                record = response.json()
                detail_success += 1
                LOGGER.info("[%s] detail success id=%s", TAG, source_id)
            except Exception as exc:
                detail_failed += 1
                LOGGER.error("[%s] detail failed id=%s error=%s", TAG, source_id, exc)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": DETAIL_API.format(source_id=source_id), "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
            results.append(_simple(record))
    LAST_STATS = {"strategy": "json_api", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

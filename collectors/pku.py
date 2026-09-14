from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_json
from common.time import in_range, parse_source_datetime


LOGGER = logging.getLogger(__name__)
SCHOOL_NAME = "北京大学"
BASE_URL = "https://scc.pku.edu.cn"
LIST_API = f"{BASE_URL}/f/recruitmentinfo/ajax_frontRecruitinfo"
DETAIL_API = f"{BASE_URL}/f/recruitmentinfo/ajax_show"
PAGE_SIZE = 15
MAX_PAGES = 200

SUPPORTED_FILTERS = {
    "company": "local",
    "position": "native",
    "keyword": "native",
    "location": "unsupported",
    "company_nature": "native",
    "industry": "native",
    "education": "unsupported",
    "position_type": "unsupported",
    "employment_type": "unsupported",
    "student_type": "unsupported",
}

COMPANY_NATURE_CODES = {
    "机关": "10",
    "科研设计单位": "20",
    "高等教育单位": "21",
    "中等、初等教育单位": "22",
    "医疗卫生单位": "23",
    "其他事业单位": "29",
    "国有企业": "31",
    "外商投资企业": "32",
    "其他企业(含民营企业等)": "39",
    "部队": "40",
    "农村建制村": "55",
    "城镇社区": "56",
    "个体工商户": "33",
    "其他(含社会组织、国际组织等)": "99",
}

INDUSTRY_CODES = {
    "农、林、牧、渔业": "11",
    "采矿业": "21",
    "制造业": "22",
    "电力、热力、燃气及水生产和供应业": "23",
    "建筑业": "24",
    "批发和零售业": "31",
    "交通运输、仓储和邮政业": "32",
    "住宿和餐饮业": "33",
    "信息传输、软件和信息技术服务业": "34",
    "金融业": "35",
    "房地产业": "36",
    "租赁和商务服务业": "37",
    "科学研究和技术服务业": "38",
    "水利、环境和公共设施管理业": "39",
    "居民服务、修理和其他服务业": "41",
    "教育": "42",
    "卫生和社会工作": "43",
    "文化、体育和娱乐业": "44",
    "公共管理、社会保障和社会组织": "45",
    "国际组织": "46",
    "军队": "80",
}

LAST_STATS: dict[str, int | str | list[str]] = {}


def _code(mapping: dict[str, str], value: str, field: str) -> str:
    if value not in mapping:
        available = "、".join(mapping)
        raise ValueError(f"{SCHOOL_NAME} {field} 不识别 {value!r}，可用值：{available}")
    return mapping[value]


def _native_payload(filters: dict[str, str], page: int) -> dict[str, str | int]:
    position = filters.get("position", "")
    keyword = filters.get("keyword", "")
    if position and keyword and position != keyword:
        raise ValueError("北京大学 position 与 keyword 共用站内 title 参数，不能同时传入不同值")
    return {
        "pageNo": page,
        "pageSize": PAGE_SIZE,
        "positionType": "1",
        "title": position or keyword,
        "corporationNature": _code(COMPANY_NATURE_CODES, filters["company_nature"], "company_nature")
        if filters.get("company_nature")
        else "",
        "corporationinfo.industry": _code(INDUSTRY_CODES, filters["industry"], "industry")
        if filters.get("industry")
        else "",
    }


def _is_local_match(item: dict[str, object], filters: dict[str, str]) -> bool:
    return not filters.get("company") or contains(item.get("corporationName"), filters["company"])


def _simple_item(list_item: dict[str, object], detail: dict[str, object] | None) -> dict[str, str]:
    detail = detail or {}
    content = str(detail.get("content") or "")
    company_info = detail.get("corporationinfo") if isinstance(detail.get("corporationinfo"), dict) else {}
    company = str(list_item.get("corporationName") or company_info.get("name") or "")
    return {
        "公司": company,
        "岗位": str(list_item.get("title") or detail.get("title") or ""),
        "岗位上新时间": str(list_item.get("startTime") or detail.get("startTime") or ""),
        "JD": html_to_text(content),
        "投递方式": extract_application_methods(
            content,
            explicit_email=str(detail.get("resumeReceiveEmail") or ""),
            explicit_url=str(detail.get("onlineApplicationUrl") or ""),
        ),
        "原始链接": urljoin(BASE_URL, str(list_item.get("url") or "")),
    }


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[PKU] strategy=json_api")
    matched: list[dict[str, object]] = []
    pages = 0
    older_pages = 0
    detail_success = 0
    detail_failed = 0

    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.post(LIST_API, data=_native_payload(filters, page))
            save_raw_json(
                raw_dir / f"list_page_{page:03d}.json",
                response.content,
                response_metadata(response, method="POST"),
            )
            payload = response.json()
            if payload.get("state") != 1:
                raise RuntimeError(f"北大列表 API 返回失败：{payload.get('msg', payload)}")
            obj = payload.get("object") or {}
            records = obj.get("list") or []
            pages += 1
            LOGGER.info("[PKU] page=%d records=%d", page, len(records))
            if not records:
                break

            normal_dates = []
            for record in records:
                published = str(record.get("startTime") or "")
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    LOGGER.warning("[PKU] invalid publish time id=%s value=%r", record.get("id"), published)
                    continue
                if str(record.get("topFlag") or "0") != "1":
                    normal_dates.append(parsed)
                if in_range(published, start, end) and _is_local_match(record, filters):
                    matched.append(record)

            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2 or bool(obj.get("lastPage")):
                break

        results: list[dict[str, str]] = []
        for record in matched:
            source_id = str(record.get("id") or "unknown")
            detail: dict[str, object] | None = None
            try:
                response = client.post(DETAIL_API, data={"recruitmentId": source_id})
                save_raw_json(
                    raw_dir / f"detail_{source_id}.json",
                    response.content,
                    response_metadata(response, method="POST"),
                )
                payload = response.json()
                if payload.get("state") != 1:
                    raise RuntimeError(str(payload.get("msg") or "detail state != 1"))
                detail = (payload.get("object") or {}).get("recruitmentinfo") or {}
                detail_success += 1
                LOGGER.info("[PKU] detail success id=%s", source_id)
            except Exception as exc:  # one detail must not stop a school run
                detail_failed += 1
                LOGGER.error("[PKU] detail failed id=%s error=%s", source_id, exc)
                save_error_meta(
                    raw_dir / f"detail_{source_id}.error",
                    {"url": DETAIL_API, "method": "POST", "error": f"{type(exc).__name__}: {exc}"},
                )
            results.append(_simple_item(record, detail))

    LAST_STATS = {
        "strategy": "json_api",
        "pages": pages,
        "matched": len(results),
        "details_success": detail_success,
        "details_failed": detail_failed,
    }
    return results

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup, Tag

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html
from common.time import in_range, parse_source_datetime


LOGGER = logging.getLogger(__name__)
SCHOOL_NAME = "清华大学"
BASE_URL = "https://career.cic.tsinghua.edu.cn"
LIST_URL = f"{BASE_URL}/xsglxt/f/jyxt/anony/xxfb"
MAX_PAGES = 200

SUPPORTED_FILTERS = {
    "company": "native",
    "position": "native",
    "keyword": "local",
    "location": "native",
    "company_nature": "native",
    "industry": "native",
    "education": "native",
    "position_type": "native",
    "employment_type": "native",
    "student_type": "native",
}

FIELD_MAP = {
    "company": "dwmc",
    "position": "zwmc",
    "location": "gzdqmc",
    "company_nature": "dwxzdm",
    "industry": "dwhydm",
    "education": "xlyq",
    "position_type": "zwlb",
    "employment_type": "zwxz",
    "student_type": "zwbqmc",
}

OPTION_CODES = {
    "company_nature": {
        "部队": "10", "中央政府": "21", "地方政府": "22", "高等院校": "311", "中初等教育单位": "312",
        "科研单位": "32", "文化体育卫生机构": "33", "其他事业单位": "34", "国有企业": "41", "外资企业": "42",
        "民营企业": "43", "个体工商户": "44", "社会组织": "50",
    },
    "industry": {
        "农、林、牧、渔业": "01", "采矿业": "02", "制造业": "03", "电力、热力、燃气及水生产和供应业": "04",
        "建筑业": "05", "批发和零售业": "06", "交通运输、仓储和邮政业": "07", "住宿和餐饮业": "08",
        "信息传输、软件和信息技术服务业": "09", "金融业": "10", "房地产业": "11", "租赁和商务服务业": "12",
        "科学研究和技术服务业": "13", "水利、环境和公共设施管理业": "14", "居民服务、修理和其他服务业": "15",
        "教育": "16", "卫生和社会工作": "17", "文化、体育和娱乐业": "18", "公共管理、社会保障和社会组织": "19",
        "国际组织": "20", "军队": "21",
    },
    "position_type": {
        "博士后研究人员": "09", "公务员": "10", "科学研究人员": "11", "工程技术人员": "13", "农林牧渔业技术人员": "17",
        "飞机和船舶技术人员": "18", "卫生专业技术人员": "19", "经济业务人员": "21", "金融业务人员": "22", "法律专业人员": "23",
        "教学人员": "24", "文学艺术工作人员": "25", "体育工作人员": "26", "新闻出版和文化工作人员": "27",
        "其他专业技术人员": "29", "办事人员和有关人员": "31", "商业和服务业人员": "49", "生产和运输设备操作人员": "60",
        "军人": "80", "其他人员": "90",
    },
    "education": {"专科": "专科", "本科": "本科", "硕士": "硕士", "博士": "博士", "博士后": "博士后"},
    "employment_type": {"全职": "全职", "实习": "实习"},
    "student_type": {"国际学生": "国际学生", "港澳台学生": "港澳台学生"},
}

LAST_STATS: dict[str, int | str] = {}


def _code(field: str, value: str) -> str:
    mapping = OPTION_CODES.get(field)
    if mapping is None:
        return value
    if value not in mapping:
        raise ValueError(f"{SCHOOL_NAME} {field} 不识别 {value!r}，可用值：{'、'.join(mapping)}")
    return mapping[value]


def _payload(filters: dict[str, str], page: int) -> dict[str, str | int]:
    payload: dict[str, str | int] = {"flag": "", "type": "", "pgno": page}
    for site_field in FIELD_MAP.values():
        payload[site_field] = ""
    for field, site_field in FIELD_MAP.items():
        if filters.get(field):
            payload[site_field] = _code(field, filters[field])
    return payload


def _parse_list(html: bytes) -> tuple[list[dict[str, object]], int | None]:
    soup = BeautifulSoup(html, "lxml")
    records: list[dict[str, object]] = []
    for node in soup.select("#todayList li"):
        date_node = node.find("span")
        anchor = node.find("a")
        if not date_node or not anchor:
            continue
        combined_title = " ".join(anchor.get_text(" ", strip=True).split())
        if "————" in combined_title:
            title, company = combined_title.rsplit("————", 1)
        else:
            title, company = combined_title, ""
        relative_url = anchor.get("ahref") or anchor.get("href") or ""
        attrs = "".join(f"{key}={value}" for key, value in anchor.attrs.items()).replace(" ", "").lower()
        records.append(
            {
                "date": date_node.get_text(strip=True),
                "title": title.strip(),
                "company": company.strip(),
                "url": urljoin(BASE_URL, relative_url),
                "source_id": (parse_qs(urlparse(relative_url).query).get("zpxxid") or [""])[0],
                "fbfw": str(anchor.get("fbfw") or ""),
                "pinned": "color:#ff0000" in attrs or "color:red" in attrs,
            }
        )
    total_node = soup.select_one("#totalPg")
    total_pages = int(total_node.get_text(strip=True)) if total_node and total_node.get_text(strip=True).isdigit() else None
    return records, total_pages


def _description_html(soup: BeautifulSoup) -> str:
    heading = next(
        (node for node in soup.select("h2.company-headline") if node.get_text(" ", strip=True) == "职位描述"),
        None,
    )
    if heading is None:
        return ""
    fragments: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "h2":
            break
        fragments.append(str(sibling))
    return "".join(fragments)


def _label_value(soup: BeautifulSoup, label: str) -> str:
    text = soup.select_one("div.content.teacher")
    if not text:
        return ""
    match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n]+)", text.get_text("\n", strip=True))
    return match.group(1).strip() if match else ""


def _parse_detail(html: bytes) -> tuple[str, str, str, bool]:
    soup = BeautifulSoup(html, "lxml")
    description_html = _description_html(soup)
    if not description_html:
        return "", "", "", False
    company = _label_value(soup, "公司名称")
    methods = extract_application_methods(description_html)
    return company, html_to_text(description_html), methods, True


def _is_local_match(record: dict[str, object], filters: dict[str, str]) -> bool:
    keyword = filters.get("keyword", "")
    return not keyword or contains(f"{record.get('title', '')} {record.get('company', '')}", keyword)


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("[Tsinghua] strategy=post_html")
    matched: list[dict[str, object]] = []
    older_pages = 0
    pages = 0
    detail_success = 0
    detail_failed = 0
    detail_restricted = 0

    with HttpClient() as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.post(LIST_URL, data=_payload(filters, page))
            save_raw_html(
                raw_dir / f"list_page_{page:03d}.html",
                response.content,
                response_metadata(response, method="POST"),
            )
            records, total_pages = _parse_list(response.content)
            pages += 1
            LOGGER.info("[Tsinghua] page=%d records=%d", page, len(records))
            if not records:
                break

            normal_dates = []
            for record in records:
                published = str(record["date"])
                try:
                    parsed = parse_source_datetime(published)
                except ValueError:
                    LOGGER.warning("[Tsinghua] invalid publish time id=%s value=%r", record["source_id"], published)
                    continue
                if not record["pinned"]:
                    normal_dates.append(parsed)
                if in_range(published, start, end) and _is_local_match(record, filters):
                    matched.append(record)

            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2 or (total_pages is not None and page >= total_pages):
                break

        results: list[dict[str, str]] = []
        for record in matched:
            source_id = str(record["source_id"] or "unknown")
            company = str(record["company"] or "")
            jd = ""
            methods = ""
            try:
                response = client.get(str(record["url"]))
                save_raw_html(
                    raw_dir / f"detail_{source_id}.html",
                    response.content,
                    response_metadata(response, method="GET"),
                )
                detail_company, jd, methods, accessible = _parse_detail(response.content)
                if not accessible:
                    detail_restricted += 1
                    LOGGER.warning("[Tsinghua] detail restricted id=%s fbfw=%s", source_id, record["fbfw"])
                else:
                    company = detail_company or company
                    detail_success += 1
                    LOGGER.info("[Tsinghua] detail success id=%s fbfw=%s", source_id, record["fbfw"])
            except Exception as exc:  # one detail must not stop a school run
                detail_failed += 1
                LOGGER.error("[Tsinghua] detail failed id=%s error=%s", source_id, exc)
                save_error_meta(
                    raw_dir / f"detail_{source_id}.error",
                    {"url": record["url"], "method": "GET", "error": f"{type(exc).__name__}: {exc}"},
                )
            results.append(
                {
                    "公司": company,
                    "岗位": str(record["title"]),
                    "岗位上新时间": str(record["date"]),
                    "JD": jd,
                    "投递方式": methods,
                    "原始链接": str(record["url"]),
                }
            )

    LAST_STATS = {
        "strategy": "post_html",
        "pages": pages,
        "matched": len(results),
        "details_success": detail_success,
        "details_failed": detail_failed,
        "details_restricted": detail_restricted,
    }
    return results

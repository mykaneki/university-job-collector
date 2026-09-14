from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.http import HttpClient, response_metadata
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "中国海洋大学", "OUC"
BASE_URL = "http://career.ouc.edu.cn/zftal-web"
LIST_PAGE = f"{BASE_URL}/zfjy!ykfw/zpztgl_cxZpztList.html?zpdxdm=1"
LIST_API = f"{BASE_URL}/zfjy!ykfw/zpztgl_cxZpztList.html?doType=query"
DETAIL_URL = f"{BASE_URL}/zfjy!ykfw/zpztgl_cxWzZpxxNry.html"
PAGE_SIZE, MAX_PAGES = 15, 200
SUPPORTED_FILTERS = {
    "company": "native", "position": "native", "keyword": "unsupported",
    "location": "local", "company_nature": "local", "industry": "local",
    "education": "local", "position_type": "local", "employment_type": "local",
    "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _list_params(page: int, filters: dict[str, str]) -> dict[str, int | str]:
    return {
        "queryModel.currentPage": page,
        "queryModel.showCount": PAGE_SIZE,
        "queryModel.sortName": "fbsj",
        "queryModel.sortOrder": "desc",
        "zpdxdm": "1",
        "zwlbdm": "",
        "zpxldm": "",
        "tdjzrq": "",
        "dwmc": filters.get("company", ""),
        "zwmc": filters.get("position", ""),
    }


def _detail_text(html: str) -> str:
    text = html_to_text(html)
    marker = "招聘信息\nRECRUITMENT INFORMATIOM"
    if marker in text:
        text = text.split(marker, 1)[1]
    if "\n版权所有：" in text:
        text = text.split("\n版权所有：", 1)[0]
    return text.strip()


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, object]] = []
    with HttpClient() as client:
        client.get(LIST_PAGE)
        for page in range(1, MAX_PAGES + 1):
            data = _list_params(page, filters)
            response = client.post(LIST_API, data=data, headers={"Referer": LIST_PAGE, "X-Requested-With": "XMLHttpRequest"})
            save_raw_json(raw_dir / f"list_page_{page:03d}.json", response.content, response_metadata(response, method="POST"))
            payload = response.json()
            rows = payload.get("items") or []
            pages += 1
            normal_dates = []
            for row in rows:
                published = str(row.get("fbsj") or "")
                try:
                    normal_dates.append(parse_source_datetime(published))
                except ValueError:
                    continue
                if in_range(published, start, end):
                    matched.append(row)
            older_pages = older_pages + 1 if normal_dates and all(value < start for value in normal_dates) else 0
            if older_pages >= 2 or page >= int(payload.get("totalPage") or 1):
                break
        simple = []
        for row in matched:
            source_id = str(row.get("id") or "unknown")
            url = f"{DETAIL_URL}?{urlencode({'id': source_id, 'zpdxdm': '1'})}"
            try:
                response = client.get(url)
                save_raw_html(raw_dir / f"detail_{source_id}.html", response.content, response_metadata(response, method="GET"))
                text = _detail_text(response.text)
                fields = {
                    "location": text, "company_nature": text, "industry": text,
                    "education": text, "position_type": text, "employment_type": text,
                }
                detail_success += 1
                if any(filters.get(k) and not contains(fields[k], filters[k]) for k in fields):
                    continue
                soup = BeautifulSoup(response.text, "lxml")
                content = str(soup.body or "")
                simple.append({
                    "公司": str(row.get("dwmc") or ""), "岗位": str(row.get("zwmc") or row.get("zpzt") or ""),
                    "岗位上新时间": str(row.get("fbsj") or ""), "JD": text,
                    "投递方式": extract_application_methods(content), "原始链接": url,
                })
            except Exception as exc:
                detail_failed += 1
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": url, "method": "GET", "error": f"{type(exc).__name__}: {exc}"})
        LAST_STATS = {"strategy": "post_json_get_html", "pages": pages, "matched": len(simple), "details_success": detail_success, "details_failed": detail_failed}
        return simple

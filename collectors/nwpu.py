from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from common.browser import ChromeCdpSession
from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "西北工业大学", "NWPU"
BASE_URL = "https://job.nwpu.edu.cn"
ENTRY_URL = f"{BASE_URL}/frontpage/nwpu/html/recruitmentinfoList.html?type=1"
MAX_PAGES = 200
SUPPORTED_FILTERS = {
    "company": "local", "position": "native", "keyword": "native",
    "location": "unsupported", "company_nature": "native", "industry": "native",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _rendered_meta(url: str, status: int | None) -> dict[str, object]:
    return {
        "url": url, "method": "GET", "status_code": status,
        "content_type": "text/html; rendered-dom", "capture": "chrome_cdp_rendered_dom",
        "success_basis": "complete_list_dom",
    }


def _response_meta(response: object, representation: str) -> dict[str, object]:
    headers = getattr(response, "headers", {})
    request = getattr(response, "request", None)
    return {
        "url": str(getattr(response, "url", "")),
        "method": str(getattr(request, "method", "GET")),
        "status_code": getattr(response, "status", None),
        "content_type": headers.get("content-type", "") if isinstance(headers, dict) else "",
        "capture": "playwright_network_response",
        "raw_representation": representation,
    }


def _save_failed_render(page: object, path: Path, requested_url: str, response: object | None) -> bool:
    try:
        rendered = page.content()  # type: ignore[attr-defined]
        final_url = str(getattr(page, "url", requested_url))
        status = getattr(response, "status", None) if response is not None else None
        save_raw_html(path, rendered, _rendered_meta(final_url, status) | {"requested_url": requested_url, "capture": "chrome_cdp_rendered_dom_after_error", "navigation_failed": True})
        return True
    except Exception:
        return False


def _pagination_has_next(html: str, current_page: int) -> bool:
    soup = BeautifulSoup(html, "lxml")
    active = soup.select_one(".pageWrap li.active")
    if active:
        try:
            current_page = int(active.get_text(" ", strip=True))
        except ValueError:
            pass
    targets = []
    for node in soup.select(".pageWrap a[onclick]"):
        match = re.search(r"page\((\d+)\s*,", str(node.get("onclick") or ""))
        if match:
            targets.append(int(match.group(1)))
    return any(target > current_page for target in targets)


def _reported_total(html: str) -> int | None:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(".pageWrap .dataNum")
    match = re.search(r"共\s*(\d+)\s*条", node.get_text(" ", strip=True) if node else "")
    return int(match.group(1)) if match else None


def _raise_pagination_error(page: object, raw_dir: Path, next_page: int, exc: Exception) -> None:
    requested_url = f"{ENTRY_URL}#page={next_page}"
    raw_saved = _save_failed_render(page, raw_dir / f"list_page_{next_page:03d}.failed.html", requested_url, None)
    save_error_meta(raw_dir / f"list_page_{next_page:03d}.error", {"url": str(getattr(page, "url", ENTRY_URL)), "method": "POST", "rendered_dom_saved": raw_saved, "error": f"{type(exc).__name__}: {exc}"})
    raise RuntimeError(f"{SCHOOL_NAME}翻到第 {next_page} 页失败") from exc


def _list_records(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    records = []
    for item in soup.select("#listPlace .infoItem"):
        anchor = item.select_one("a.tit[href*='recruitmentinfo/show']")
        date = item.select_one(".left .time")
        if not anchor or not date:
            continue
        company = item.select_one(".mid .eName")
        nature = item.select_one(".mid .eNature")
        nature_text = nature.get_text(" ", strip=True).split("|", 1)[0].strip() if nature else ""
        records.append({
            "title": anchor.get("title") or anchor.get_text(" ", strip=True),
            "published": date.get_text(" ", strip=True),
            "company": company.get("title") or company.get_text(" ", strip=True) if company else "",
            "company_nature": nature_text,
            "url": urljoin(BASE_URL, str(anchor.get("href") or "")),
        })
    return records


def _native_value(html: str, input_name: str, label: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    mapping = {}
    for node in soup.select(f"input[name='{input_name}']"):
        parent = node.find_parent("label")
        if parent:
            mapping[parent.get_text(" ", strip=True)] = str(node.get("value") or "")
    if label not in mapping:
        raise ValueError(f"{SCHOOL_NAME} {input_name} 不识别 {label!r}，可用值：{'、'.join(mapping)}")
    return mapping[label]


def _detail(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one(".positionDetail .name.getCompany")
    meta = soup.select_one(".midInfo .l_con")
    meta_text = meta.get_text(" ", strip=True) if meta else ""
    company_match = re.search(r"发布企业：\s*(.+?)\s*(?:日期：|$)", meta_text)
    date_match = re.search(r"日期：\s*(\d{4}-\d{1,2}-\d{1,2}(?: \d{2}:\d{2}:\d{2})?)", meta_text)
    content = soup.select_one(".positionDetailMain .positionDetailLeft")
    return {
        "title": title.get_text(" ", strip=True) if title else "",
        "company": company_match.group(1).strip() if company_match else "",
        "published": date_match.group(1) if date_match else "",
        "content": str(content or ""),
    }


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    position, keyword = filters.get("position", ""), filters.get("keyword", "")
    if position and keyword and position != keyword:
        raise ValueError(f"{SCHOOL_NAME} position 与 keyword 共用站内 title 参数，不能传入不同值")
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, str]] = []
    with ChromeCdpSession() as chrome:
        page = chrome.new_page()
        response = page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=30_000)
        if response is not None:
            save_raw_html(raw_dir / "entry_initial_response.html", response.body(), _response_meta(response, "original_response_body"))
        # WAF 会让首次主响应状态为 412，但站点脚本随后能渲染完整公开列表；
        # 是否成功只以真实完整列表 DOM 为准。
        page.wait_for_selector("#listPlace .infoItem a.tit[href*='recruitmentinfo/show']", timeout=30_000)
        initial_html = page.content()
        title_filter = position or keyword
        nature_value = _native_value(initial_html, "corporationNature", filters["company_nature"]) if filters.get("company_nature") else ""
        industry_value = _native_value(initial_html, "corporationinfo", filters["industry"]) if filters.get("industry") else ""
        if title_filter or nature_value or industry_value:
            page.locator("#title").fill(title_filter)
            page.locator("#corporationNature").evaluate("(el, value) => el.value = value", nature_value)
            page.locator("#corporationinfo").evaluate("(el, value) => el.value = value", industry_value)
            before = page.locator("#listPlace").inner_html()
            with page.expect_response(lambda item: "ajax_frontRecruitinfo" in item.url, timeout=30_000) as response_info:
                page.evaluate("() => page(1, 15)")
            xhr = response_info.value
            save_raw_json(raw_dir / "list_page_001.xhr.json", xhr.body(), _response_meta(xhr, "original_response_body"))
            page.wait_for_function("before => document.querySelector('#listPlace')?.innerHTML !== before", arg=before, timeout=30_000)
        first_status = response.status if response else None
        previous_signature: tuple[str, ...] | None = None
        for page_no in range(1, MAX_PAGES + 1):
            page.wait_for_selector("#listPlace", timeout=20_000)
            rendered = page.content()
            save_raw_html(raw_dir / f"list_page_{page_no:03d}.html", rendered, _rendered_meta(page.url, first_status if page_no == 1 else None))
            rows = _list_records(rendered)
            pages += 1
            if not rows:
                if _reported_total(rendered) == 0:
                    break
                save_error_meta(raw_dir / f"list_page_{page_no:03d}.error", {"url": page.url, "method": "GET", "error": "complete list DOM contained no records without an explicit zero total"})
                raise RuntimeError(f"{SCHOOL_NAME}第 {page_no} 页完整 DOM 无记录，且未确认为空结果")
            signature = tuple(row["url"] for row in rows)
            if previous_signature is not None and signature == previous_signature:
                save_error_meta(raw_dir / f"list_page_{page_no:03d}.error", {"url": page.url, "method": "POST", "error": "pagination returned the previous page signature"})
                raise RuntimeError(f"{SCHOOL_NAME}第 {page_no} 页与上一页重复，翻页未生效")
            previous_signature = signature
            dates = []
            for row in rows:
                try:
                    dates.append(parse_source_datetime(row["published"]))
                except ValueError:
                    continue
                if not in_range(row["published"], start, end):
                    continue
                if filters.get("company") and not contains(row["company"], filters["company"]):
                    continue
                matched.append(row)
            older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
            if older_pages >= 2:
                break
            if not _pagination_has_next(rendered, page_no):
                break
            before = page.locator("#listPlace").inner_html()
            try:
                with page.expect_response(lambda item: "ajax_frontRecruitinfo" in item.url, timeout=20_000) as response_info:
                    page.evaluate("pageNo => page(pageNo, 15)", page_no + 1)
                xhr = response_info.value
                save_raw_json(raw_dir / f"list_page_{page_no + 1:03d}.xhr.json", xhr.body(), _response_meta(xhr, "original_response_body"))
                page.wait_for_function("before => document.querySelector('#listPlace')?.innerHTML !== before", arg=before, timeout=20_000)
            except Exception as exc:
                _raise_pagination_error(page, raw_dir, page_no + 1, exc)

        results = []
        for record in matched:
            source_id = parse_qs(urlparse(record["url"]).query).get("recruitmentId", ["unknown"])[0]
            detail: dict[str, str] = {}
            final_url = record["url"]
            response = None
            try:
                response = page.goto(record["url"], wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector(".positionDetailMain .positionDetailLeft", timeout=30_000)
                rendered = page.content()
                final_url = page.url
                save_raw_html(raw_dir / f"detail_{source_id}.html", rendered, _rendered_meta(page.url, response.status if response else None))
                detail = _detail(rendered)
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                raw_saved = _save_failed_render(page, raw_dir / f"detail_{source_id}.failed.html", record["url"], response)
                save_error_meta(raw_dir / f"detail_{source_id}.error", {"url": record["url"], "method": "GET", "capture": "chrome_cdp", "rendered_dom_saved": raw_saved, "error": f"{type(exc).__name__}: {exc}"})
            published = detail.get("published") or record["published"]
            if not in_range(published, start, end):
                continue
            content = detail.get("content", "")
            text = html_to_text(content)
            results.append({
                "公司": detail.get("company") or record["company"],
                "岗位": detail.get("title") or record["title"],
                "岗位上新时间": published,
                "JD": text,
                "投递方式": extract_application_methods(content),
                "原始链接": final_url,
            })
    LAST_STATS = {"strategy": "chrome_cdp_rendered_html", "pages": pages, "matched": len(results), "details_success": detail_success, "details_failed": detail_failed}
    return results

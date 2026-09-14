from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Page, Response

from common.browser import ChromeCdpSession
from common.filters import contains, validate_supported
from common.html import extract_application_methods, html_to_text
from common.output import save_error_meta, save_raw_html, save_raw_json
from common.time import in_range, parse_source_datetime

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "中国科学技术大学", "USTC"
BASE_URL = "https://www.job.ustc.edu.cn"
LIST_URL = f"{BASE_URL}/Recruitment/list.aspx"
LIST_API_PATH = "/Ajax/jywapi.ashx"
PAGE_SIZE, MAX_PAGES = 10, 200
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "local", "keyword": "local",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def _rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    result = []
    for node in soup.select("#zpgg li"):
        anchor = node.select_one("p.pa a[href*='itemid=']")
        date_node = node.select_one("time span")
        if isinstance(anchor, Tag) and isinstance(date_node, Tag):
            result.append({
                "title": anchor.get_text(" ", strip=True),
                "date": date_node.get_text(" ", strip=True),
                "url": urljoin(BASE_URL, str(anchor.get("href") or "")),
            })
    return result


def _http_meta(response: Response, *, page: int | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {
        "url": response.url,
        "method": response.request.method,
        "status_code": response.status,
        "content_type": response.headers.get("content-type", ""),
        "payload": response.request.post_data or "",
        "artifact_type": "http_response_body",
        "capture": "external_chrome_cdp_playwright",
    }
    if page is not None:
        metadata["page"] = page
    return metadata


def _rendered_meta(
    url: str,
    trigger: Response | None,
    *,
    page: int | None = None,
    interaction: str = "navigate",
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "url": url,
        "method": "BROWSER",
        "content_type": "text/html",
        "artifact_type": "rendered_dom",
        "capture": "external_chrome_cdp_playwright",
        "interaction": interaction,
        "trigger_response": _http_meta(trigger, page=page) if trigger is not None else None,
    }
    if page is not None:
        metadata["page"] = page
    return metadata


def _xhr_meta(response: Response, page_number: int) -> dict[str, object]:
    return _http_meta(response, page=page_number)


def _is_list_xhr(response: Response, page_number: int) -> bool:
    return (
        LIST_API_PATH in response.url
        and response.request.method == "POST"
        and "action=joblist2" in (response.request.post_data or "")
        and f"pageindex={page_number}" in (response.request.post_data or "")
    )


def _is_pager_xhr(response: Response, page_number: int) -> bool:
    return (
        LIST_API_PATH in response.url
        and response.request.method == "POST"
        and "action=getpage" in (response.request.post_data or "")
        and f"pageindex={page_number}" in (response.request.post_data or "")
    )


def _list_signature(rows: list[dict[str, str]]) -> str:
    values = [[row["url"], row["title"], row["date"]] for row in rows]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _wait_for_page(page: Page, target_page: int, previous_signature: str) -> None:
    page.wait_for_function(
        """({target, previousSignature}) => {
            const current = document.querySelector('#pagestr .a_cur');
            const values = Array.from(document.querySelectorAll('#zpgg li')).map(node => {
                const anchor = node.querySelector('p.pa a[href*="itemid="]');
                const date = node.querySelector('time span');
                return [anchor?.href || '', anchor?.textContent.trim() || '', date?.textContent.trim() || ''];
            });
            const signature = JSON.stringify(values);
            return current?.dataset.page === String(target)
                && values.length > 0
                && signature !== previousSignature;
        }""",
        arg={"target": target_page, "previousSignature": previous_signature},
        timeout=20000,
    )


def _await_xhr(
    page: Page,
    responses: list[Response],
    predicate: Callable[[Response], bool],
    label: str,
) -> Response:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for response in responses:
            if predicate(response):
                return response
        page.wait_for_timeout(100)
    raise TimeoutError(f"{SCHOOL_NAME}{label} XHR 超时")


def _xhr_identity(response: Response) -> tuple[str, int] | None:
    if LIST_API_PATH not in response.url or response.request.method != "POST":
        return None
    payload = parse_qs(response.request.post_data or "")
    action = (payload.get("action") or [""])[0]
    page_text = (payload.get("pageindex") or [""])[0]
    if action not in {"joblist2", "getpage"} or not page_text.isdigit():
        return None
    return action, int(page_text)


def _save_observed_xhrs(
    raw_dir: Path,
    responses: list[Response],
    saved_response_ids: set[int] | None = None,
) -> None:
    saved = saved_response_ids if saved_response_ids is not None else set()
    attempts: dict[tuple[str, int], int] = {}
    for observation_index, response in enumerate(responses, start=1):
        identity = _xhr_identity(response)
        if identity is None:
            continue
        attempts[identity] = attempts.get(identity, 0) + 1
        attempt = attempts[identity]
        if id(response) in saved:
            continue
        action, page_number = identity
        base = (
            f"list_api_page_{page_number:03d}.json"
            if action == "joblist2"
            else f"pager_api_page_{page_number:03d}.html"
        )
        base_path = Path(base)
        filename = (
            base
            if attempt == 1
            else f"{base_path.stem}_attempt_{attempt:03d}{base_path.suffix}"
        )
        metadata = {
            **_xhr_meta(response, page_number),
            "observation_index": observation_index,
            "observation_attempt": attempt,
        }
        try:
            if action == "joblist2":
                save_raw_json(raw_dir / filename, response.body(), metadata)
            else:
                save_raw_html(raw_dir / filename, response.body(), metadata)
            saved.add(id(response))
        except Exception as exc:
            save_error_meta(raw_dir / f"{Path(filename).stem}_body_error.error", {
                **metadata,
                "error": f"{type(exc).__name__}: {exc}",
            })


def _save_observed_documents(
    raw_dir: Path,
    responses: list[Response],
    stem: str,
    saved_response_ids: set[int],
) -> None:
    attempt = 0
    for observation_index, response in enumerate(responses, start=1):
        request = response.request
        if getattr(request, "resource_type", "") != "document":
            continue
        attempt += 1
        if id(response) in saved_response_ids:
            continue
        suffix = "" if attempt == 1 else f"_attempt_{attempt:03d}"
        filename = f"{stem}_response{suffix}.html"
        metadata = {
            **_http_meta(response),
            "observation_index": observation_index,
            "observation_attempt": attempt,
        }
        try:
            save_raw_html(raw_dir / filename, response.body(), metadata)
            saved_response_ids.add(id(response))
        except Exception as body_exc:
            save_error_meta(raw_dir / f"{Path(filename).stem}_body_error.error", {
                **metadata,
                "error": f"{type(body_exc).__name__}: {body_exc}",
            })


def _save_failure_snapshot(
    raw_dir: Path,
    page: Page,
    stem: str,
    exc: BaseException,
    observed_responses: list[Response],
    saved_response_ids: set[int],
    trigger: Response | None,
    requested_url: str,
    operation_responses: list[Response] | None = None,
) -> None:
    _save_observed_xhrs(raw_dir, observed_responses, saved_response_ids)
    _save_observed_documents(
        raw_dir,
        operation_responses if operation_responses is not None else observed_responses,
        stem,
        saved_response_ids,
    )
    try:
        save_raw_html(
            raw_dir / f"{stem}.html",
            page.content(),
            _rendered_meta(page.url, trigger, interaction="failure_snapshot"),
        )
    except Exception as dom_exc:
        save_error_meta(raw_dir / f"{stem}_dom_error.error", {
            "url": page.url,
            "method": "BROWSER",
            "artifact_type": "rendered_dom",
            "error": f"{type(dom_exc).__name__}: {dom_exc}",
        })
    save_error_meta(raw_dir / f"{stem}_error.error", {
        "url": page.url,
        "requested_url": requested_url,
        "method": "BROWSER",
        "trigger_response": _http_meta(trigger) if trigger is not None else None,
        "error": f"{type(exc).__name__}: {exc}",
    })


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pages = older_pages = detail_success = detail_failed = 0
    matched: list[dict[str, str]] = []
    with ChromeCdpSession() as chrome:
        page = chrome.new_page()
        observed_responses: list[Response] = []

        def observe_response(response: Response) -> None:
            observed_responses.append(response)

        page.on("response", observe_response)
        saved_response_ids: set[int] = set()
        operation_start = 0
        document_response: Response | None = None
        list_trigger: Response | None = None
        failure_stem = "list_page_001"
        try:
            document_response = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
            list_trigger = document_response
            if document_response is None:
                raise RuntimeError(f"{SCHOOL_NAME}列表页加载失败：no_response")
            save_raw_html(
                raw_dir / "list_document_page_001.html",
                document_response.body(),
                _http_meta(document_response, page=1),
            )
            saved_response_ids.add(id(document_response))
            if document_response.status != 200:
                raise RuntimeError(f"{SCHOOL_NAME}列表页加载失败：status={document_response.status}")
            first_xhr = _await_xhr(
                page, observed_responses, lambda value: _is_list_xhr(value, 1), "首页列表"
            )
            list_trigger = first_xhr
            _save_observed_xhrs(raw_dir, observed_responses, saved_response_ids)
            if first_xhr.status != 200:
                raise RuntimeError(f"{SCHOOL_NAME}首页 XHR 失败：status={first_xhr.status}")
            first_pager_xhr = _await_xhr(
                page, observed_responses, lambda value: _is_pager_xhr(value, 1), "首页分页器"
            )
            list_trigger = first_pager_xhr
            _save_observed_xhrs(raw_dir, observed_responses, saved_response_ids)
            if first_pager_xhr.status != 200:
                raise RuntimeError(f"{SCHOOL_NAME}首页分页器 XHR 失败：status={first_pager_xhr.status}")
            page.locator("#zpgg li").first.wait_for(timeout=20000)
            page.locator("#pagestr .a_cur[data-page='1']").wait_for(timeout=20000)
            list_trigger = document_response
            for page_no in range(1, MAX_PAGES + 1):
                rendered = page.content()
                save_raw_html(
                    raw_dir / f"list_page_{page_no:03d}.html",
                    rendered,
                    _rendered_meta(
                        page.url,
                        list_trigger,
                        page=page_no,
                        interaction="navigate" if page_no == 1 else "xhr_pagination",
                    ),
                )
                rows = _rows(rendered)
                pages += 1
                dates = []
                for row in rows:
                    try:
                        parsed = parse_source_datetime(row["date"])
                    except ValueError:
                        continue
                    dates.append(parsed)
                    if in_range(row["date"], start, end):
                        matched.append(row)
                LOGGER.info("[%s] page=%d records=%d", TAG, page_no, len(rows))
                older_pages = older_pages + 1 if dates and all(value < start for value in dates) else 0
                next_link = page.locator("#pagestr .a_next")
                if older_pages >= 2 or next_link.count() == 0:
                    break
                target_text = next_link.get_attribute("data-page") or ""
                onclick = next_link.get_attribute("onclick") or ""
                if not target_text.isdigit() or not onclick:
                    break
                target_page = int(target_text)
                if target_page <= page_no:
                    break
                failure_stem = f"list_page_{target_page:03d}"
                previous_signature = _list_signature(rows)
                operation_start = len(observed_responses)
                with page.expect_response(
                    lambda value: _is_list_xhr(value, target_page),
                    timeout=20000,
                ) as response_info:
                    next_link.click()
                page_xhr = response_info.value
                list_trigger = page_xhr
                _save_observed_xhrs(raw_dir, observed_responses, saved_response_ids)
                if page_xhr.status != 200:
                    raise RuntimeError(f"{SCHOOL_NAME}第 {target_page} 页 XHR 失败：status={page_xhr.status}")
                pager_xhr = _await_xhr(
                    page,
                    observed_responses,
                    lambda value: _is_pager_xhr(value, target_page),
                    f"第 {target_page} 页分页器",
                )
                _save_observed_xhrs(raw_dir, observed_responses, saved_response_ids)
                if pager_xhr.status != 200:
                    list_trigger = pager_xhr
                    raise RuntimeError(
                        f"{SCHOOL_NAME}第 {target_page} 页分页器 XHR 失败：status={pager_xhr.status}"
                    )
                _wait_for_page(page, target_page, previous_signature)
        except Exception as exc:
            _save_failure_snapshot(
                raw_dir,
                page,
                failure_stem,
                exc,
                observed_responses,
                saved_response_ids,
                list_trigger,
                LIST_URL,
                observed_responses[operation_start:],
            )
            raise

        simple = []
        for row in matched:
            url = row["url"]
            source_id = url.split("itemid=", 1)[-1].split("&", 1)[0]
            title_matches = not filters.get("position") or contains(row["title"], filters["position"])
            if not title_matches:
                continue
            detail_response: Response | None = None
            operation_start = len(observed_responses)
            try:
                detail_response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if detail_response is None:
                    raise RuntimeError("detail no_response")
                save_raw_html(
                    raw_dir / f"detail_{source_id}_response.html",
                    detail_response.body(),
                    _http_meta(detail_response),
                )
                saved_response_ids.add(id(detail_response))
                if detail_response.status != 200:
                    status = detail_response.status
                    raise RuntimeError(f"detail status={status}")
                page.locator("#info1 .edit-info").wait_for(timeout=20000)
                rendered = page.content()
                save_raw_html(
                    raw_dir / f"detail_{source_id}.html", rendered,
                    _rendered_meta(page.url, detail_response),
                )
                soup = BeautifulSoup(rendered, "lxml")
                content_node = soup.select_one("#info1 .edit-info")
                content = str(content_node or "")
                text = html_to_text(content)
                detail_success += 1
                if filters.get("keyword") and not contains(f"{row['title']} {text}", filters["keyword"]):
                    continue
                simple.append({
                    "公司": "", "岗位": row["title"], "岗位上新时间": row["date"],
                    "JD": text, "投递方式": extract_application_methods(content), "原始链接": url,
                })
            except Exception as exc:
                detail_failed += 1
                _save_failure_snapshot(
                    raw_dir,
                    page,
                    f"detail_{source_id}",
                    exc,
                    observed_responses,
                    saved_response_ids,
                    detail_response,
                    url,
                    observed_responses[operation_start:],
                )
                keyword = filters.get("keyword", "")
                if not keyword or contains(row["title"], keyword):
                    simple.append({
                        "公司": "", "岗位": row["title"], "岗位上新时间": row["date"],
                        "JD": "", "投递方式": "", "原始链接": url,
                    })
        LAST_STATS = {
            "strategy": "external_chrome_cdp_rendered_html", "pages": pages,
            "matched": len(simple), "details_success": detail_success, "details_failed": detail_failed,
        }
        return simple

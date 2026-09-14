#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = frozenset({
    "access_token",
    "accesstoken",
    "api_key",
    "apikey",
    "auth",
    "auth_code",
    "authorization",
    "assertion",
    "authcode",
    "code",
    "cookie",
    "csrf",
    "csrf_token",
    "csrftoken",
    "email",
    "credential",
    "id_token",
    "idtoken",
    "key",
    "jwt",
    "mail",
    "mobile",
    "password",
    "passwd",
    "phone",
    "pwd",
    "secret",
    "session",
    "session_id",
    "sessionid",
    "saml_art",
    "samlart",
    "sig",
    "sign",
    "signature",
    "token",
    "ticket",
    "xsrf_token",
    "xsrftoken",
})


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.username is not None or parts.password is not None:
        hostname = parts.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parts.port}" if parts.port is not None else hostname
    query = urlencode([
        (
            key,
            "<redacted>" if key.lower().replace("-", "_") in _SENSITIVE_QUERY_KEYS else value,
        )
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ])
    fragment = "<redacted>" if parts.fragment else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def _request_log_payload(request: object, *, show_post_data: bool) -> dict[str, object]:
    post_data = request.post_data
    payload: dict[str, object] = {
        "kind": "request",
        "method": request.method,
        "url": _safe_url(request.url),
        "has_post_data": post_data is not None,
        "post_data_bytes": len(post_data.encode("utf-8")) if post_data is not None else 0,
    }
    if show_post_data and post_data is not None:
        payload["post_data"] = post_data
    return payload


async def inspect(url: str, seconds: int, *, show_post_data: bool = False) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        page = await browser.new_page()

        def log_request(request: object) -> None:
            payload = _request_log_payload(request, show_post_data=show_post_data)
            print(json.dumps(payload, ensure_ascii=False))

        def log_response(response: object) -> None:
            payload = {
                "kind": "response",
                "status": response.status,
                "url": _safe_url(response.url),
                "content_type": response.headers.get("content-type", ""),
            }
            print(json.dumps(payload, ensure_ascii=False))

        page.on("request", log_request)
        page.on("response", log_response)
        await page.goto(url, wait_until="domcontentloaded")
        print("浏览器探测中，可手动翻页/筛选/打开详情……")
        await asyncio.sleep(seconds)
        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Playwright Network 探测工具（不参与生产采集）")
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument(
        "--show-post-data",
        action="store_true",
        help="显示原始 POST 请求体；可能含凭据或个人信息，仅用于已确认的公开页面",
    )
    args = parser.parse_args()
    asyncio.run(inspect(args.url, args.seconds, show_post_data=args.show_post_data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

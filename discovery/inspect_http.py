#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from bs4 import BeautifulSoup

from common.http import HttpClient


def main() -> int:
    parser = argparse.ArgumentParser(description="新高校 HTTP 探测工具（不参与生产采集）")
    parser.add_argument("url")
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--data", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()
    data = dict(value.split("=", 1) for value in args.data)

    with HttpClient(min_interval=0) as client:
        response = client.request(args.method, args.url, data=data or None)
    print(f"status={response.status_code}")
    print(f"url={response.url}")
    print(f"content_type={response.headers.get('content-type', '')}")
    print(f"bytes={len(response.content)}")
    if "json" in response.headers.get("content-type", ""):
        payload = response.json()
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:10000])
    else:
        soup = BeautifulSoup(response.content, "lxml")
        print("forms=")
        for form in soup.find_all("form"):
            fields = [node.get("name") for node in form.find_all(["input", "select", "textarea"]) if node.get("name")]
            print({"action": form.get("action"), "method": form.get("method"), "fields": fields})
        print("scripts=")
        for script in soup.find_all("script", src=True):
            print(script.get("src"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

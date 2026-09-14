from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import httpx

from common.filters import validate_supported
from common.http import HttpClient, response_metadata
from common.output import save_raw_html

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "苏州大学", "SUDA"
ENTRY_URL = "https://suda.91job.org.cn/sub-station/campus?xxdm=10285"
SUPPORTED_FILTERS = {key: "unsupported" for key in (
    "company", "position", "keyword", "location", "company_nature", "industry",
    "education", "position_type", "employment_type", "student_type",
)}
LAST_STATS: dict[str, int | str] = {}


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    del start, end
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.warning("[%s] strategy=unstable_waf_access", TAG)
    with HttpClient() as client:
        try:
            response = client.get(ENTRY_URL)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET") | {"access": "waf_blocked", "waf_rule_id": "80001"})
            LAST_STATS = {"strategy": "unstable_waf_access", "pages": 1, "matched": 0, "error": f"HTTP {response.status_code}"}
            raise RuntimeError("苏州大学当前网络出口被 91job 创宇盾规则 80001 拦截（HTTP 403）") from exc
        save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET"))
    LAST_STATS = {"strategy": "unstable_waf_access", "pages": 1, "matched": 0, "error": "app shell only"}
    raise RuntimeError("苏州大学当前只取得 SPA 外壳，静态资源随后仍被 WAF 拦截，无法稳定探索业务请求")

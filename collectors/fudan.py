from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import httpx

from common.filters import validate_supported
from common.http import HttpClient, response_metadata
from common.output import save_raw_html

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "复旦大学", "FUDAN"
ENTRY_URL = "https://career.fudan.edu.cn/Zhaopin/zhiweiList.html?jobtype=1"
SUPPORTED_FILTERS = {
    "company": "unsupported", "position": "unsupported", "keyword": "unsupported",
    "location": "unsupported", "company_nature": "unsupported", "industry": "unsupported",
    "education": "unsupported", "position_type": "unsupported",
    "employment_type": "unsupported", "student_type": "unsupported",
}
LAST_STATS: dict[str, int | str] = {}


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    del start, end
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.warning("[%s] strategy=network_blocked_ipv6_502", TAG)
    with HttpClient() as client:
        try:
            response = client.get(ENTRY_URL)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET") | {"access": "network_blocked"})
            LAST_STATS = {"strategy": "network_blocked_ipv6_502", "pages": 1, "matched": 0, "error": f"HTTP {response.status_code}"}
            raise RuntimeError("复旦大学当前网络出口访问失败：站点未发布 IPv6 资源（HTTP 502）") from exc
        save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET"))
    LAST_STATS = {"strategy": "unexplored_after_network_recovery", "pages": 1, "matched": 0}
    raise RuntimeError("复旦大学站点网络已恢复，需重新探索真实列表和详情请求后再启用生产采集")

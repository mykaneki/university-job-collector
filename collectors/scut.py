from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from common.filters import validate_supported
from common.http import HttpClient, response_metadata
from common.output import save_raw_html

LOGGER = logging.getLogger(__name__)
SCHOOL_NAME, TAG = "华南理工大学", "SCUT"
ENTRY_URL = "https://jyzx.scut.edu.cn/"
SUPPORTED_FILTERS = {key: "unsupported" for key in ("company", "position", "keyword", "location", "company_nature", "industry", "education", "position_type", "employment_type", "student_type")}
LAST_STATS: dict[str, int | str] = {}


def collect(start: datetime, end: datetime, filters: dict[str, str], raw_dir: Path) -> list[dict[str, str]]:
    del start, end
    global LAST_STATS
    validate_supported(filters, SUPPORTED_FILTERS, SCHOOL_NAME)
    raw_dir.mkdir(parents=True, exist_ok=True)
    with HttpClient() as client:
        response = client.get(ENTRY_URL)
        save_raw_html(raw_dir / "list_page_001.html", response.content, response_metadata(response, method="GET") | {"access": "cas_login_required"})
    LAST_STATS = {"strategy": "login_required", "pages": 1, "matched": 0, "error": "CAS login required"}
    raise RuntimeError("华南理工大学就业指导中心外网入口跳转统一身份认证，公开招聘列表不可见")

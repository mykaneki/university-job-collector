from __future__ import annotations

import json
import logging
from pathlib import Path

from collectors import whu
from common.output import save_simple_json
from common.time import parse_cli_datetime


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    run_dir = root / "output" / "20260909_whu_revised"
    items = whu.collect(
        parse_cli_datetime("2026-09-06 00:00:00"),
        parse_cli_datetime("2026-09-08 23:59:59", is_end=True),
        {},
        run_dir / "raw" / "whu",
    )
    items = save_simple_json(run_dir / "simple" / "whu.json", items)
    summary = {"run_dir": str(run_dir), "school": "whu", "stats": whu.LAST_STATS, "simple_count": len(items)}
    (run_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

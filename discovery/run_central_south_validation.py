from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from pathlib import Path

from common.output import save_simple_json
from common.time import parse_cli_datetime


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    run_dir = root / "output" / datetime.now().strftime("%Y%m%d_%H%M%S_central_south")
    start = parse_cli_datetime("2026-09-06 00:00:00")
    end = parse_cli_datetime("2026-09-08 23:59:59", is_end=True)
    summary: dict[str, object] = {"run_dir": str(run_dir), "schools": {}}
    for code in ("hust", "whu", "wut", "csu", "hnu", "sysu", "scut", "xmu"):
        module = importlib.import_module(f"collectors.{code}")
        error = ""
        try:
            items = module.collect(start, end, {}, run_dir / "raw" / code)
        except Exception as exc:
            items = []
            error = f"{type(exc).__name__}: {exc}"
            logging.exception("[%s] validation failed", code.upper())
        normalized = save_simple_json(run_dir / "simple" / f"{code}.json", items)
        summary["schools"][code] = {"stats": module.LAST_STATS, "simple_count": len(normalized), "error": error}
        (run_dir / "run_summary.json").parent.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

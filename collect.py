#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from pathlib import Path

from common.filters import parse_filter_args
from common.output import save_simple_json
from common.time import now_shanghai, parse_cli_datetime


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "schools.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按高校、时间范围和确定性筛选条件采集招聘信息")
    parser.add_argument("--schools", required=True, help="逗号分隔的学校代码，如 pku,tsinghua")
    parser.add_argument("--start", required=True, help="开始时间，Asia/Shanghai，包含边界")
    parser.add_argument("--end", required=True, help="结束时间，Asia/Shanghai，包含边界")
    parser.add_argument("--filter", action="append", default=[], metavar="KEY=VALUE", help="可重复传入")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "output")
    return parser.parse_args()


def load_config() -> dict[str, dict[str, object]]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def create_run_dir(output_root: Path) -> Path:
    base = now_shanghai().strftime("%Y%m%d_%H%M%S")
    candidate = output_root / base
    suffix = 1
    while candidate.exists():
        candidate = output_root / f"{base}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        start = parse_cli_datetime(args.start)
        end = parse_cli_datetime(args.end, is_end=True)
        if start > end:
            raise ValueError("start 不能晚于 end")
        filters = parse_filter_args(args.filter)
        schools = [value.strip() for value in args.schools.split(",") if value.strip()]
        if not schools:
            raise ValueError("schools 不能为空")
        if len(set(schools)) != len(schools):
            raise ValueError("schools 不能重复")
        config = load_config()
        unknown = [school for school in schools if school not in config]
        if unknown:
            raise ValueError(f"未配置学校：{', '.join(unknown)}")
        modules = {school: importlib.import_module(str(config[school]["collector"])) for school in schools}
        for school, module in modules.items():
            from common.filters import validate_supported

            validate_supported(filters, module.SUPPORTED_FILTERS, str(config[school]["name"]))
    except (ValueError, ImportError, KeyError, json.JSONDecodeError) as exc:
        print(f"参数/配置错误：{exc}", file=sys.stderr)
        return 2

    run_dir = create_run_dir(args.output_root.resolve())
    simple_dir = run_dir / "simple"
    all_items: list[dict[str, str]] = []
    summary: dict[str, object] = {
        "run_dir": str(run_dir),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "filters": filters,
        "schools": {},
    }
    failed = False

    for school in schools:
        module = modules[school]
        try:
            items = module.collect(start=start, end=end, filters=filters, raw_dir=run_dir / "raw" / school)
            saved = save_simple_json(simple_dir / f"{school}.json", items)
            all_items.extend(saved)
            summary["schools"][school] = dict(module.LAST_STATS)
        except Exception as exc:
            failed = True
            logging.exception("[%s] collection failed", school)
            save_simple_json(simple_dir / f"{school}.json", [])
            summary["schools"][school] = {"error": f"{type(exc).__name__}: {exc}"}

    all_saved = save_simple_json(simple_dir / "all.json", all_items)
    summary["all_records"] = len(all_saved)
    summary["completed_at"] = now_shanghai().isoformat()
    (run_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"output={run_dir}")
    for school in schools:
        print(f"{school}={json.dumps(summary['schools'][school], ensure_ascii=False)}")
    print(f"all_records={len(all_saved)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

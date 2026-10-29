import argparse
import dataclasses
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import __doc__ as DESCRIPTION
from .collect import Collector
from .config import load_config, load_token
from .gitdiff import PartialRepo, collect_diffs
from .http import JsonClient
from .patronus import Patronus
from .rawstore import RawStore
from .teamcity import TeamCity

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "collector.toml"

log = logging.getLogger("collector")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m collector", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="fetch new finished robots and master runs into data/raw")
    collect.add_argument("--lookback-days", type=int, help="override collect.lookback_days")
    collect.add_argument("--workers", type=int, help="override collect.workers")
    collect.add_argument("--no-master", action="store_true", help="skip aggregator runs on master")
    collect.add_argument("--dry-run", action="store_true", help="only report how many robots are new")

    diffs = commands.add_parser("diffs", help="compute git diffs of collected robots")
    diffs.add_argument("--retry-failed", action="store_true", help="also redo diffs that failed before")

    normalize = commands.add_parser("normalize", help="rebuild data/clean Parquet tables from data/raw")
    normalize.add_argument("--since", type=date.fromisoformat, help="only partitions from this date (YYYY-MM-DD)")

    commands.add_parser("all", help="collect, diffs, then normalize the recent partitions")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if workers := getattr(args, "workers", None):
        cfg = dataclasses.replace(cfg, workers=workers)
    _setup_logging(cfg.log_dir, args.command)
    store = RawStore(cfg.raw_dir)
    lookback_days = getattr(args, "lookback_days", None) or cfg.lookback_days

    if args.command in ("collect", "all"):
        collector = Collector(
            cfg,
            TeamCity(JsonClient(cfg.teamcity_url, load_token("TEAMCITY_TOKEN"))),
            Patronus(JsonClient(cfg.patronus_url, load_token("SPACE_TOKEN"))),
            store,
        )
        master_runs = cfg.master_runs and not getattr(args, "no_master", False)
        stats = collector.run(lookback_days, master_runs, getattr(args, "dry_run", False))
        log.info("collect: %s", dict(stats))
    if args.command in ("diffs", "all"):
        stats = collect_diffs(store, PartialRepo(cfg.git_dir, cfg.git_remote), getattr(args, "retry_failed", False))
        log.info("diffs: %s", dict(stats))
    if args.command in ("normalize", "all"):
        from .normalize import normalize as run_normalize  # needs pyarrow, which collect does not

        since = args.since if args.command == "normalize" else date.today() - timedelta(days=lookback_days + 1)
        run_normalize(store, cfg.clean_dir, since)


def _setup_logging(log_dir: Path, command: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_dir / f"{stamp}-{command}.log")],
    )


if __name__ == "__main__":
    main()

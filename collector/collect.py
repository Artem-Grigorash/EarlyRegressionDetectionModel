import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import COLLECTOR_VERSION
from .config import Config
from .http import NotFound
from .patronus import TERMINAL_STATUSES, Patronus
from .rawstore import RawStore, write_json
from .teamcity import TeamCity

log = logging.getLogger(__name__)

# refs/patronus/<author>-<robot uuid>/checks; older robots have no author part.
ROBOT_BRANCH = re.compile(
    r"^refs/patronus/(?:.*-)?(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/checks$"
)
MASTER_BRANCHES = frozenset({"master", "<default>"})


class Collector:
    def __init__(self, cfg: Config, teamcity: TeamCity, patronus: Patronus, store: RawStore):
        self._cfg = cfg
        self._tc = teamcity
        self._patronus = patronus
        self._store = store

    def run(self, lookback_days: int, master_runs: bool, dry_run: bool) -> Counter[str]:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        robots, master = self._discover(since)
        robots = [r for r in sorted(robots) if not self._store.robot_dir(r).exists()]
        master = [b for b in sorted(master) if not self._store.master_dir(b).exists()] if master_runs else []
        log.info("since %s: %d new robots, %d new master runs", since.isoformat(), len(robots), len(master))
        stats: Counter[str] = Counter()
        if dry_run:
            return stats

        with ThreadPoolExecutor(self._cfg.workers) as pool:
            futures = {pool.submit(self._collect_robot, r): f"robot {r}" for r in robots}
            futures |= {pool.submit(self._collect_master, b): f"master build {b}" for b in master}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    stats[future.result()] += 1
                except Exception:
                    log.exception("failed to collect %s", futures[future])
                    stats["error"] += 1
                if i % 50 == 0:
                    log.info("%d/%d done: %s", i, len(futures), dict(stats))
        return stats

    def _discover(self, since: datetime) -> tuple[set[str], set[int]]:
        robots: set[str] = set()
        master: set[int] = set()
        for check in self._cfg.checks:
            for build in self._tc.finished_builds(check, since):
                branch = build.get("branchName") or ""
                if m := ROBOT_BRANCH.match(branch):
                    robots.add(m["id"])
                elif branch in MASTER_BRANCHES:
                    master.add(build["id"])
        return robots, master

    def _collect_robot(self, robot_id: str) -> str:
        robot = self._patronus.robot(robot_id)
        if robot["status"] not in TERMINAL_STATUSES:
            return "robot_pending"  # picked up by a later run
        checks = self._patronus.teamcity_checks(robot_id)
        changes = self._patronus.changes(robot_id)

        missing: list[str] = []
        with self._store.entry(self._store.robot_dir(robot_id)) as d:
            write_json(d / "robot.json.gz", robot)
            write_json(d / "checks.json.gz", checks)
            write_json(d / "changes.json.gz", changes)
            with_vcs_changes = True  # the changed files are the same for every build of the robot
            for check in checks["teamCityChecks"]:
                if check["buildConfigurationId"] not in self._cfg.checks:
                    continue
                for attempt in check.get("attempts") or []:
                    write_json(d / "attempts" / f"{attempt['id']}.json.gz", self._patronus.attempt(attempt["id"]))
                    if build_id := attempt.get("buildId"):
                        missing += self._save_build(d / "builds" / str(build_id), int(build_id), with_vcs_changes)
                        with_vcs_changes = False
            _write_manifest(d, missing)
        return "robot_incomplete" if missing else "robot"

    def _collect_master(self, build_id: int) -> str:
        with self._store.entry(self._store.master_dir(build_id)) as d:
            missing = self._save_build(d, build_id, with_vcs_changes=False)
            _write_manifest(d, missing)
        return "master_incomplete" if missing else "master"

    def _save_build(self, d: Path, build_id: int, with_vcs_changes: bool) -> list[str]:
        """Saves a composite build with its chain and tests; returns what TeamCity no longer had."""
        try:
            build = self._tc.build(build_id)
        except NotFound:
            log.warning("build %d is gone from TeamCity", build_id)
            return [f"build {build_id}"]
        write_json(d / "build.json.gz", build)
        write_json(d / "buckets.json.gz", self._tc.buckets(build_id))
        tests = self._tc.test_occurrences(build_id, self._cfg.test_page_size)
        write_json(d / "tests.json.gz", tests)
        if with_vcs_changes:
            write_json(d / "vcs_changes.json.gz", self._tc.vcs_changes(build_id))

        expected = (build.get("testOccurrences") or {}).get("count", 0)
        if len(tests) != expected:
            log.warning("build %d: %d tests fetched, %d expected", build_id, len(tests), expected)
            return [f"tests of build {build_id}: {len(tests)}/{expected}"]
        return []


def _write_manifest(d: Path, missing: list[str]) -> None:
    write_json(
        d / "manifest.json.gz",
        {
            "collector_version": COLLECTOR_VERSION,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "missing": missing,
        },
    )

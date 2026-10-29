"""data/raw -> data/clean: one Parquet table per entity, partitioned as <table>/date=YYYY-MM-DD/.

The date is the robot's start date (for master runs, the build's start date). A partition is
rewritten from scratch every time it is normalized, so the stage is safe to rerun.
"""

import logging
import re
import shutil
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .rawstore import RawStore, read_json

log = logging.getLogger(__name__)

TS = pa.timestamp("us", tz="UTC")
FLUSH_ROWS = 500_000

SCHEMAS = {
    "robots": pa.schema([
        ("robot_id", pa.string()), ("name", pa.string()),
        ("started_at", TS), ("finished_at", TS),
        ("status", pa.string()), ("type", pa.string()), ("push_mode", pa.string()), ("is_emergency", pa.bool_()),
        ("owner_name", pa.string()), ("owner_email", pa.string()),
        ("source_branch", pa.string()), ("source_revision", pa.string()),
        ("target_branch", pa.string()), ("target_revision", pa.string()), ("test_ref_revision", pa.string()),
        ("review_key", pa.string()), ("commits_number", pa.int32()),
    ]),
    # Every safe-push check of a robot, including skipped ones and ones not collected.
    "checks": pa.schema([
        ("robot_id", pa.string()), ("check_id", pa.string()), ("status", pa.string()),
        ("skip_reason", pa.string()), ("attempt_limit", pa.int32()), ("attempts", pa.int32()),
    ]),
    # One composite aggregator build: an attempt of a robot's check, or a run on master (robot_id is null).
    "runs": pa.schema([
        ("run_id", pa.int64()), ("robot_id", pa.string()), ("check_id", pa.string()),
        ("attempt_no", pa.int32()), ("patronus_attempt_id", pa.string()),
        ("branch_kind", pa.string()), ("branch", pa.string()), ("revision", pa.string()),
        ("queued_at", TS), ("started_at", TS), ("finished_at", TS),
        ("tc_status", pa.string()), ("tc_status_text", pa.string()), ("tests_count", pa.int32()),
        ("patronus_status", pa.string()), ("failed_tests_n", pa.int32()), ("failed_builds_n", pa.int32()),
    ]),
    # Builds of a run's chain; `reused` buckets were taken over from an earlier attempt, not rerun.
    "run_buckets": pa.schema([
        ("run_id", pa.int64()), ("bucket_build_id", pa.int64()), ("bucket_type", pa.string()),
        ("status", pa.string()), ("status_text", pa.string()), ("composite", pa.bool_()), ("reused", pa.bool_()),
    ]),
    # Keyed by bucket, so a bucket shared by several attempts is stored once; join runs via run_buckets.
    # A test can occur several times in one bucket when TeamCity retries it within the build.
    "test_results": pa.schema([
        ("bucket_build_id", pa.int64()), ("occurrence_id", pa.int64()), ("test_id", pa.int64()),
        ("status", pa.string()), ("duration_ms", pa.int64()),
        ("muted", pa.bool_()), ("currently_muted", pa.bool_()), ("new_failure", pa.bool_()), ("ignored", pa.bool_()),
    ]),
    # Names of the tests seen in the partition; the same test_id appears in many partitions.
    "tests": pa.schema([("test_id", pa.int64()), ("name", pa.string())]),
    # Patronus' verdict on each failed test of an attempt.
    "patronus_failures": pa.schema([
        ("run_id", pa.int64()), ("patronus_attempt_id", pa.string()),
        ("test_id", pa.int64()), ("test_name", pa.string()), ("bucket_build_id", pa.int64()),
        ("is_ignored", pa.bool_()), ("ignore_reason", pa.string()),
    ]),
    # source = "git" (with line counts) or "teamcity" (file list only, when the git diff failed).
    "changed_files": pa.schema([
        ("robot_id", pa.string()), ("path", pa.string()), ("change_type", pa.string()),
        ("lines_added", pa.int32()), ("lines_deleted", pa.int32()), ("source", pa.string()),
    ]),
}

GIT_CHANGE_TYPES = {"A": "added", "M": "edited", "D": "removed", "T": "edited"}
OCCURRENCE_ID = re.compile(r"\bid:(\d+)$")


def normalize(store: RawStore, clean_dir: Path, since: date | None) -> None:
    robots_by_date: dict[date, list[Path]] = defaultdict(list)
    for d in store.robot_dirs():
        robots_by_date[_iso(read_json(d / "robot.json.gz")["startDateTime"]).date()].append(d)
    master_by_date: dict[date, list[Path]] = defaultdict(list)
    for d in store.master_dirs():
        build = read_json(d / "build.json.gz", {})
        if started := _tc(build.get("startDate") or build.get("queuedDate")):
            master_by_date[started.date()].append(d)

    for day in sorted(robots_by_date.keys() | master_by_date.keys()):
        if since and day < since:
            continue
        with PartitionWriter(clean_dir, day) as out:
            for d in robots_by_date[day]:
                _robot(d, out)
            for d in master_by_date[day]:
                _master_run(d, out)
        log.info("%s: %d robots, %d master runs", day, len(robots_by_date[day]), len(master_by_date[day]))


class PartitionWriter:
    def __init__(self, clean_dir: Path, day: date):
        self._clean_dir = clean_dir
        self._day = day
        self._buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._writers: dict[str, pq.ParquetWriter] = {}
        self.test_names: dict[int, str] = {}

    def __enter__(self) -> "PartitionWriter":
        for table in SCHEMAS:
            shutil.rmtree(self._partition(table), ignore_errors=True)
        return self

    def __exit__(self, *exc: object) -> None:
        self.write("tests", [{"test_id": k, "name": v} for k, v in self.test_names.items()])
        for table in list(self._buffers):
            self._flush(table)
        for writer in self._writers.values():
            writer.close()

    def _partition(self, table: str) -> Path:
        return self._clean_dir / table / f"date={self._day.isoformat()}"

    def write(self, table: str, rows: list[dict[str, Any]]) -> None:
        buffer = self._buffers[table]
        buffer += rows
        if len(buffer) >= FLUSH_ROWS:
            self._flush(table)

    def _flush(self, table: str) -> None:
        rows = self._buffers.pop(table, [])
        if not rows:
            return
        if (writer := self._writers.get(table)) is None:
            path = self._partition(table)
            path.mkdir(parents=True)
            writer = self._writers[table] = pq.ParquetWriter(path / "part-0.parquet", SCHEMAS[table],
                                                             compression="zstd")
        writer.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS[table]))


def _robot(d: Path, out: PartitionWriter) -> None:
    robot = read_json(d / "robot.json.gz")
    checks = read_json(d / "checks.json.gz")["teamCityChecks"]
    changes = read_json(d / "changes.json.gz")
    robot_id = robot["id"]
    owner = robot.get("owner") or {}
    out.write("robots", [{
        "robot_id": robot_id, "name": robot.get("name"),
        "started_at": _iso(robot.get("startDateTime")), "finished_at": _iso(robot.get("finishDateTime")),
        "status": robot["status"], "type": robot.get("type"), "push_mode": robot.get("pushMode"),
        "is_emergency": robot.get("isEmergency"),
        "owner_name": owner.get("name"), "owner_email": owner.get("email"),
        "source_branch": robot.get("sourceBranch"), "source_revision": robot.get("sourceRevision"),
        "target_branch": robot.get("targetBranch"), "target_revision": robot.get("targetRevision"),
        "test_ref_revision": robot.get("testReferenceRevision"),
        "review_key": robot.get("spaceReviewKey"), "commits_number": changes.get("commitsNumber"),
    }])
    out.write("checks", [{
        "robot_id": robot_id, "check_id": c["buildConfigurationId"], "status": c.get("status"),
        "skip_reason": c.get("skipReason"), "attempt_limit": c.get("attemptLimit"),
        "attempts": len(c.get("attempts") or []),
    } for c in checks])

    seen_buckets: set[int] = set()
    seen_occurrences: set[tuple[int, int]] = set()
    for check in checks:
        for attempt in sorted(check.get("attempts") or [], key=lambda a: a["number"]):
            attempt_file = d / "attempts" / f"{attempt['id']}.json.gz"
            if not attempt_file.exists():
                continue  # a check that is not collected
            details = read_json(attempt_file)
            run_id = int(attempt["buildId"]) if attempt.get("buildId") else None
            build_dir = d / "builds" / str(run_id)
            build = read_json(build_dir / "build.json.gz", {})
            out.write("runs", [{
                **_run_columns(build),
                "run_id": run_id, "robot_id": robot_id, "check_id": check["buildConfigurationId"],
                "attempt_no": attempt["number"], "patronus_attempt_id": attempt["id"], "branch_kind": "patronus",
                "started_at": _tc(build.get("startDate")) or _iso(attempt.get("startedAt")),
                "finished_at": _tc(build.get("finishDate")) or _iso(attempt.get("finishedAt")),
                "patronus_status": attempt.get("status"),
                "failed_tests_n": details.get("failedTestsNumber"), "failed_builds_n": details.get("failedBuildsNumber"),
            }])
            out.write("patronus_failures", [{
                "run_id": run_id, "patronus_attempt_id": attempt["id"],
                "test_id": _int(t.get("testId")), "test_name": t.get("name"), "bucket_build_id": _int(t.get("buildId")),
                "is_ignored": t.get("isIgnoredFailure"), "ignore_reason": t.get("ignoreReason"),
            } for t in details.get("failedTests") or []])
            if run_id is not None:
                _build_contents(build_dir, run_id, out, seen_buckets, seen_occurrences)

    out.write("changed_files", _changed_files(d, robot_id))


def _master_run(d: Path, out: PartitionWriter) -> None:
    build = read_json(d / "build.json.gz")
    out.write("runs", [{
        **_run_columns(build),
        "run_id": build["id"], "robot_id": None, "check_id": build.get("buildTypeId"),
        "attempt_no": None, "patronus_attempt_id": None, "branch_kind": "master",
        "started_at": _tc(build.get("startDate")), "finished_at": _tc(build.get("finishDate")),
        "patronus_status": None, "failed_tests_n": None, "failed_builds_n": None,
    }])
    _build_contents(d, build["id"], out, set(), set())


def _run_columns(build: dict[str, Any]) -> dict[str, Any]:
    revisions = (build.get("revisions") or {}).get("revision") or [{}]
    return {
        "branch": build.get("branchName"), "revision": revisions[0].get("version"),
        "queued_at": _tc(build.get("queuedDate")), "tc_status": build.get("status"),
        "tc_status_text": build.get("statusText"),
        "tests_count": (build.get("testOccurrences") or {}).get("count"),
    }


def _build_contents(build_dir: Path, run_id: int, out: PartitionWriter,
                    seen_buckets: set[int], seen_occurrences: set[tuple[int, int]]) -> None:
    buckets = read_json(build_dir / "buckets.json.gz", [])
    out.write("run_buckets", [{
        "run_id": run_id, "bucket_build_id": b["id"], "bucket_type": b.get("buildTypeId"),
        "status": b.get("status"), "status_text": b.get("statusText"), "composite": b.get("composite", False),
        "reused": b["id"] in seen_buckets,
    } for b in buckets])
    seen_buckets.update(b["id"] for b in buckets)

    rows = []
    for t in read_json(build_dir / "tests.json.gz", []):
        key = (t["build"]["id"], int(OCCURRENCE_ID.search(t["id"])[1]))
        if key in seen_occurrences:
            continue  # the bucket was reused from an earlier attempt
        seen_occurrences.add(key)
        test_id = int(t["test"]["id"])
        out.test_names[test_id] = t["name"]
        rows.append({
            "bucket_build_id": key[0], "occurrence_id": key[1], "test_id": test_id,
            "status": t.get("status"), "duration_ms": t.get("duration"),
            "muted": t.get("muted", False), "currently_muted": t.get("currentlyMuted", False),
            "new_failure": t.get("newFailure", False), "ignored": t.get("ignored", False),
        })
    out.write("test_results", rows)


def _changed_files(d: Path, robot_id: str) -> list[dict[str, Any]]:
    diff = read_json(d / "diff.json.gz", {})
    if "files" in diff:
        return [{
            "robot_id": robot_id, "path": f["path"],
            "change_type": GIT_CHANGE_TYPES.get(f["change_type"], f["change_type"]),
            "lines_added": f["lines_added"], "lines_deleted": f["lines_deleted"], "source": "git",
        } for f in diff["files"]]
    files: dict[str, str] = {}
    for vcs_changes in (d / "builds").glob("*/vcs_changes.json.gz"):
        for change in read_json(vcs_changes):
            for f in (change.get("files") or {}).get("file") or []:
                files[f["file"]] = f.get("changeType")
    return [{
        "robot_id": robot_id, "path": path, "change_type": change_type,
        "lines_added": None, "lines_deleted": None, "source": "teamcity",
    } for path, change_type in files.items()]


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _tc(value: str | None) -> datetime | None:
    return datetime.strptime(value, "%Y%m%dT%H%M%S%z") if value else None


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None

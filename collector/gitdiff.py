import logging
import subprocess
from collections import Counter
from itertools import batched
from pathlib import Path
from typing import Any

from .rawstore import RawStore, read_json, write_json

log = logging.getLogger(__name__)

FETCH_BATCH = 20


class PartialRepo:
    """A bare, shallow, blob-less clone: fetching a commit brings only its trees, and the blobs
    `git diff --numstat` needs are fetched lazily. Commits stay fetchable by SHA after Patronus
    deletes refs/patronus/*, but not forever, so diffs should be taken soon after collection.
    """

    def __init__(self, path: Path, remote: str):
        self._path = path
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            self._git("init", "--bare", "--quiet")
            self._git("remote", "add", "origin", remote)
            self._git("config", "remote.origin.promisor", "true")
            self._git("config", "remote.origin.partialclonefilter", "blob:none")

    def _git(self, *args: str, timeout: float = 900) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(self._path), *args],
            check=True, capture_output=True, text=True, timeout=timeout,
        ).stdout

    def has_commit(self, sha: str) -> bool:
        result = subprocess.run(["git", "--git-dir", str(self._path), "cat-file", "-e", f"{sha}^{{commit}}"],
                                capture_output=True)
        return result.returncode == 0

    def fetch(self, shas: list[str]) -> None:
        """Fetches the commits; one unfetchable SHA must not fail the rest, hence the per-SHA fallback."""
        missing = [s for s in shas if not self.has_commit(s)]
        if not missing:
            return
        try:
            self._fetch(missing)
        except subprocess.CalledProcessError:
            for sha in missing:
                try:
                    self._fetch([sha])
                except subprocess.CalledProcessError as e:
                    log.warning("cannot fetch %s: %s", sha, e.stderr.strip())

    def _fetch(self, shas: list[str]) -> None:
        self._git("fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", "origin", *shas)

    def diff(self, base: str, head: str) -> list[dict[str, Any]]:
        status = self._git("diff", "--no-renames", "--name-status", "-z", base, head).split("\0")[:-1]
        change_types = dict(zip(status[1::2], status[0::2]))
        files = []
        for record in self._git("diff", "--no-renames", "--numstat", "-z", base, head).split("\0")[:-1]:
            added, deleted, path = record.split("\t", 2)
            files.append({
                "path": path,
                "change_type": change_types.get(path),
                # "-" is git's marker for binary files
                "lines_added": None if added == "-" else int(added),
                "lines_deleted": None if deleted == "-" else int(deleted),
            })
        return files


def collect_diffs(store: RawStore, repo: PartialRepo, retry_failed: bool) -> Counter[str]:
    """Writes diff.json.gz (targetRevision..testReferenceRevision) for every robot that has builds."""
    pending = []
    for d in store.robot_dirs():
        if not (d / "builds").exists():
            continue  # the collected checks did not run for this robot
        existing = read_json(d / "diff.json.gz", {})
        if existing and not (retry_failed and "error" in existing):
            continue
        pending.append((d, read_json(d / "robot.json.gz")))
    log.info("%d robots need a diff", len(pending))

    stats: Counter[str] = Counter()
    for chunk in batched(pending, FETCH_BATCH):
        shas = {robot.get(k) for _, robot in chunk for k in ("targetRevision", "testReferenceRevision")}
        repo.fetch(sorted(s for s in shas if s))
        for d, robot in chunk:
            base, head = robot.get("targetRevision"), robot.get("testReferenceRevision")
            result: dict[str, Any] = {"base": base, "head": head}
            if not base or not head:
                result["error"] = "robot has no target or test reference revision"
            else:
                try:
                    result["files"] = repo.diff(base, head)
                except subprocess.CalledProcessError as e:
                    result["error"] = e.stderr.strip()
            stats["error" if "error" in result else "ok"] += 1
            write_json(d / "diff.json.gz", result)
        log.info("diffs: %s", dict(stats))
    return stats

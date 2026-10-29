import gzip
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TMP_SUFFIX = ".tmp"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_json(path: Path, default: Any = None) -> Any:
    if default is not None and not path.exists():
        return default
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


class RawStore:
    """data/raw layout:

    robots/<id[:2]>/<robot_id>/
        robot.json.gz, changes.json.gz, checks.json.gz   Patronus responses
        attempts/<attempt_id>.json.gz                     Patronus attempt incl. failed tests
        builds/<build_id>/                                composite TeamCity build of an attempt
            build.json.gz, buckets.json.gz, tests.json.gz[, vcs_changes.json.gz]
        diff.json.gz                                      written by the diffs stage
        manifest.json.gz
    master/<id[-2:]>/<build_id>/                          aggregator run on master, same build files
    """

    def __init__(self, root: Path):
        self.root = root

    def robot_dir(self, robot_id: str) -> Path:
        return self.root / "robots" / robot_id[:2] / robot_id

    def master_dir(self, build_id: int) -> Path:
        return self.root / "master" / str(build_id)[-2:] / str(build_id)

    def robot_dirs(self) -> Iterator[Path]:
        return self._entries("robots")

    def master_dirs(self) -> Iterator[Path]:
        return self._entries("master")

    def _entries(self, kind: str) -> Iterator[Path]:
        return (p for p in sorted((self.root / kind).glob("*/*")) if not p.name.endswith(TMP_SUFFIX))

    @contextmanager
    def entry(self, final: Path) -> Iterator[Path]:
        """A directory that appears at `final` only once fully written, so a crash leaves nothing half-done."""
        tmp = final.with_name(final.name + TMP_SUFFIX)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            yield tmp
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        tmp.rename(final)

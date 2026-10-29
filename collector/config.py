import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

TOKENS_FILE = Path.home() / ".config" / "early-regression" / "tokens.env"


@dataclass(frozen=True)
class Config:
    teamcity_url: str
    test_page_size: int
    patronus_url: str
    git_remote: str
    checks: tuple[str, ...]
    lookback_days: int
    workers: int
    master_runs: bool
    data_dir: Path

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def clean_dir(self) -> Path:
        return self.data_dir / "clean"

    @property
    def git_dir(self) -> Path:
        return self.data_dir / "git" / "ultimate.git"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        d = tomllib.load(f)
    return Config(
        teamcity_url=d["teamcity"]["url"],
        test_page_size=d["teamcity"]["test_page_size"],
        patronus_url=d["patronus"]["url"],
        git_remote=d["git"]["remote"],
        checks=tuple(d["collect"]["checks"]),
        lookback_days=d["collect"]["lookback_days"],
        workers=d["collect"]["workers"],
        master_runs=d["collect"]["master_runs"],
        data_dir=(path.parent / d["paths"]["data"]).resolve(),
    )


def load_token(name: str) -> str:
    """The token from the environment, falling back to TOKENS_FILE (KEY=VALUE lines)."""
    if value := os.environ.get(name):
        return value
    if TOKENS_FILE.exists():
        for line in TOKENS_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip()
    raise SystemExit(f"{name} is not set: export it or add it to {TOKENS_FILE}")

"""Collects safe-push aggregator results from Patronus, TeamCity and git into a local dataset.

Three stages, each idempotent:
  collect    Patronus robots + TeamCity builds/tests -> data/raw (gzipped API responses, as is)
  diffs      git numstat of every collected robot    -> data/raw/.../diff.json.gz
  normalize  data/raw -> data/clean (Parquet tables, partitioned by date)

The raw layer is the source of truth: TeamCity drops per-test results after ~12 days,
so everything downstream must be rebuildable from data/raw alone.
"""

COLLECTOR_VERSION = 1

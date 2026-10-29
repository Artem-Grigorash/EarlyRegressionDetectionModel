# Project Context: Early Regression Detection Model

This is a thesis repository (Neapolis University Pafos, author: Artem Grigorash).
Topic: **ML-based test selection and prioritization for early regression detection in JetBrains CI.**
Source document with the full rationale: `Thesis pipline.pdf` (repository root).

## Problem

- At JetBrains, a small set of critical tests (the "**aggregator**", i.e. the gate) runs on every push to `main`; if any of them fails, the merge is blocked.
- All other tests run **on a schedule**, with intervals based on each test's priority. The test sets are static and independent of the actual changes.
- Consequences: even a trivial change triggers a full run of ~4 hours (distributed across agents); a regression that slips past the aggregator may only surface hours or days later.
- The classic solution, "test maps" (coverage maps), is hard to keep up to date and has no insight into the *nature* of a change.
- Goal: for each change, run the tests most likely to reveal a problem, ranked so that failing tests come first.

## What we take from the literature (2025 papers: Google, BMW, SAP HANA)

**CI stages:** pre-submit → gate (aggregator) → post-submit.
- JetBrains generally has no pre-submit, and the aggregator is overly sensitive and must not be optimized (every test there has to run).
  → **The thesis focuses on post-submit**, which contains the vast majority of tests.
- Post-submit aims to catch meaningful status transitions (PASS→FAIL; BMW also counts FAIL→PASS). Failures of already-broken or flaky tests carry no new information.
- Initial candidate selection is done by **Bazel** (dependency graph; already integrated into JetBrains infrastructure). ML then ranks and selects tests within that set under a resource budget.

**Execution schemes being compared (second axis of the thesis):**
- **BMW-style:** post-submit triggered after every merge / PR; each target is scored independently, ranked, and the top-N within budget are executed.
- **Google-style:** fixed-length cycles (20 minutes at Google); compute the diff between the branch now and at the start of the cycle → Bazel → ranking → run top-N.
- Both companies keep periodic full runs (Google's "Comprehensive Cycle", BMW's full suite before releases), since no model guarantees 100% recall.

**Ranking formulations (from the SAP HANA paper):**
- *Pointwise:* predict the utility / failure probability of each test independently, then sort.
- *Pairwise:* learn the relative order of pairs of tests.
- *Listwise:* optimize the quality of the whole list (positions of failing tests).

**SAP HANA findings:** LightGBM Ranker gives the best balance of quality and training time (XGBoost is close; CatBoost is comparable in quality but slow; Random Forest is clearly worse). The best RL variant is pairwise A2C (pairwise TRPO on a reduced dataset), but both are outperformed by LightGBM. The pairwise implementation is based on bubble sort, O(n²); listwise did not deliver the expected benefits.

**Google:** GBDT classifier (Yggdrasil Decision Forests), pointwise: predicts whether a target has developed a *new* failure due to changes since the previous cycle.
**BMW:** PR-DQL, pointwise Deep Q-Learning with a reward that depends on the CI stage; a network of 4 fully connected layers with dropout and L2 regularization.

## The four models compared in the thesis

| # | Model | Source | Type | Formulation |
|---|-------|--------|------|-------------|
| 1 | GBDT classifier | Google | Supervised | Pointwise (binary "new failure" classification) |
| 2 | Deep Q-Learning (PR-DQL) | BMW | RL | Pointwise (test priority) |
| 3 | LightGBM Ranker | SAP HANA | Supervised | Pairwise (LambdaRank) |
| 4 | Pairwise A2C | SAP HANA | RL | Pairwise |

**Thesis goal:** train all four pipelines on a JetBrains test dataset and compare them by:
- regression detection quality (recall of failing tests within budget, ranking metrics such as APFD / NDCG);
- feedback latency (time from defect introduction to detection);
- computational overhead (number of tests executed, training and inference time).

## Data plan

- Stage 1: pre-train on **aggregator (safe-push gate) results**: every change runs the full gate, so labels are complete and the culprit is the change itself. Aggregator tests are structurally the same as post-submit ones (a priority subset).
- Stage 2: fine-tune on a small group of post-submit tests that are fully on Bazel (Bazel covers ~1/4 of the repo).
- Features must exist in both domains (no Bazel-only core features, no test-ID features). Train/test splits are by time only.

## Data collection (`collector/`)

`python -m collector collect | diffs | normalize | all` (config: `config/collector.toml`; tokens `TEAMCITY_TOKEN`, `SPACE_TOKEN` from env or `~/.config/early-regression/tokens.env`). Output goes to `data/` (gitignored): `raw/` = gzipped API responses (source of truth), `clean/` = Parquet tables per date.

Facts established against the live APIs (2026-10):
- **TeamCity keeps per-test results only ~12 days**, so there is no long history: the collector must run regularly (at least every few days) and accumulate.
- A safe push = Patronus "robot" (`https://patronus.labs.jb.gg/app/rest/v1`, Space token). Its TeamCity branch is `refs/patronus/[<author>-]<robot uuid>/checks`, so robots are discovered via aggregator builds in TeamCity. Patronus has no robot listing endpoint.
- Main check: `ijplatform_master_Idea_Tests_Aggregator` (composite, ~124 buckets, ~36k tests, one `testOccurrences` call). ~450–500 robots per weekday; on a sample day ~30 robots had blocking test failures.
- Retry attempts rerun only failed buckets (the rest are reused): fail-then-pass on the same revision = flaky.
- Patronus' per-attempt `failedTests` carry `isIgnoredFailure`/`ignoreReason`, but "blocking" failures are still noisy (environment/master-wide breakage hits many unrelated robots at once). Labels need cross-robot and master-baseline filtering.
- Diff: `git diff targetRevision..testReferenceRevision`. Commits stay fetchable by SHA after the refs are deleted; the collector uses a blob-less shallow bare repo in `data/git/`.

## Repository state

Data collector written (`collector/`); no labels/features/models yet. `main.py` is a PyCharm stub. Update this file when key decisions are made (labels, features, metrics, module layout).

## Terminology

- **target:** a Bazel target (a test or group of tests).
- **aggregator:** JetBrains' gate test set, run before merging into `main`.
- **regression:** a PASS→FAIL transition caused by a specific change (not flaky, and not a test that was already broken).

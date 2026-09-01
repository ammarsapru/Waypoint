# External repository benchmarks

This suite validates the analyzer against shallow clones of established Python
repositories. Clones live outside the application workspace by default at
`C:\tmp\waypoint-benchmarks`.

The benchmark verifies:

- Parse success and performance
- Unique, stable node and edge identities
- Valid graph endpoints
- Valid source spans
- Evidence-status invariants
- Correct package names for flat and `src/` layouts
- Known architectural modules, classes, methods, and functions
- First-party import resolution

Run:

```powershell
.\.venv\Scripts\python.exe benchmarks\run_repositories.py `
  --fixtures-root C:\tmp\waypoint-benchmarks `
  --output benchmarks\results
```

Use `--repository flask` to run one fixture. Results are written as JSON and
Markdown so changes in analyzer quality can be reviewed rather than inferred
from node counts alone.

## Retrieval benchmark

The local retrieval benchmark measures file Hit@5, Hit@10, mean reciprocal rank,
nDCG@10, indexing/restoration time, cold/warm query latency, latency percentiles, and
process peak memory against known evidence files:

```powershell
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --output benchmarks\results\retrieval-latest.json
```

Add repository-specific cases by supplying another JSON file with `--cases`.

V3 also contains a 50-question, three-repository development benchmark with explicit
development/held-out labels, required path groups, forbidden distractors, repository and
language slices, and an adjudication gate. Pending seed labels are rejected unless the
caller explicitly opts into a non-release diagnostic:

```powershell
.\.venv\Scripts\python.exe benchmarks\run_retrieval_v3.py `
  --repository waypoint=. `
  --repository flask=.waypoint-clones/pallets--flask--214023150b `
  --repository praw=.waypoint-clones/haxtheweb--praw--69db9269ab `
  --allow-unadjudicated `
  --output benchmarks\results\retrieval-v3.json
```

See `documentation/benchmarks/history.md` for formulas, limitations, and the preserved
V2-to-V3 comparison.

## V4 independent-agent benchmark

V4 uses a separate 50-case, four-repository corpus. Each fixed endpoint independently
chooses production repository tools; candidate results, explicit source reads, and final
citations are scored separately. The benchmark also records schema corrections, retries,
tokens, latency, and trace-correlated hook/log quality.

```powershell
.\.venv\Scripts\python.exe benchmarks\run_agentic_retrieval_v4.py `
  --repository waypoint=. `
  --repository flask=.waypoint-clones\pallets--flask--214023150b `
  --repository praw=.waypoint-clones\haxtheweb--praw--69db9269ab `
  --repository open-notebook=.waypoint-clones\lfnovo--open-notebook--5d0a28a4b0 `
  --endpoint openrouter:poolside/laguna-m.1:free `
  --endpoint openrouter:poolside/laguna-xs-2.1:free `
  --max-cases 50 `
  --allow-unadjudicated `
  --confirm-live-provider-usage `
  --output benchmarks\results\agentic-v4-poolside-campaign.json
```

The current execution status and non-transient OpenRouter daily-quota blocker are
documented in `documentation/benchmarks/history.md`.

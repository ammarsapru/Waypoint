# Claude Code versus OpenRouter quality benchmark

Analysis: `db40e1f66b094d829629a4e2fdf42d02` · Tasks: 4

| Endpoint | Success | Concept recall | Expected-file recall | p50 latency | p95 latency | TTFT p50 | TTFT samples | Uncached input | Cache create | Cache read | Effective input | Output tokens | Tool calls | Citations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| claude-code:sonnet | 100.0% | 96.4% | 87.5% | 27540.568 ms | 47076.934 ms | N/A | 0 | 12 | 20237 | 111959 | 132208 | 11266 | 4 | 23 |
| openrouter:openrouter/free | 50.0% | 90.0% | 75.0% | 34434.041 ms | 48910.665 ms | 23944.391 | 2 | 10430 | 0 | 0 | 10430 | 3234 | 2 | 4 |

OpenRouter TTFT is measured from request dispatch to the first meaningful streaming content or tool-call delta. Claude Agent SDK does not expose raw token deltas, so its TTFT remains N/A and the first assistant-event timing is retained only in the status field.

Scoring rule: non-empty validated answer, citations, >=60% required-concept recall, and at least one expected file cited.


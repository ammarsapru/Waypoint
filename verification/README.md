# Verification evidence

This folder is the auditable Phase 3 verification package. It separates stable tests
from network- and model-dependent demonstrations.

| Area | Location | Network/model usage |
|---|---|---|
| Coverage threshold | [`coverage/README.md`](coverage/README.md) | None |
| Unit and integration evidence | [`tested-evidence/README.md`](tested-evidence/README.md) | None |
| Real HTTP E2E against Flask | [`http-e2e/README.md`](http-e2e/README.md) | GitHub only when no local checkout is supplied |
| Live multi-step agent demo | [`live-multistep/README.md`](live-multistep/README.md) | Yes; explicit confirmation required |
| Same-question two-model comparison | [`model-comparison/README.md`](model-comparison/README.md) | Yes; two configured endpoints |
| Deployment constraints | [`deployment/README.md`](deployment/README.md) | None |
| Operational quality, retrieval, model accuracy, memory and performance | [`operational-evaluation/README.md`](operational-evaluation/README.md) | Live model run is opt-in |

Flask is the reference repository because it is a familiar, actively structured Python
web framework of moderate size. It is large enough to exercise `src/` layout, imports,
classes, decorators, CLI entry points, request routing, tests, graph retrieval, and
onboarding without the runtime and memory cost of a very large monorepo.

Generated JSON and JSONL results belong in `verification/results/`. Live tests are opt-in
so ordinary CI runs remain deterministic and do not spend model credits.

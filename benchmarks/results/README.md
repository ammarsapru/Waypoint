# Validated repository matrix

The analyzer is validated against shallow GitHub snapshots with deterministic
architecture assertions. Generated machine-readable reports remain beside this
file.

| Repository | Commit | Python files | Nodes | Edges | Analysis time | Peak memory | Result |
|---|---:|---:|---:|---:|---:|---:|---:|
| Django | `3d34265` | 2,927 | 46,437 | 82,649 | 47.12 s | 464.40 MiB | Pass |
| Flask | `36e4a82` | 83 | 1,704 | 2,336 | 1.18 s | 55.33 MiB | Pass |
| Requests | `f361ead` | 37 | 845 | 1,424 | 0.78 s | 62.52 MiB | Pass |
| ItsDangerous | `672971d` | 15 | 160 | 324 | 0.12 s | 62.52 MiB | Pass |

Checks include:

- Expected repository size
- Parse success threshold
- Unique node and edge IDs
- Valid edge endpoints
- Valid source spans
- Evidence-status invariants
- Known architectural modules and symbols
- Correct `src/` package layout
- Obvious first-party import resolution
- Role-specific onboarding route creation
- Route diversity across roles
- Source-backed contribution mission generation
- Iterative architecture-cycle analysis

Django contains an intentional invalid-syntax test fixture at
`tests/test_runner_apps/tagged/tests_syntax_error.py`. It is correctly reported
as a parse diagnostic, and the remaining 2,926 files are analyzed.

Static `may_call` edges remain inferences rather than runtime proof. Dynamic
dispatch, framework registration, callbacks, and compatibility shims remain
explicitly unresolved unless runtime tracing supplies evidence.

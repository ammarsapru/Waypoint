# Analyzer external-repository benchmark

Generated: 2026-07-18T23:56:41.563752+00:00

| Repository | Size | Commit | Files | Nodes | Edges | Time | Peak memory | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Flask | medium | 36e4a82 | 83 | 1704 | 2336 | 1.20s | 72.61 MiB | PASS |
| Requests | small-medium | f361ead | 37 | 845 | 1424 | 0.67s | 79.13 MiB | FAIL |
| ItsDangerous | small | 672971d | 15 | 160 | 324 | 0.11s | 79.13 MiB | PASS |

## Flask

- PASS — repository_size: Discovered 83 Python files
- PASS — parse_success: Parse failure ratio 0.000%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- PASS — architectural_modules: All expected architectural modules were found
- PASS — architectural_symbols: All expected architectural symbols were found
- PASS — source_layout: Package names do not leak the source-directory prefix
- PASS — first_party_import_resolution: No obvious first-party imports remain unresolved

## Requests

- PASS — repository_size: Discovered 37 Python files
- PASS — parse_success: Parse failure ratio 0.000%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- PASS — architectural_modules: All expected architectural modules were found
- PASS — architectural_symbols: All expected architectural symbols were found
- PASS — source_layout: Package names do not leak the source-directory prefix
- FAIL — first_party_import_resolution: 1 obvious first-party imports remain unresolved, e.g. ['from requests.packages.urllib3.poolmanager import PoolManager']

## ItsDangerous

- PASS — repository_size: Discovered 15 Python files
- PASS — parse_success: Parse failure ratio 0.000%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- PASS — architectural_modules: All expected architectural modules were found
- PASS — architectural_symbols: All expected architectural symbols were found
- PASS — source_layout: Package names do not leak the source-directory prefix
- PASS — first_party_import_resolution: No obvious first-party imports remain unresolved

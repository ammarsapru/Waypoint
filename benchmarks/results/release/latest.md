# Analyzer external-repository benchmark

Generated: 2026-07-19T00:25:14.887070+00:00

| Repository | Size | Commit | Files | Nodes | Edges | Time | Peak memory | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Django | large | 3d34265 | 2927 | 46437 | 82649 | 47.12s | 464.40 MiB | PASS |
| Flask | medium | 36e4a82 | 83 | 1704 | 2336 | 1.10s | 468.88 MiB | PASS |
| Requests | small-medium | f361ead | 37 | 845 | 1424 | 0.74s | 468.88 MiB | PASS |
| ItsDangerous | small | 672971d | 15 | 160 | 324 | 0.14s | 468.88 MiB | PASS |

## Django

- PASS — repository_size: Discovered 2927 Python files
- PASS — parse_success: Parse failure ratio 0.034%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- PASS — architectural_modules: All expected architectural modules were found
- PASS — architectural_symbols: All expected architectural symbols were found
- PASS — source_layout: Package names do not leak the source-directory prefix
- PASS — first_party_import_resolution: No obvious first-party imports remain unresolved
- PASS — adaptive_tours: Every developer role produced a source-backed route
- PASS — role_route_diversity: Role-specific tours do not all begin at the same module
- PASS — contribution_mission: Contribution mission has a source-backed target

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
- PASS — adaptive_tours: Every developer role produced a source-backed route
- PASS — role_route_diversity: Role-specific tours do not all begin at the same module
- PASS — contribution_mission: Contribution mission has a source-backed target

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
- PASS — first_party_import_resolution: No obvious first-party imports remain unresolved
- PASS — adaptive_tours: Every developer role produced a source-backed route
- PASS — role_route_diversity: Role-specific tours do not all begin at the same module
- PASS — contribution_mission: Contribution mission has a source-backed target

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
- PASS — adaptive_tours: Every developer role produced a source-backed route
- PASS — role_route_diversity: Role-specific tours do not all begin at the same module
- PASS — contribution_mission: Contribution mission has a source-backed target

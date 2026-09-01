# Analyzer external-repository benchmark

Generated: 2026-07-18T23:54:50.077224+00:00

| Repository | Size | Commit | Files | Nodes | Edges | Time | Peak memory | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Django | large | 3d34265 | 2927 | 46437 | 82649 | 43.66s | 0.00 MiB | FAIL |

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
- FAIL — first_party_import_resolution: 1 obvious first-party imports remain unresolved, e.g. ['from do_django_release import create_checksum_file, find_release_artifacts, parse_major_version']

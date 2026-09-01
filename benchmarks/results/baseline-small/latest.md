# Analyzer external-repository benchmark

Generated: 2026-07-18T23:55:32.377182+00:00

| Repository | Size | Commit | Files | Nodes | Edges | Time | Peak memory | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Flask | medium | 36e4a82 | 83 | 1704 | 2164 | 2.56s | 0.00 MiB | FAIL |
| Requests | small-medium | f361ead | 37 | 845 | 1261 | 1.27s | 0.00 MiB | FAIL |
| ItsDangerous | small | 672971d | 15 | 160 | 287 | 0.32s | 0.00 MiB | FAIL |

## Flask

- PASS — repository_size: Discovered 83 Python files
- PASS — parse_success: Parse failure ratio 0.000%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- FAIL — architectural_modules: Missing modules: flask, flask.app, flask.blueprints, flask.ctx
- FAIL — architectural_symbols: Missing symbols: flask.app.Flask, flask.blueprints.Blueprint, flask.ctx.AppContext
- FAIL — source_layout: Incorrect module prefixes found, e.g. ['src.flask', 'src.flask.__main__', 'src.flask.app', 'src.flask.blueprints', 'src.flask.cli']
- FAIL — first_party_import_resolution: 56 obvious first-party imports remain unresolved, e.g. ['flask', 'flask.views', 'from flask import Blueprint', 'from flask import Flask', 'from flask import Module']

## Requests

- PASS — repository_size: Discovered 37 Python files
- PASS — parse_success: Parse failure ratio 0.000%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- FAIL — architectural_modules: Missing modules: requests, requests.api, requests.models, requests.sessions
- FAIL — architectural_symbols: Missing symbols: requests.api.get, requests.models.Response, requests.sessions.Session
- FAIL — source_layout: Incorrect module prefixes found, e.g. ['src.requests', 'src.requests.__version__', 'src.requests._internal_utils', 'src.requests._types', 'src.requests.adapters']
- FAIL — first_party_import_resolution: 22 obvious first-party imports remain unresolved, e.g. ['from requests import compat', 'from requests import hooks', 'from requests._internal_utils import unicode_is_ascii', 'from requests.adapters import HTTPAdapter', 'from requests.auth import HTTPDigestAuth, _basic_auth_str']

## ItsDangerous

- PASS — repository_size: Discovered 15 Python files
- PASS — parse_success: Parse failure ratio 0.000%
- PASS — unique_node_ids: All node IDs are unique
- PASS — unique_edge_ids: All edge IDs are unique
- PASS — valid_edge_endpoints: Every edge endpoint resolves to a node
- PASS — valid_source_spans: Every source-backed node has a valid span
- PASS — evidence_invariants: Edge kinds use the expected evidence status
- FAIL — architectural_modules: Missing modules: itsdangerous, itsdangerous.serializer, itsdangerous.signer, itsdangerous.timed
- FAIL — architectural_symbols: Missing symbols: itsdangerous.serializer.Serializer, itsdangerous.signer.Signer, itsdangerous.timed.TimestampSigner
- FAIL — source_layout: Incorrect module prefixes found, e.g. ['src.itsdangerous', 'src.itsdangerous._json', 'src.itsdangerous.encoding', 'src.itsdangerous.exc', 'src.itsdangerous.serializer']
- FAIL — first_party_import_resolution: 23 obvious first-party imports remain unresolved, e.g. ['from itsdangerous.encoding import base64_decode', 'from itsdangerous.encoding import base64_encode', 'from itsdangerous.encoding import bytes_to_int', 'from itsdangerous.encoding import int_to_bytes', 'from itsdangerous.encoding import want_bytes']

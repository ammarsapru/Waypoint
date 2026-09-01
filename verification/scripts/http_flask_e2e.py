from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_REPOSITORY = "https://github.com/pallets/flask"


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    print(f"{method} {path} -> {response.status_code} ({duration_ms} ms)")
    response.raise_for_status()
    return response


def run(base_url: str, repository_url: str, repository_path: str | None) -> dict[str, Any]:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=300) as client:
        if repository_path:
            analysis_response = _request(
                client,
                "POST",
                "/api/v1/analysis",
                json={"repository_path": repository_path},
            )
        else:
            analysis_response = _request(
                client,
                "POST",
                "/api/v1/analysis/github",
                json={"repository_url": repository_url},
            )
        analysis = analysis_response.json()
        analysis_id = analysis["analysis_id"]
        assert analysis["stats"]["files_parsed"] >= 60, analysis["stats"]
        assert analysis["stats"]["parse_failures"] <= 3, analysis["stats"]
        source_paths = {
            node["span"]["path"]
            for node in analysis["nodes"]
            if node.get("span") and node["kind"] == "module"
        }
        expected = {"src/flask/__init__.py", "src/flask/app.py", "src/flask/cli.py"}
        assert expected.issubset(source_paths), sorted(expected - source_paths)

        summary = _request(
            client, "GET", f"/api/v1/analyses/{analysis_id}/summary"
        ).json()
        index = _request(
            client, "GET", f"/api/v1/analyses/{analysis_id}/index"
        ).json()
        source = _request(
            client,
            "GET",
            f"/api/v1/analyses/{analysis_id}/source",
            params={"path": "src/flask/app.py"},
        ).json()
        architecture = _request(
            client, "GET", f"/api/v1/analyses/{analysis_id}/architecture"
        ).json()
        assert summary["analysis_id"] == analysis_id
        assert index["status"] == "complete"
        assert index["chunks"] == index["vectors"]
        assert "class Flask" in source["content"]

        flask_class = next(
            node
            for node in analysis["nodes"]
            if node["kind"] == "class"
            and node["qualified_name"] == "flask.app.Flask"
        )
        usage = _request(
            client,
            "GET",
            f"/api/v1/analyses/{analysis_id}/nodes/{flask_class['id']}/usage",
        ).json()
        neighborhood = _request(
            client,
            "GET",
            f"/api/v1/analyses/{analysis_id}/nodes/{flask_class['id']}/neighborhood",
            params={"depth": 1},
        ).json()
        assert usage["symbol"]["qualified_name"] == "flask.app.Flask"
        assert neighborhood["center_node_id"] == flask_class["id"]

    return {
        "reference_repository": repository_url,
        "analysis_id": analysis_id,
        "stats": analysis["stats"],
        "summary": summary,
        "index": index,
        "architecture": {
            "import_cycle_count": architecture["import_cycle_count"],
            "hotspot_count": architecture["hotspot_count"],
        },
        "verified_files": sorted(expected),
        "verified_symbol": usage["symbol"],
        "http_e2e": "passed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic HTTP E2E checks against the Flask repository."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY)
    parser.add_argument(
        "--repository-path",
        help="Use an existing Flask checkout inside ONBOARD_ALLOWED_ROOT instead of cloning.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("verification/results/http-flask-e2e.json"),
    )
    args = parser.parse_args()
    result = run(args.base_url, args.repository_url, args.repository_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PASS: deterministic Flask HTTP E2E evidence written to {args.output}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from verification.scripts.core_coverage_report import build_report


def percentage(value: str | None) -> float:
    return round(float(value or 0) * 100, 1)


def render(xml_path: Path, manifest_path: Path = Path("verification/core-features.json")) -> str:
    root = ET.parse(xml_path).getroot()
    rows: list[tuple[str, float, float]] = []
    for item in root.findall(".//class"):
        rows.append((
            item.attrib.get("filename", "unknown"),
            percentage(item.attrib.get("line-rate")),
            percentage(item.attrib.get("branch-rate")),
        ))
    rows.sort(key=lambda row: (row[1], row[2], row[0]))
    lines = [
        "# Human-readable coverage report",
        "",
        "## Overall result",
        "",
        "| Metric | Result | Required gate | Status |",
        "|---|---:|---:|---|",
        f"| Line coverage | {percentage(root.attrib.get('line-rate'))}% | 70.0% combined branch-aware gate | {'PASS' if percentage(root.attrib.get('line-rate')) >= 70 else 'REVIEW'} |",
        f"| Branch coverage | {percentage(root.attrib.get('branch-rate'))}% | Included in combined coverage.py total | Measured |",
        "",
        "The enforced pytest value is coverage.py's combined statement/branch result. "
        "The XML exposes line and branch rates separately, which is why neither individual row is a replacement for the command's final total.",
        "",
        "## Files needing the most attention",
        "",
        "| File | Line coverage | Branch coverage |",
        "|---|---:|---:|",
    ]
    for filename, line_rate, branch_rate in rows[:20]:
        lines.append(f"| `{filename}` | {line_rate}% | {branch_rate}% |")
    lines += [
        "",
        "## How to inspect details",
        "",
        "Open `.waypoint-data/coverage-html/index.html` for annotated source lines. Red lines were missed; yellow lines were partially covered branches; green lines were executed.",
        "",
    ]
    core_report, core_passed = build_report(xml_path, manifest_path)
    core_section = core_report.replace(
        "# Core-feature coverage report",
        "## Rubric-required core-feature coverage",
        1,
    )
    lines += [
        "---",
        "",
        core_section,
        "",
        f"Unified coverage status: {'PASS' if core_passed else 'FAIL'}.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Cobertura coverage XML into a concise Markdown report.")
    parser.add_argument("--xml", type=Path, default=Path(".waypoint-data/coverage.xml"))
    parser.add_argument("--output", type=Path, default=Path("verification/results/coverage-report.md"))
    parser.add_argument("--manifest", type=Path, default=Path("verification/core-features.json"))
    args = parser.parse_args()
    report = render(args.xml, args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()

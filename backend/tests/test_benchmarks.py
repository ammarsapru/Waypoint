from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.run_retrieval import _deduplicated_paths, _score_case
from benchmarks.run_retrieval_v3 import run as run_v3
from verification.scripts.model_quality_benchmark import _score_answer
from backend.app.agent.retrieval import RepositoryRetrievalIndex


class RetrievalBenchmarkTests(unittest.TestCase):
    def test_v3_manifest_is_versioned_balanced_and_not_release_approved(self) -> None:
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads(
            (root / "benchmarks" / "retrieval_cases_v3.json").read_text(
                encoding="utf-8"
            )
        )
        cases = manifest["cases"]
        self.assertEqual(manifest["version"], "3.0")
        self.assertEqual(len(cases), 50)
        self.assertEqual(len({case["id"] for case in cases}), 50)
        self.assertEqual(
            {name: sum(case["repository"] == name for case in cases)
             for name in manifest["repositories"]},
            {"waypoint": 20, "flask": 15, "praw": 15},
        )
        self.assertEqual(
            {split: sum(case["split"] == split for case in cases)
             for split in ("development", "heldout")},
            {"development": 30, "heldout": 20},
        )
        for case in cases:
            self.assertTrue(case["question"].strip())
            self.assertTrue(case["required_path_groups"])
            self.assertTrue(all(group for group in case["required_path_groups"]))
            self.assertEqual(case["adjudication"]["status"], "pending_human")
            for path in [
                *(item for group in case["required_path_groups"] for item in group),
                *case.get("acceptable_paths", []),
                *case.get("forbidden_paths", []),
            ]:
                self.assertFalse(Path(path).is_absolute())

    def test_v3_runner_rejects_unadjudicated_labels_by_default(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with self.assertRaisesRegex(ValueError, "require human adjudication"):
            run_v3(
                root / "benchmarks" / "retrieval_cases_v3.json",
                {"waypoint": root, "flask": root, "praw": root},
                repeats=1,
                allow_unadjudicated=False,
            )

    def test_v3_runner_aggregates_repository_split_and_language_slices(self) -> None:
        manifest = {
            "version": "3-test",
            "repositories": ["waypoint"],
            "cases": [
                {
                    "id": f"case-{index}",
                    "repository": "waypoint",
                    "split": "development" if index < 30 else "heldout",
                    "language": "python",
                    "question": "question",
                    "required_path_groups": [["backend/app/main.py"]],
                    "acceptable_paths": [],
                    "forbidden_paths": [],
                    "adjudication": {"status": "human_approved"},
                }
                for index in range(50)
            ],
        }
        evaluation = {
            "index_ms": 1.0,
            "index_restore_ms": 0.1,
            "process_peak_memory_mib": 10.0,
            "mean_query_ms": 2.0,
            "query_p95_ms": 3.0,
            "results": [
                {
                    "id": f"case-{index}",
                    "hit_at_1": 1.0,
                    "hit_at_3": 1.0,
                    "hit_at_5": 1.0,
                    "hit_at_10": 1.0,
                    "precision_at_5": 1.0,
                    "precision_at_10": 1.0,
                    "path_recall_at_5": 1.0,
                    "path_recall_at_10": 1.0,
                    "required_group_recall_at_5": 1.0,
                    "required_group_recall_at_10": 1.0,
                    "average_precision_at_10": 1.0,
                    "ndcg_at_10": 1.0,
                    "first_relevant_rank": 1,
                    "strict_pass": True,
                    "forbidden_hits": [],
                }
                for index in range(50)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("benchmarks.run_retrieval_v3.evaluate", return_value=evaluation):
                result = run_v3(
                    path,
                    {"waypoint": Path(directory)},
                    repeats=1,
                    allow_unadjudicated=False,
                )
        self.assertTrue(result["release_gate_eligible"])
        self.assertEqual(result["overall"]["strict_quality_score"], 1.0)
        self.assertEqual(result["slices"]["split"]["heldout"]["cases"], 20)

    def test_query_terms_remove_question_noise_and_expand_morphology(self) -> None:
        raw, terms = RepositoryRetrievalIndex._query_terms(
            "How are conversations stored and restored through the API?"
        )
        self.assertIn("how", raw)
        self.assertNotIn("how", terms)
        self.assertNotIn("and", terms)
        self.assertIn("conversation", terms)
        self.assertIn("store", terms)
        self.assertIn("restore", terms)

    def test_v3_query_decomposition_and_intent_layers(self) -> None:
        clauses = RepositoryRetrievalIndex._query_clauses(
            "Trace validation through storage, and then expose it through the API"
        )
        self.assertGreaterEqual(len(clauses), 2)
        layers = RepositoryRetrievalIndex._intent_layers(
            {"validation", "storage", "api", "configuration", "test"}
        )
        self.assertTrue({"implementation", "interface", "tests"}.issubset(layers))
        self.assertEqual(
            RepositoryRetrievalIndex._source_layer("backend/app/api/routes.py"),
            "interface",
        )

    def test_strict_case_scores_required_groups_precision_and_forbidden_paths(self) -> None:
        case = {
            "required_path_groups": [["app.py", "src/app.py"], ["service.py"]],
            "acceptable_paths": ["models.py"],
            "forbidden_paths": ["generated/report.md"],
            "max_first_relevant_rank": 2,
            "min_required_group_recall_at_10": 1.0,
            "min_precision_at_5": 0.5,
        }

        score = _score_case(
            case, ["models.py", "src/app.py", "noise.py", "service.py"]
        )

        self.assertEqual(score["first_relevant_rank"], 1)
        self.assertEqual(score["required_group_recall_at_10"], 1.0)
        self.assertEqual(score["precision_at_5"], 0.75)
        self.assertTrue(score["strict_pass"])

        failed = _score_case(case, ["src/app.py", "generated/report.md", "service.py"])
        self.assertEqual(failed["forbidden_hits"], ["generated/report.md"])
        self.assertFalse(failed["strict_pass"])

    def test_file_ranking_deduplicates_multiple_chunks_from_one_file(self) -> None:
        paths = _deduplicated_paths([
            {"path": "app.py"},
            {"path": "app.py"},
            {"path": "service.py"},
        ])
        self.assertEqual(paths, ["app.py", "service.py"])

    def test_model_score_enforces_expected_citation_spans_and_output_limits(self) -> None:
        case = {
            "required_concepts": [["request"], ["dispatch"]],
            "required_evidence_groups": [["app.py"]],
            "acceptable_paths": ["app.py"],
            "expected_citation_groups": [[
                {"path": "app.py", "start_line": 90, "end_line": 130}
            ]],
            "min_concept_recall": 1.0,
            "min_evidence_group_recall": 1.0,
            "min_citation_precision": 1.0,
            "min_expected_citation_group_recall": 1.0,
            "output_constraints": {
                "min_answer_characters": 20,
                "max_answer_characters": 500,
                "min_citations": 1,
                "max_citations": 2,
                "max_citation_span_lines": 50,
                "max_output_tokens": 100,
            },
        }
        report = {
            "answer": "Request dispatch invokes the selected view and returns its response.",
            "citations": [{"span": {"path": "app.py", "start_line": 100, "end_line": 120}}],
            "output_tokens": 40,
            "validation_status": "passed",
        }
        self.assertTrue(_score_answer(case, report)["passed"])

        report["citations"][0]["span"] = {
            "path": "app.py", "start_line": 1, "end_line": 80
        }
        failed = _score_answer(case, report)
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["expected_citation_group_recall"], 0.0)
        self.assertFalse(failed["output_checks"]["citation_span_limit"])


if __name__ == "__main__":
    unittest.main()

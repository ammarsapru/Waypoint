from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.onboarding.models import (
    ChallengeAnswer,
    DeveloperRole,
    GroundedQuestion,
    StoredChallenge,
    TourRequest,
)
from backend.app.onboarding.service import OnboardingService, TourStateStore, tour_states


class OnboardingServiceTests(unittest.TestCase):
    def test_two_hour_onboarding_budget_is_supported(self) -> None:
        self.assertEqual(TourRequest(minutes=120).minutes, 120)
        with self.assertRaises(ValueError):
            TourRequest(minutes=121)

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        package = self.root / "app"
        tests = self.root / "tests"
        package.mkdir()
        tests.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "auth.py").write_text(
            "def verify_token(token: str) -> bool:\n"
            "    return bool(token)\n",
            encoding="utf-8",
        )
        (package / "routes.py").write_text(
            "from app.auth import verify_token\n\n"
            "def login(token: str) -> bool:\n"
            "    return verify_token(token)\n",
            encoding="utf-8",
        )
        (tests / "test_auth.py").write_text(
            "from app.auth import verify_token\n\n"
            "def test_verify():\n"
            "    assert verify_token('token')\n",
            encoding="utf-8",
        )
        self.report = RepositoryAnalyzer().analyze(self.root).model_copy(
            update={"analysis_id": "test-analysis"}
        )
        self.service = OnboardingService(self.report)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_role_specific_tours_are_grounded_and_different(self) -> None:
        security = self.service.plan_tour(
            TourRequest(role=DeveloperRole.SECURITY, goal="Review authentication")
        )
        qa = self.service.plan_tour(
            TourRequest(role=DeveloperRole.QA, goal="Understand test coverage")
        )
        self.assertTrue(security.steps)
        self.assertTrue(all(step.evidence for step in security.steps))
        self.assertNotEqual(
            [step.node_id for step in security.steps],
            [step.node_id for step in qa.steps],
        )

    def test_challenge_updates_mastery_score(self) -> None:
        plan = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        challenge = next(
            step.challenge for step in plan.steps if step.challenge is not None
        )
        correct_node = next(
            step.node_id
            for step in plan.steps
            if step.challenge and step.challenge.id == challenge.id
        )
        result = tour_states.answer(
            plan.id,
            ChallengeAnswer(
                challenge_id=challenge.id,
                selected_node_id=correct_node,
            ),
        )
        self.assertTrue(result.correct)
        self.assertEqual(result.points_earned, 5)
        self.assertEqual(result.points_possible, 5)
        self.assertEqual(result.correct_node_id, correct_node)
        self.assertIn(correct_node, result.mastered_node_ids)

    def test_incorrect_answer_scores_zero_but_still_reveals_correct_node(self) -> None:
        plan = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        challenge = next(
            step.challenge for step in plan.steps if step.challenge is not None
        )
        wrong_option = next(
            option.node_id
            for option in challenge.options
            if option.node_id != next(
                step.node_id
                for step in plan.steps
                if step.challenge and step.challenge.id == challenge.id
            )
        )
        result = tour_states.answer(
            plan.id,
            ChallengeAnswer(challenge_id=challenge.id, selected_node_id=wrong_option),
        )
        self.assertFalse(result.correct)
        self.assertEqual(result.points_earned, 0)
        self.assertIsNotNone(result.correct_node_id)
        self.assertNotEqual(result.correct_node_id, wrong_option)

    def test_re_answering_a_challenge_replaces_its_points_instead_of_adding(self) -> None:
        plan = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        challenge = next(
            step.challenge for step in plan.steps if step.challenge is not None
        )
        correct_node = next(
            step.node_id
            for step in plan.steps
            if step.challenge and step.challenge.id == challenge.id
        )
        wrong_option = next(
            option.node_id for option in challenge.options if option.node_id != correct_node
        )
        first = tour_states.answer(
            plan.id,
            ChallengeAnswer(challenge_id=challenge.id, selected_node_id=wrong_option),
        )
        second = tour_states.answer(
            plan.id,
            ChallengeAnswer(challenge_id=challenge.id, selected_node_id=correct_node),
        )
        self.assertEqual(first.tour_points_earned, 0)
        # Answering the same challenge again replaces its score - it must not sum to
        # 5 (first, wrong) + 5 (second, right) = 10 for a single challenge.
        self.assertEqual(second.tour_points_earned, 5)

    def test_mastery_is_reached_at_70_percent_of_every_challenges_points(self) -> None:
        plan = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        challenges = [step.challenge for step in plan.steps if step.challenge is not None]
        correct_nodes = {
            step.challenge.id: step.node_id
            for step in plan.steps
            if step.challenge is not None
        }
        total_possible = len(challenges) * 5
        result = None
        for challenge in challenges:
            result = tour_states.answer(
                plan.id,
                ChallengeAnswer(
                    challenge_id=challenge.id,
                    selected_node_id=correct_nodes[challenge.id],
                ),
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.tour_points_possible, total_possible)
        self.assertEqual(result.tour_points_earned, total_possible)
        self.assertTrue(result.tour_mastery)

    def test_free_text_evaluation_scales_model_score_to_points_out_of_five(self) -> None:
        plan = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        challenge_id = "free-text-challenge"
        stored = StoredChallenge(
            explanation="Tokens are verified in app/auth.py.",
            question_type="free_text",
            expected_concepts=["token validation", "auth boundary"],
            node_id=next(
                step.node_id for step in plan.steps if step.challenge is not None
            ),
        )
        tour_states._challenges[plan.id][challenge_id] = stored

        result = tour_states.record_evaluation(
            plan.id,
            challenge_id,
            stored,
            correct=False,
            explanation="Covers token validation but misses the auth boundary.",
            score=0.86,
            mastered_concepts=["token validation"],
            remediation="Explain how the auth boundary rejects unsigned tokens.",
        )
        self.assertEqual(result.points_earned, 4)  # round(0.86 * 5) == 4
        self.assertEqual(result.points_possible, 5)
        self.assertIsNone(result.correct_node_id)
        self.assertEqual(
            result.expected_concepts, ["auth boundary", "token validation"]
        )

    def test_tour_state_restores_from_sqlite(self) -> None:
        database = self.root / "tour-state.sqlite3"
        # Each store keeps a persistent SQLite connection open per thread (see
        # TourStateStore._connect), so both must be closed explicitly here -
        # tearDown deletes self.root right after this test returns, and unittest
        # runs addCleanup callbacks *after* tearDown, too late to release the file
        # handle before that Windows rmtree.
        first = TourStateStore(path=database)
        try:
            plan = self.service.plan_tour(
                TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
            )
            challenges = tour_states._challenges[plan.id]
            first.put(
                plan.id, challenges,
                analysis_id=plan.analysis_id, name="Restore test tour", plan=plan,
            )
        finally:
            first.close()

        challenge_id = next(iter(challenges))
        restored = TourStateStore(path=database)
        try:
            self.assertEqual(
                restored.challenge(plan.id, challenge_id), challenges[challenge_id]
            )
        finally:
            restored.close()

    def test_list_tours_is_scoped_to_its_analysis_and_omits_plan_content(self) -> None:
        first = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        second = self.service.plan_tour(
            TourRequest(role=DeveloperRole.QA, goal="Understand test coverage", name="My QA pass")
        )

        summaries = tour_states.list_tours("test-analysis")
        summary_ids = {summary.tour_id for summary in summaries}
        self.assertIn(first.id, summary_ids)
        self.assertIn(second.id, summary_ids)
        named = next(summary for summary in summaries if summary.tour_id == second.id)
        self.assertEqual(named.name, "My QA pass")
        self.assertEqual(named.role, DeveloperRole.QA)
        self.assertEqual(named.step_count, len(second.steps))

        self.assertEqual(tour_states.list_tours("a-different-analysis"), [])

    def test_get_plan_reconstructs_the_full_tour_for_reopening(self) -> None:
        plan = self.service.plan_tour(
            TourRequest(role=DeveloperRole.BACKEND, goal="Learn request routing")
        )
        reopened = tour_states.get_plan(plan.id)
        self.assertEqual(reopened, plan)
        self.assertIsNone(tour_states.get_plan("no-such-tour-id"))

    def test_architecture_mission_and_search_are_source_grounded(self) -> None:
        architecture = self.service.architecture_report()
        mission = self.service.contribution_mission(DeveloperRole.BACKEND)
        search = self.service.search("verify")
        self.assertEqual(architecture.analysis_id, "test-analysis")
        self.assertEqual(mission.risk, "low")
        self.assertTrue(mission.target_node.span)
        self.assertTrue(search.results)
        self.assertIn("verify", search.results[0].node.qualified_name)

    def test_grounded_answer_cites_source_and_refuses_unknowns(self) -> None:
        answer = self.service.answer(
            GroundedQuestion(question="Where is token verification handled?")
        )
        self.assertFalse(answer.refused)
        self.assertTrue(answer.citations)
        self.assertEqual(answer.citations[0].span.path, "app/auth.py")

        refusal = self.service.answer(
            GroundedQuestion(question="Where is quantum billing orchestration?")
        )
        self.assertTrue(refusal.refused)
        self.assertEqual(refusal.citations, [])

    def test_code_journey_is_bounded_and_source_grounded(self) -> None:
        start = self.service.search("login").results[0].node
        journey = self.service.journey(start.id, max_steps=5)
        self.assertEqual(journey.start_node_id, start.id)
        self.assertLessEqual(len(journey.steps), 5)
        self.assertEqual(journey.steps[0].node.id, start.id)
        self.assertTrue(all(step.evidence for step in journey.steps))

    def test_revision_comparison_detects_source_changes(self) -> None:
        previous = self.report.model_copy(update={"analysis_id": "previous"})
        (self.root / "app" / "auth.py").write_text(
            "def verify_token(token: str) -> bool:\n"
            "    return token.startswith('signed-')\n",
            encoding="utf-8",
        )
        current = RepositoryAnalyzer().analyze(self.root).model_copy(
            update={"analysis_id": "current"}
        )
        comparison = OnboardingService(current).compare(previous)
        self.assertEqual(
            [item.qualified_name for item in comparison.modified],
            ["app.auth"],
        )
        self.assertTrue(comparison.refresher)


if __name__ == "__main__":
    unittest.main()

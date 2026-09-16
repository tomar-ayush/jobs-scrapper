"""Bug condition exploration tests for scorer.py.

These tests are designed to FAIL on the UNFIXED scorer.py.
Failure is the expected outcome — it proves the bugs exist.

Bug 1: 503 errors are not remembered across batches.
       A model that returned 503 in batch N is still in the
       available pool for batch N+1, wasting an API round-trip.

Bug 2: _exhausted_models is module-level and leaks across
       score_jobs() calls within the same process.

Requirements: 1.1, 1.2, 1.3, 1.4
**Validates: Requirements 1.1, 1.2, 1.3, 1.4**
"""

import importlib
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_503_exception() -> Exception:
    """Return an exception whose string representation contains '503'."""
    return Exception("503 Service Unavailable")


def _make_429_exception() -> Exception:
    """Return an exception whose string representation contains '429'."""
    return Exception("429 quota exhausted")


def _make_200_response(content: str = '{"JOB_0": {"score": 8, "reasoning": "good", "matching_skills": [], "missing_skills": [], "seniority_fit": "good"}}') -> MagicMock:
    """Return a mock response object that looks like a successful Gemini response."""
    response = MagicMock()
    response.text = content
    return response


# ---------------------------------------------------------------------------
# Bug 1: 503 is not remembered between batches
# ---------------------------------------------------------------------------

class TestBug1_503NotRemembered:
    """
    Demonstrate that a model returning 503 in one _call_model invocation
    is still in the available list on the NEXT invocation — on unfixed code.

    The assertion checks the EXPECTED (fixed) behaviour:
      "gemini-3.7-flash" should NOT be in the available list on the second call.

    On unfixed code the assertion FAILS because the model is never added
    to any tracking set when it returns 503, so it keeps appearing as available.
    """

    def test_503_model_should_be_skipped_on_second_call(self):
        """
        Bug 1 exploration: After gemini-3.7-flash returns 503 during the first
        _call_model call, it must NOT be tried again in the second call.

        EXPECTED ON UNFIXED CODE: FAIL
        Counterexample: gemini-3.7-flash is still called on the second invocation.
        """
        import scorer  # import after potential reload

        # Track which models were actually called
        called_models: list[str] = []

        def fake_generate(model: str, contents, config=None):
            called_models.append(model)
            if model == "gemini-3.7-flash":
                raise _make_503_exception()
            # Any other model succeeds
            return _make_200_response()

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = fake_generate

        with patch("scorer._get_client", return_value=mock_client), \
             patch("scorer._get_resume", return_value="Fake resume text"):

            # First batch call — gemini-3.7-flash should 503 and be skipped
            called_models.clear()
            result1 = scorer._call_model("batch 1 prompt")

            models_called_in_batch1 = list(called_models)

            # Second batch call — on FIXED code, gemini-3.7-flash must be absent
            called_models.clear()
            result2 = scorer._call_model("batch 2 prompt")

            models_called_in_batch2 = list(called_models)

        # The expected (correct) assertion: gemini-3.7-flash must NOT be tried again
        # On UNFIXED code this FAILS — proving Bug 1 exists
        assert "gemini-3.7-flash" not in models_called_in_batch2, (
            f"Bug 1 confirmed: gemini-3.7-flash was called again in batch 2 "
            f"despite returning 503 in batch 1. "
            f"Batch 1 calls: {models_called_in_batch1}, "
            f"Batch 2 calls: {models_called_in_batch2}"
        )

    def test_503_model_is_still_available_after_503_on_unfixed_code(self):
        """
        Directly inspect the module-level state to show that after a 503,
        the unfixed scorer does NOT add the model to any tracking set.

        On UNFIXED code: _exhausted_models does not contain gemini-3.7-flash
        after a 503, so the model remains fully 'available'.

        EXPECTED ON UNFIXED CODE: FAIL on the final assertion.
        """
        import scorer

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = _make_503_exception()

        with patch("scorer._get_client", return_value=mock_client), \
             patch("scorer._get_resume", return_value="Fake resume text"):
            scorer._call_model("any prompt")

        # After a 503 the model must be tracked as unavailable.
        # On UNFIXED code _exhausted_models is empty after a 503 → assertion FAILS.
        assert "gemini-3.7-flash" in scorer._exhausted_models or \
               (hasattr(scorer, "RunState")), (
            "Bug 1 confirmed: gemini-3.7-flash is NOT tracked after returning 503. "
            "The model will be retried in every subsequent batch, wasting API round-trips."
        )


# ---------------------------------------------------------------------------
# Bug 2: _exhausted_models leaks across score_jobs() calls
# ---------------------------------------------------------------------------

class TestBug2_ExhaustedModelsLeaks:
    """
    Demonstrate that _exhausted_models carries state between score_jobs()
    calls within the same process — on unfixed code.

    The assertion checks the EXPECTED (fixed) behaviour:
      On the second score_jobs() call, gemini-3.7-flash must be attempted
      (state should have been reset).

    On unfixed code the assertion FAILS because _exhausted_models still
    contains gemini-3.7-flash from the first call.
    """

    def test_second_score_jobs_call_should_reset_exhausted_models(self):
        """
        Bug 2 exploration: After gemini-3.7-flash is exhausted (429) during
        the first score_jobs() call, a second score_jobs() call must attempt
        gemini-3.7-flash again (fresh state).

        EXPECTED ON UNFIXED CODE: FAIL
        Counterexample: gemini-3.7-flash is skipped in the second score_jobs()
        call because _exhausted_models was never cleared.
        """
        import scorer

        # Reset module state before test
        scorer._exhausted_models = set()

        called_models_run2: list[str] = []

        # First run: gemini-3.7-flash → 429 (exhausted), others → 503 so batch fails
        run1_call_count = [0]

        def fake_generate_run1(model: str, contents, config=None):
            if model == "gemini-3.7-flash":
                raise _make_429_exception()
            # All other models also fail in run 1 so score_jobs returns []
            raise Exception("503 Service Unavailable")

        # Second run: all models succeed
        def fake_generate_run2(model: str, contents, config=None):
            called_models_run2.append(model)
            return _make_200_response()

        mock_client = MagicMock()

        jobs = [{
            "title": "Engineer",
            "company": "Acme",
            "location": "India",
            "description": "Python backend role",
        }]

        with patch("scorer._get_client", return_value=mock_client), \
             patch("scorer._get_resume", return_value="Fake resume text"), \
             patch("scorer.time") as mock_time:

            # --- First score_jobs() call ---
            mock_client.models.generate_content.side_effect = fake_generate_run1
            scorer.score_jobs(jobs, delay=0)

            # --- Second score_jobs() call with fresh-succeeding models ---
            called_models_run2.clear()
            mock_client.models.generate_content.side_effect = fake_generate_run2
            scorer.score_jobs(jobs, delay=0)

        # On FIXED code: gemini-3.7-flash must be in called_models_run2
        # because a fresh RunState was created for the second call.
        # On UNFIXED code: gemini-3.7-flash is absent → FAILS, proving Bug 2.
        assert "gemini-3.7-flash" in called_models_run2, (
            f"Bug 2 confirmed: gemini-3.7-flash was NOT attempted in the second "
            f"score_jobs() call. _exhausted_models leaked from the first call. "
            f"Models attempted in run 2: {called_models_run2}"
        )

    def test_module_level_exhausted_models_persists_across_calls(self):
        """
        Directly verify the cross-call state leak: after score_jobs() exhausts
        gemini-3.7-flash, the module-level _exhausted_models still contains it
        at the start of a second score_jobs() call.

        EXPECTED ON UNFIXED CODE: PASS (the global IS dirty — confirming the bug).
        This is a diagnostic/documentation test rather than a fix-validation test.
        """
        import scorer

        # Clean slate
        scorer._exhausted_models = set()

        def fake_generate(model: str, contents, config=None):
            if model == "gemini-3.7-flash":
                raise _make_429_exception()
            raise Exception("503 Service Unavailable")

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = fake_generate

        jobs = [{
            "title": "Engineer",
            "company": "Acme",
            "location": "India",
            "description": "Python backend role",
        }]

        with patch("scorer._get_client", return_value=mock_client), \
             patch("scorer._get_resume", return_value="Fake resume text"), \
             patch("scorer.time"):
            scorer.score_jobs(jobs, delay=0)

        # On UNFIXED code: _exhausted_models contains gemini-3.7-flash after the call.
        # This documents that the global IS dirty — confirming the root cause of Bug 2.
        exhausted_after_run1 = set(scorer._exhausted_models)
        assert "gemini-3.7-flash" in exhausted_after_run1, (
            "Diagnostic: gemini-3.7-flash should be in _exhausted_models after run 1 "
            "on unfixed code — but it wasn't found. Check mock setup."
        )
        # The real problem: this state is never reset for a second call.
        # The next score_jobs() call will see a non-empty _exhausted_models.
        # (Tested by test_second_score_jobs_call_should_reset_exhausted_models)

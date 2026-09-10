"""Tests for the TOTAL per-turn mid-stream replay attempt cap (swp-ufba).

``streaming_retry()`` already caps each individual call at a handful of
attempts (the "streak"), resetting that streak to zero every time a fresh
call starts (initial run, each queued-steer follow-up, each hook-retry
follow-up). That per-call reset meant a turn that kept getting reinterrupted
mid-stream across many follow-up calls had no OVERALL ceiling — the reported
symptom was 48 replay attempts over ~2 hours with nothing terminating the
loop. These tests exercise the new ``_TurnReplayBudget`` / ``TurnReplayLimitExceeded``
machinery that tracks attempts across the whole turn, independent of any
single call's own streak counter.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from code_puppy.agents._runtime import (
    TurnReplayLimitExceeded,
    _TurnReplayBudget,
    _turn_replay_budget,
    streaming_retry,
)
from code_puppy.config import get_max_turn_replay_attempts


class TestGetMaxTurnReplayAttempts:
    def test_default_is_eight(self):
        with patch("code_puppy.config.get_value", return_value=None):
            assert get_max_turn_replay_attempts() == 8

    def test_respects_config_override(self):
        with patch("code_puppy.config.get_value", return_value="5"):
            assert get_max_turn_replay_attempts() == 5

    def test_floors_at_one_for_nonsensical_values(self):
        with patch("code_puppy.config.get_value", return_value="-3"):
            assert get_max_turn_replay_attempts() == 1

    def test_falls_back_to_default_on_garbage_value(self):
        with patch("code_puppy.config.get_value", return_value="not-a-number"):
            assert get_max_turn_replay_attempts() == 8


class TestTurnReplayBudget:
    def test_allows_exactly_cap_attempts(self):
        budget = _TurnReplayBudget(cap=3)
        # Attempts 1, 2, 3 must all succeed without raising.
        budget.bump_and_check()
        budget.bump_and_check()
        budget.bump_and_check()
        assert budget.used == 3

    def test_raises_on_the_attempt_past_cap(self):
        budget = _TurnReplayBudget(cap=3)
        budget.bump_and_check()
        budget.bump_and_check()
        budget.bump_and_check()
        with pytest.raises(TurnReplayLimitExceeded, match="max_turn_replay_attempts"):
            budget.bump_and_check()

    def test_cap_hit_logs_a_clear_error(self):
        budget = _TurnReplayBudget(cap=1)
        budget.bump_and_check()  # consumes the only allowed attempt
        with patch("code_puppy.agents._runtime.emit_error") as mock_emit_error:
            with pytest.raises(TurnReplayLimitExceeded):
                budget.bump_and_check()
            assert mock_emit_error.called
            logged_msg = mock_emit_error.call_args[0][0]
            assert "cap" in logged_msg.lower()
            assert "max_turn_replay_attempts" in logged_msg


class TestStreamingRetryHonoursTurnBudget:
    """The regression case: MANY separate streaming_retry-wrapped calls,
    each with its own short "streak" that resets to zero, must still be
    bounded by a single TOTAL cap shared across all of them via the
    ContextVar — exactly the follow-up-call pattern in ``_do_run``'s
    queued-steer / hook-retry loop.
    """

    @pytest.mark.asyncio
    async def test_many_reinterrupted_follow_up_calls_hit_the_total_cap(self):
        """Mirrors the real production shape: a flaky connection where EACH
        follow-up call (queued steer / hook retry) individually recovers
        after one retry — so no single call's streak ever exhausts and
        propagates an exception on its own — but the cumulative attempts
        across many such follow-ups is what must be bounded. This is exactly
        how 48 attempts over ~2h could accrue with per-call caps (3 streak,
        50 queued-steer follow-ups) that never individually trip: up to
        50 follow-ups x up to 3 attempts each = up to 150 total attempts
        with the old code, nothing stopping it sooner.
        """
        cap = 5
        token = _turn_replay_budget.set(_TurnReplayBudget(cap=cap))
        try:
            total_calls_made = 0
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(TurnReplayLimitExceeded):
                    # Simulate far more follow-up calls than the cap allows.
                    for _ in range(20):
                        attempt_in_this_call = 0

                        @streaming_retry(max_attempts=3, delays=(0, 0, 0))
                        async def _call() -> str:
                            nonlocal attempt_in_this_call, total_calls_made
                            total_calls_made += 1
                            attempt_in_this_call += 1
                            if attempt_in_this_call == 1:
                                # First attempt of every follow-up call is a
                                # transient blip that recovers on retry — the
                                # per-call streak resets to 0 next time.
                                raise httpx.ReadError(
                                    "connection dropped mid-stream"
                                )
                            return "ok"

                        await _call()
                # Must have stopped once the TOTAL crossed the cap, nowhere
                # near the 20-follow-up unbounded-looping alternative.
                assert total_calls_made <= cap + 1
        finally:
            _turn_replay_budget.reset(token)

    @pytest.mark.asyncio
    async def test_eventual_success_within_cap_still_works(self):
        """Sanity check: the budget must not block legitimate, bounded retries."""
        token = _turn_replay_budget.set(_TurnReplayBudget(cap=8))
        try:
            factory = AsyncMock(
                side_effect=[
                    httpx.ReadError("blip"),
                    "recovered",
                ]
            )

            @streaming_retry(max_attempts=3, delays=(0, 0, 0))
            async def _call() -> str:
                return await factory()

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await _call()
            assert result == "recovered"
        finally:
            _turn_replay_budget.reset(token)

    @pytest.mark.asyncio
    async def test_no_budget_in_context_is_uncapped_legacy_behavior(self):
        """Outside a turn (e.g. direct unit-test calls), no ContextVar budget
        is set — streaming_retry must behave exactly as it did before this
        change (bounded only by its own max_attempts, no total-cap raise).
        """
        assert _turn_replay_budget.get() is None
        factory = AsyncMock(side_effect=[httpx.ReadError("blip"), "recovered"])

        @streaming_retry(max_attempts=3, delays=(0, 0, 0))
        async def _call() -> str:
            return await factory()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _call()
        assert result == "recovered"

    @pytest.mark.asyncio
    async def test_cap_exceeded_error_is_never_classified_as_retryable(self):
        """TurnReplayLimitExceeded must be a terminal error, never mistaken
        for a transient connection blip by the classifier that drives both
        streaming_retry and cli_runner's friendly-vs-traceback rendering.
        """
        from code_puppy.agents.base_agent import should_retry_streaming_exception

        assert not should_retry_streaming_exception(
            TurnReplayLimitExceeded("cap exceeded")
        )

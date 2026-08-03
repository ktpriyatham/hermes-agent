"""Telegram connection-attempt governor — bot-flood-ban protection.

Regression test for the 2026-08-02 incident in which bot @neo_me_bot was
flood-banned by Telegram (every request to that bot id hung, regardless of
token secret; other bot ids answered instantly). Root cause: three nested
retry layers with no shared ceiling — the gateway reconnect loop drove the
adapter connect loop (8 attempts, 15s backoff cap) plus the polling
network-error loop, producing ~96 connection attempts/hour.

The governor is a process-wide rolling-window limiter that every connect
attempt must pass through, so no combination of retry loops can exceed the
hourly budget.
"""

import asyncio

import pytest

from plugins.platforms.telegram import adapter as tg


@pytest.fixture(autouse=True)
def _clean_governor_state(monkeypatch):
    """Each test gets a fresh attempt log."""
    tg._CONNECT_ATTEMPT_LOG.clear()
    yield
    tg._CONNECT_ATTEMPT_LOG.clear()


def test_attempts_within_budget_do_not_block(monkeypatch):
    """Up to the hourly budget, connect attempts pass through immediately."""
    monkeypatch.setenv("HERMES_TELEGRAM_MAX_CONNECTS_PER_HOUR", "5")

    async def run():
        for _ in range(5):
            await asyncio.wait_for(tg._govern_connect_attempt("bot"), timeout=1.0)

    asyncio.run(run())
    assert len(tg._CONNECT_ATTEMPT_LOG["bot"]) == 5


def test_attempt_over_budget_blocks(monkeypatch):
    """The attempt past the budget waits instead of hitting Telegram.

    This is the property that prevents the ban: a retry storm becomes a
    trickle rather than ~96 attempts/hour.
    """
    monkeypatch.setenv("HERMES_TELEGRAM_MAX_CONNECTS_PER_HOUR", "3")

    async def run():
        for _ in range(3):
            await asyncio.wait_for(tg._govern_connect_attempt("bot"), timeout=1.0)
        # 4th must block (it waits for the window to free up)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(tg._govern_connect_attempt("bot"), timeout=0.5)

    asyncio.run(run())
    # the blocked attempt must NOT be recorded as consumed
    assert len(tg._CONNECT_ATTEMPT_LOG["bot"]) == 3


def test_zero_budget_disables_governor(monkeypatch):
    """0 is an escape hatch — no limiting at all."""
    monkeypatch.setenv("HERMES_TELEGRAM_MAX_CONNECTS_PER_HOUR", "0")

    async def run():
        for _ in range(25):
            await asyncio.wait_for(tg._govern_connect_attempt("bot"), timeout=1.0)

    asyncio.run(run())


def test_budget_is_per_adapter(monkeypatch):
    """Two bots don't consume each other's budget."""
    monkeypatch.setenv("HERMES_TELEGRAM_MAX_CONNECTS_PER_HOUR", "2")

    async def run():
        await asyncio.wait_for(tg._govern_connect_attempt("bot-a"), timeout=1.0)
        await asyncio.wait_for(tg._govern_connect_attempt("bot-a"), timeout=1.0)
        # bot-b still has its own full budget
        await asyncio.wait_for(tg._govern_connect_attempt("bot-b"), timeout=1.0)

    asyncio.run(run())
    assert len(tg._CONNECT_ATTEMPT_LOG["bot-a"]) == 2
    assert len(tg._CONNECT_ATTEMPT_LOG["bot-b"]) == 1


def test_stale_attempts_age_out(monkeypatch):
    """Attempts older than the window stop counting against the budget."""
    monkeypatch.setenv("HERMES_TELEGRAM_MAX_CONNECTS_PER_HOUR", "2")
    import time as _time

    # two attempts recorded well over an hour ago
    old = _time.monotonic() - (tg._CONNECT_WINDOW_SECONDS + 60)
    tg._CONNECT_ATTEMPT_LOG["bot"] = [old, old]

    async def run():
        # budget looks spent, but both entries are stale → must pass instantly
        await asyncio.wait_for(tg._govern_connect_attempt("bot"), timeout=1.0)

    asyncio.run(run())
    assert len(tg._CONNECT_ATTEMPT_LOG["bot"]) == 1


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_MAX_CONNECTS_PER_HOUR", "not-a-number")
    assert tg._max_connects_per_hour() == 12

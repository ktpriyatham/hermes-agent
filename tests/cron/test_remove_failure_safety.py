"""Removal must not commit a deletion before a failed cancellation lookup."""

import sqlite3

import pytest

from cron import executions, jobs
from hermes_constants import get_hermes_home


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = get_hermes_home()
    ledger = home / "cron" / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", ledger)
    with jobs.use_cron_store(home):
        yield home


def test_corrupt_ledger_removal_preserves_schedule_and_output(store):
    job = jobs.create_job(prompt="keep until removal succeeds", schedule="every 1h")
    other = jobs.create_job(prompt="unrelated schedule", schedule="every 2h")
    output = jobs.save_job_output(job["id"], "preserved output")
    executions.create_execution(job["id"], source="test")
    ledger = store / "cron" / "executions.db"
    with sqlite3.connect(ledger) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    # Invalid schema B-tree page type, while retaining the SQLite file header.
    with ledger.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00")
    before = (store / "cron" / "jobs.json").read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        jobs.remove_job(job["id"])

    assert (store / "cron" / "jobs.json").read_bytes() == before
    assert jobs.get_job(job["id"]) is not None
    assert jobs.get_job(other["id"]) is not None
    assert output.read_text() == "preserved output"
    assert not list((store / "cron" / "cancels").glob("*.cancel"))


def test_output_cleanup_failure_does_not_report_removal_failure(store, monkeypatch, caplog):
    job = jobs.create_job(prompt="remove", schedule="every 1h")
    output = jobs.save_job_output(job["id"], "leftover output")
    fire = executions.create_execution(job["id"], source="test")["id"]

    def denied(path):
        raise PermissionError("output cleanup denied")

    monkeypatch.setattr(jobs.shutil, "rmtree", denied)
    assert jobs.remove_job(job["id"]) is True
    assert jobs.get_job(job["id"]) is None
    assert output.exists()
    assert (store / "cron" / "cancels" / f"{fire}.cancel").exists()
    assert "removed" in caplog.text.lower()
    assert "output cleanup" in caplog.text.lower()


def test_marker_write_failure_preserves_schedule(store):
    job = jobs.create_job(prompt="keep", schedule="every 1h")
    executions.create_execution(job["id"], source="test")
    # A real filesystem failure at request_cancel, not a mocked cancellation result.
    (store / "cron" / "cancels").write_text("not a directory")
    before = (store / "cron" / "jobs.json").read_bytes()

    with pytest.raises(OSError):
        jobs.remove_job(job["id"])

    assert (store / "cron" / "jobs.json").read_bytes() == before


def test_successful_removal_by_name_requests_cancel_before_deletion(store, monkeypatch):
    from cron.cancellation import cancel_requested

    job = jobs.create_job(name="removal target", prompt="remove", schedule="every 1h")
    other = jobs.create_job(prompt="keep", schedule="every 2h")
    fire = executions.create_execution(job["id"], source="test")["id"]
    real_save = jobs.save_jobs

    def save(records, **kwargs):
        assert cancel_requested(fire)
        assert jobs.get_job(job["id"]) is not None
        return real_save(records, **kwargs)

    monkeypatch.setattr(jobs, "save_jobs", save)
    assert jobs.remove_job("removal target") is True
    assert jobs.get_job(job["id"]) is None
    assert jobs.get_job(other["id"]) is not None
    # Request publication is NOT an acknowledgement that a worker has stopped.
    row = executions.get_execution(fire)
    assert row is not None
    assert row["status"] == "claimed"
    assert jobs.remove_job(job["id"]) is False


def test_self_removal_still_allows_delivery_without_cancelling_itself(store):
    from cron.cancellation import cancel_requested

    job = jobs.create_job(prompt="self remove", schedule="every 1h")
    fire = executions.create_execution(job["id"], source="test")["id"]
    with jobs.self_removal_delivery_scope(job["id"]):
        assert jobs.remove_job(job["id"]) is True
        assert jobs.self_removal_delivery_allowed(job["id"])
    assert not cancel_requested(fire)
    assert jobs.get_job(job["id"]) is None


def test_failed_save_does_not_authorize_self_removal_delivery(store, monkeypatch):
    job = jobs.create_job(prompt="self remove", schedule="every 1h")

    def denied(*args, **kwargs):
        raise PermissionError("save denied")

    monkeypatch.setattr(jobs, "save_jobs", denied)
    with jobs.self_removal_delivery_scope(job["id"]):
        with pytest.raises(PermissionError, match="save denied"):
            jobs.remove_job(job["id"])
        assert not jobs.self_removal_delivery_allowed(job["id"])
    assert jobs.get_job(job["id"]) is not None


def test_contending_fire_cannot_claim_between_cancel_and_removal(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    job = jobs.create_job(prompt="remove", schedule="every 1h")
    executions.create_execution(job["id"], source="test")
    attempting = Event()
    real_cancel = jobs.cancel_job_run
    contenders = []

    def claim():
        with jobs.use_cron_store(store):
            attempting.set()
            return jobs.claim_job_for_fire(job["id"], force=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        def cancel(*args, **kwargs):
            result = real_cancel(*args, **kwargs)
            contenders.append(pool.submit(claim))
            assert attempting.wait(5)
            return result

        monkeypatch.setattr(jobs, "cancel_job_run", cancel)
        assert jobs.remove_job(job["id"]) is True
        assert contenders[0].result(timeout=5) is False
    assert jobs.get_job(job["id"]) is None

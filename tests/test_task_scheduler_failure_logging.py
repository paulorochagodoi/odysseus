"""A failed scheduled run must be logged as a failure, not as "completed".

Legacy built-ins without a sealed request authority failed every run with
"Scheduled action has no matching server request authority", yet the
scheduler logged each one as "completed", hiding the breakage.
"""
import logging
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def scheduler_db(monkeypatch, tmp_path):
    import core.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.task_scheduler import TaskScheduler

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    with sessions() as db:
        db.add(database.ScheduledTask(id="t1", owner="alice", name="Tidy sessions",
                                     task_type="action", action="tidy_sessions",
                                     trigger_type="manual"))
        db.add(database.TaskRun(id="r1", task_id="t1", status="queued"))
        db.commit()
    scheduler = TaskScheduler(session_manager=None)
    monkeypatch.setattr(scheduler, "add_notification", lambda *a, **k: None)
    yield scheduler, sessions, database
    engine.dispose()


@pytest.mark.asyncio
async def test_failed_action_run_is_logged_as_failure(scheduler_db, monkeypatch, caplog):
    scheduler, sessions, database = scheduler_db
    msg = "Scheduled action has no matching server request authority."
    monkeypatch.setattr(scheduler, "_execute_action", AsyncMock(return_value=(msg, False)))

    with caplog.at_level(logging.INFO, logger="src.task_scheduler"):
        await scheduler._execute_task_locked("t1", "r1", gate_foreground=False)

    with sessions() as db:
        assert db.get(database.TaskRun, "r1").status == "error"
    lines = [r for r in caplog.records if "Tidy sessions" in r.getMessage()]
    assert any(r.levelno == logging.WARNING and "failed" in r.getMessage()
               and msg in r.getMessage() for r in lines)
    assert not any("completed" in r.getMessage() for r in lines)


@pytest.mark.asyncio
async def test_successful_action_run_is_logged_as_completed(scheduler_db, monkeypatch, caplog):
    scheduler, sessions, database = scheduler_db
    monkeypatch.setattr(scheduler, "_execute_action", AsyncMock(return_value=("ok", True)))
    monkeypatch.setattr(scheduler, "_deliver_task_result", AsyncMock())

    with caplog.at_level(logging.INFO, logger="src.task_scheduler"):
        await scheduler._execute_task_locked("t1", "r1", gate_foreground=False)

    with sessions() as db:
        assert db.get(database.TaskRun, "r1").status == "success"
    assert any("completed (run r1)" in r.getMessage() for r in caplog.records)
    assert not any("failed (run r1)" in r.getMessage() for r in caplog.records)

"""@spec BR-009 — retention prune runs isolated from the live sweep.

Retention prune wiring + isolation tests.

The prune SQL itself is asserted in test_store.py (offline) and exercised live
against Postgres. Here we cover the scheduler glue: ``build_scheduler``
registers the ``retention_prune`` job only when enabled and both stores are
available, and ``run_retention_prune`` isolates one store failing from the
other so housekeeping never crashes the monitor.
"""

from __future__ import annotations

from typing import Any

from monitoring_agent.scheduler import MetaDeps, build_scheduler, run_retention_prune
from monitoring_agent.settings import Settings


class _FakeStore:
    def __init__(self, *, deleted: int = 0, boom: bool = False) -> None:
        self._deleted = deleted
        self._boom = boom
        self.called_with: int | None = None

    async def prune(self, *, retention_days: int) -> int:
        self.called_with = retention_days
        if self._boom:
            raise RuntimeError("db gone")
        return self._deleted


def _settings(**kw: Any) -> Settings:
    return Settings(targets=[], agent_endpoints={}, **kw)


def _meta_deps(probe_state: Any) -> MetaDeps:
    return MetaDeps(probe_state=probe_state)


async def test_run_retention_prune_returns_per_table_counts() -> None:
    store = _FakeStore(deleted=4)
    probe_state = _FakeStore(deleted=2)
    out = await run_retention_prune(
        _settings(incident_retention_days=90, probe_state_retention_days=30),
        store,  # type: ignore[arg-type]
        probe_state,  # type: ignore[arg-type]
    )
    assert out == {"incidents_pruned": 4, "probe_state_pruned": 2}
    assert store.called_with == 90
    assert probe_state.called_with == 30


async def test_run_retention_prune_isolates_one_store_failure() -> None:
    store = _FakeStore(boom=True)  # incidents prune blows up
    probe_state = _FakeStore(deleted=5)
    out = await run_retention_prune(_settings(), store, probe_state)  # type: ignore[arg-type]
    # The failing prune is swallowed; the other still runs.
    assert out == {"incidents_pruned": 0, "probe_state_pruned": 5}


def test_build_scheduler_registers_prune_job_when_enabled() -> None:
    sched = build_scheduler(
        _settings(meta_enabled=True),
        graph=None,  # type: ignore[arg-type]  # add_job only stores the ref
        meta_deps=_meta_deps(_FakeStore()),
        store=_FakeStore(),  # type: ignore[arg-type]
    )
    assert sched.get_job("retention_prune") is not None


def test_build_scheduler_skips_prune_when_no_store() -> None:
    sched = build_scheduler(
        _settings(meta_enabled=True),
        graph=None,  # type: ignore[arg-type]
        meta_deps=_meta_deps(_FakeStore()),
        store=None,
    )
    assert sched.get_job("retention_prune") is None


def test_build_scheduler_skips_prune_when_interval_disabled() -> None:
    sched = build_scheduler(
        _settings(meta_enabled=True, retention_prune_interval_seconds=0),
        graph=None,  # type: ignore[arg-type]
        meta_deps=_meta_deps(_FakeStore()),
        store=_FakeStore(),  # type: ignore[arg-type]
    )
    assert sched.get_job("retention_prune") is None

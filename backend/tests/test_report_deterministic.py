import json
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask

from app.api import report as report_api
from app.models.project import ProjectStatus
from app.models.task import TaskManager
from app.services.report_agent import ReportManager
from app.services.simulation_runner import RunnerStatus

SIM = "sim-1"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(ReportManager, "_now", staticmethod(lambda: NOW))


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(tmp_path))
    return tmp_path


def _write(reports_dir, rid, status, created, sim=SIM, hb=None, created_raw=None):
    """hb = minutes since last disk update (default: same as created age)."""
    folder = reports_dir / rid
    folder.mkdir()
    meta = {
        "report_id": rid, "simulation_id": sim, "graph_id": "g",
        "simulation_requirement": "r", "status": status,
    }
    if created_raw is not None:
        if created_raw != "MISSING":
            meta["created_at"] = created_raw
    else:
        meta["created_at"] = created.isoformat()
    (folder / "meta.json").write_text(json.dumps(meta))
    if hb is None:
        hb = 1 if created is None else (NOW - created).total_seconds() / 60
    ts = NOW.timestamp() - hb * 60
    os.utime(folder / "meta.json", (ts, ts))


def _ago(minutes):
    return NOW - timedelta(minutes=minutes)


def _order(monkeypatch, order):
    real = os.listdir
    monkeypatch.setattr(
        os, "listdir", lambda p: order(real(p)) if str(p) == ReportManager.REPORTS_DIR else real(p)
    )


@pytest.mark.parametrize("order", [sorted, lambda x: sorted(x, reverse=True)])
def test_latest_completed_wins_regardless_of_listdir_order(reports_dir, monkeypatch, order):
    _write(reports_dir, "report_a_old", "completed", _ago(100))
    _write(reports_dir, "report_z_new", "completed", _ago(10))
    _write(reports_dir, "report_m_mid", "completed", _ago(50))
    _order(monkeypatch, order)
    assert ReportManager.get_report_by_simulation(SIM).report_id == "report_z_new"


@pytest.mark.parametrize("order", [sorted, lambda x: sorted(x, reverse=True)])
def test_completed_beats_newer_pending(reports_dir, monkeypatch, order):
    _write(reports_dir, "report_c", "completed", _ago(100))
    _write(reports_dir, "report_p", "pending", _ago(1))
    _order(monkeypatch, order)
    assert ReportManager.get_report_by_simulation(SIM).report_id == "report_c"


@pytest.mark.parametrize("order", [sorted, lambda x: sorted(x, reverse=True)])
def test_only_unfinished_returns_newest(reports_dir, monkeypatch, order):
    _write(reports_dir, "report_p1", "pending", _ago(30))
    _write(reports_dir, "report_p2", "generating", _ago(5))
    _order(monkeypatch, order)
    assert ReportManager.get_report_by_simulation(SIM).report_id == "report_p2"


def test_corrupt_report_is_skipped(reports_dir):
    _write(reports_dir, "report_ok", "completed", _ago(10))
    bad = reports_dir / "report_bad"
    bad.mkdir()
    (bad / "meta.json").write_text("{not json")
    (reports_dir / "report_empty").mkdir()
    _write(reports_dir, "report_other", "completed", _ago(1), sim="other")
    assert ReportManager.get_report_by_simulation(SIM).report_id == "report_ok"


def test_old_reports_stay_listed(reports_dir):
    _write(reports_dir, "report_old", "completed", _ago(100))
    _write(reports_dir, "report_new", "completed", _ago(10))
    ids = {r.report_id for r in ReportManager.list_reports(simulation_id=SIM)}
    assert ids == {"report_old", "report_new"}


def test_active_report_detection_uses_heartbeat_not_created_at(reports_dir):
    # created long ago but heartbeat fresh -> alive; created recently but silent -> zombie
    _write(reports_dir, "report_failed", "failed", _ago(1))
    _write(reports_dir, "report_silent", "generating", _ago(20), hb=15)
    assert ReportManager.get_active_report_by_simulation(SIM) is None
    _write(reports_dir, "report_beating", "generating", _ago(300), hb=2)
    assert ReportManager.get_active_report_by_simulation(SIM).report_id == "report_beating"


@pytest.mark.parametrize("raw", ["MISSING", "", "garbage",
                                 "2026-09-20T11:00:00Z", "2026-09-20T11:00:00+00:00",
                                 "2026-09-20T11:00:00"])
def test_created_at_mixed_or_bad_does_not_crash(reports_dir, raw):
    _write(reports_dir, "report_x", "generating", None, hb=1, created_raw=raw)
    assert ReportManager.get_active_report_by_simulation(SIM).report_id == "report_x"
    assert ReportManager.get_report_by_simulation(SIM).report_id == "report_x"


def test_negative_age_is_not_active(reports_dir):
    _write(reports_dir, "report_future", "generating", _ago(1), hb=-30)
    assert ReportManager.get_active_report_by_simulation(SIM) is None


def test_mixed_tz_sort_is_correct(reports_dir):
    _write(reports_dir, "report_a", "completed", None, created_raw="2026-09-20T11:00:00Z")
    _write(reports_dir, "report_b", "completed", None, created_raw="2026-09-20T11:30:00+00:00")
    _write(reports_dir, "report_c", "completed", None, created_raw="2026-09-20T10:59:00")
    assert ReportManager.get_report_by_simulation(SIM).report_id == "report_b"


# ---------- generate endpoint ----------

@pytest.fixture
def api(monkeypatch, reports_dir):
    tm = TaskManager()
    monkeypatch.setattr(tm, "_tasks", {})
    sim = SimpleNamespace(project_id="p", graph_id="g")
    project = SimpleNamespace(
        project_id="p", graph_id="g", status=ProjectStatus.GRAPH_COMPLETED,
        simulation_requirement="req",
    )
    monkeypatch.setattr(report_api, "SimulationManager",
                        lambda: SimpleNamespace(get_simulation=lambda _i: sim))
    monkeypatch.setattr(report_api.ProjectManager, "get_project",
                        classmethod(lambda _c, _i: project))
    monkeypatch.setattr(report_api.SimulationRunner, "get_run_state",
                        classmethod(lambda _c, _i: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)))
    monkeypatch.setattr(report_api.ZepGraphMemoryManager, "get_updater",
                        classmethod(lambda _c, _i: None))
    started = []

    class ParkedThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            started.append(self.target)  # worker never runs -> stays "running"

    monkeypatch.setattr(report_api, "threading", SimpleNamespace(Thread=ParkedThread))
    monkeypatch.setattr(report_api, "register_graph_reader", lambda *a: None)
    monkeypatch.setattr(report_api, "unregister_graph_reader", lambda *a: None)
    return started


def _post(force=False):
    app = Flask(__name__)
    with app.test_request_context("/api/report/generate", method="POST",
                                  json={"simulation_id": SIM, "force_regenerate": force}):
        res = report_api.generate_report()
        resp, status = res if isinstance(res, tuple) else (res, res.status_code)
        return resp.get_json(), status


@pytest.mark.parametrize("force", [False, True])
def test_second_generate_returns_running_task(api, force):
    first, s1 = _post()
    second, s2 = _post(force)
    assert s1 == s2 == 200
    assert first["data"]["already_generated"] is False
    assert second["data"]["already_running"] is True
    assert second["data"]["report_id"] == first["data"]["report_id"]
    assert second["data"]["task_id"] == first["data"]["task_id"]
    assert len(api) == 1
    assert len(TaskManager().list_tasks("report_generate")) == 1


def test_concurrent_generate_creates_single_task(api):
    results = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(_post(force=True))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(api) == 1
    assert sum(1 for b, _ in results if not b["data"].get("already_running")) == 1


def test_fresh_heartbeat_blocks_even_with_force(api, reports_dir):
    _write(reports_dir, "report_disk", "generating", _ago(5), hb=2)
    body, status = _post(force=True)
    assert body["data"]["already_running"] is True
    assert body["data"]["report_id"] == "report_disk"
    assert body["data"]["task_id"] is None
    assert api == []


@pytest.mark.parametrize("force", [False, True])
def test_zombie_disk_does_not_block_and_is_marked_failed(api, reports_dir, force):
    _write(reports_dir, "report_zombie", "generating", _ago(30), hb=30)
    body, _ = _post(force)
    assert "already_running" not in body["data"]
    assert body["data"]["report_id"] != "report_zombie"
    assert len(api) == 1
    meta = json.loads((reports_dir / "report_zombie" / "meta.json").read_text())
    assert meta["status"] == "failed"
    assert meta["error"] == "orphaned: no live worker"


def test_failed_task_with_generating_meta_allows_retry(api, reports_dir):
    first, _ = _post()
    rid, tid = first["data"]["report_id"], first["data"]["task_id"]
    meta = json.loads((reports_dir / rid / "meta.json").read_text())
    assert meta["status"] == "pending"  # written synchronously in the request
    (reports_dir / rid / "meta.json").write_text(json.dumps({**meta, "status": "generating"}))
    TaskManager().fail_task(tid, "boom")
    second, _ = _post(force=True)
    assert "already_running" not in second["data"]
    assert second["data"]["report_id"] != rid
    assert len(api) == 2


def test_pending_meta_written_before_thread_start(api, reports_dir, monkeypatch):
    seen = {}
    orig = report_api.threading.Thread

    class Spy(orig):
        def start(self):
            seen["metas"] = [p.name for p in reports_dir.iterdir()]
            super().start()

    monkeypatch.setattr(report_api, "threading", SimpleNamespace(Thread=Spy))
    body, _ = _post()
    assert body["data"]["report_id"] in seen["metas"]


def test_run_generate_exception_marks_meta_failed(api, reports_dir, monkeypatch):
    class Boom:
        def __init__(self, **kw):
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(report_api, "ReportAgent", Boom)
    body, _ = _post()
    api[0]()  # run the worker inline
    rid = body["data"]["report_id"]
    meta = json.loads((reports_dir / rid / "meta.json").read_text())
    assert meta["status"] == "failed"
    assert "agent exploded" in meta["error"]


def test_old_failed_report_does_not_block(api, reports_dir):
    _write(reports_dir, "report_failed", "failed", _ago(3))
    _write(reports_dir, "report_stuck", "generating", _ago(300), hb=300)
    body, _ = _post()
    assert "already_running" not in body["data"]
    assert body["data"]["already_generated"] is False
    assert len(api) == 1


def test_force_creates_new_version_and_old_stays(api, reports_dir):
    _write(reports_dir, "report_old", "completed", _ago(100))
    body, _ = _post(force=True)
    assert body["data"]["already_generated"] is False
    assert "already_running" not in body["data"]
    assert body["data"]["report_id"] != "report_old"
    assert (reports_dir / "report_old" / "meta.json").exists()


def test_completed_without_force_still_already_generated(api, reports_dir):
    _write(reports_dir, "report_done", "completed", _ago(100))
    body, _ = _post()
    assert body["data"]["already_generated"] is True
    assert body["data"]["report_id"] == "report_done"


def test_409_gate_still_applies(api, monkeypatch):
    monkeypatch.setattr(report_api.SimulationRunner, "get_run_state",
                        classmethod(lambda _c, _i: SimpleNamespace(runner_status=RunnerStatus.RUNNING)))
    _, status = _post()
    assert status == 409

from datetime import datetime
import json
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

from flask import Flask
from types import SimpleNamespace
from werkzeug.serving import make_server

from app.api import graph as graph_api
from app.api import simulation as simulation_api
from app.models.project import Project, ProjectStatus
from app.services.report_agent import Report, ReportStatus
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import RunnerStatus
from app.models.task import TaskStatus


@contextmanager
def _real_server(blueprint, url_prefix):
    """Spin up a REAL Flask app on a REAL socket (not test_client()), so the
    CRITICAL-fix test below exercises the full WSGI/HTTP stack instead of
    calling the view function directly."""
    app = Flask(__name__)
    app.register_blueprint(blueprint, url_prefix=url_prefix)
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _http_post(url):
    req = urllib.request.Request(
        url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _project(status, graph_id="graph-1"):
    now = datetime.now().isoformat()
    return Project(
        project_id="proj-1",
        name="Project",
        status=status,
        created_at=now,
        updated_at=now,
        ontology={"entity_types": [], "edge_types": []},
        graph_id=graph_id,
        graph_build_task_id="task-1",
        zep_batch_id="batch-1",
        zep_batch_operation_id="operation-1",
    )


def _json_result(result):
    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, result.status_code
    return response.get_json(), status


def test_project_reset_deletes_the_cloud_graph_before_clearing_reference(monkeypatch):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    events = []

    class Builder:
        def __init__(self, **_kwargs):
            pass

        def delete_graph(self, graph_id):
            events.append(("cloud-delete", graph_id))

    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, saved: events.append(("save", saved.graph_id))),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 200
    assert body["success"] is True
    assert events == [("cloud-delete", "graph-1"), ("save", None)]
    assert project.zep_batch_id is None
    assert project.status == ProjectStatus.ONTOLOGY_GENERATED


def test_project_reset_refuses_a_graph_with_an_active_simulation(monkeypatch):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "get_simulation_ids_for_graph",
        classmethod(lambda _cls, _graph_id: ["sim-active"]),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 409
    assert "sim-active" in body["error"]


def test_project_reset_refuses_a_completed_simulation_with_a_report(monkeypatch):
    """T3B2: a COMPLETED simulation whose report still exists genuinely
    depends on the graph (chat/tools query it) — reset must be blocked."""
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    simulation = SimpleNamespace(simulation_id="sim-completed", graph_id="graph-1")
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: [simulation]),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
        ),
    )
    monkeypatch.setattr(
        graph_api.os, "listdir", lambda _path: ["report-1"]
    )
    monkeypatch.setattr(
        graph_api.os.path, "isdir", lambda _path: True
    )
    monkeypatch.setattr(
        graph_api.ReportManager,
        "get_report",
        classmethod(
            lambda _cls, _report_id: SimpleNamespace(
                report_id="report-1",
                simulation_id="sim-completed",
                status=graph_api.ReportStatus.COMPLETED,
                created_at="2026-01-01T00:00:00",
            )
        ),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 409
    assert "sim-completed" in body["error"]
    assert project.graph_id == "graph-1"


def test_project_reset_allows_a_completed_simulation_without_a_report(monkeypatch):
    """No regression: COMPLETED with no report on disk must not block reset."""
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    simulation = SimpleNamespace(simulation_id="sim-no-report", graph_id="graph-1")
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(graph_api, "GraphBuilderService", lambda **_kwargs: SimpleNamespace(
        delete_graph=lambda _graph_id: None
    ))
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: [simulation]),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
        ),
    )
    monkeypatch.setattr(graph_api.os, "listdir", lambda _path: [])

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 200
    assert project.graph_id is None


def test_project_reset_ignores_a_crashed_simulation(monkeypatch):
    """T3B2: CRASHED must be treated like FAILED — it must not block reset
    forever just because it never reached a clean terminal state."""
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(graph_api, "GraphBuilderService", lambda **_kwargs: SimpleNamespace(
        delete_graph=lambda _graph_id: None
    ))
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: []),
    )
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "get_simulation_ids_for_graph",
        classmethod(lambda _cls, _graph_id: ["sim-crashed"]),
    )
    discarded = []
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "discard_inactive_updater",
        classmethod(lambda _cls, simulation_id: discarded.append(simulation_id)),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.CRASHED)
        ),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 200
    assert project.graph_id is None
    assert discarded == ["sim-crashed"]


def test_graph_delete_cannot_discard_an_updater_during_finalization(monkeypatch):
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "get_simulation_ids_for_graph",
        classmethod(lambda _cls, _graph_id: ["sim-finalizing"]),
    )
    discarded = []
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "discard_inactive_updater",
        classmethod(
            lambda _cls, simulation_id: discarded.append(simulation_id)
        ),
    )
    lock = graph_api.SimulationRunner._finalization_lock("sim-finalizing")
    lock.acquire()
    try:
        assert graph_api._active_graph_consumers("graph-1") == ["sim-finalizing"]
    finally:
        lock.release()

    assert discarded == []


def test_repeated_build_request_reuses_the_existing_task(monkeypatch):
    project = _project(ProjectStatus.GRAPH_BUILDING)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api,
        "TaskManager",
        lambda: SimpleNamespace(
            get_task=lambda _task_id: SimpleNamespace(status=TaskStatus.PROCESSING)
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={"project_id": "proj-1", "force": True},
    ):
        body, status = _json_result(graph_api.build_graph())

    assert status == 200
    assert body["success"] is True
    assert body["data"]["reused"] is True
    assert body["data"]["task_id"] == "task-1"
    assert body["data"]["graph_id"] == "graph-1"


def test_stale_build_after_restart_is_recoverable_instead_of_reused(monkeypatch):
    project = _project(ProjectStatus.GRAPH_BUILDING)
    project.zep_batch_id = None
    project.zep_batch_operation_id = None
    saved = []
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, value: saved.append(value.status)),
    )
    monkeypatch.setattr(
        graph_api,
        "TaskManager",
        lambda: SimpleNamespace(get_task=lambda _task_id: None),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={"project_id": "proj-1"},
    ):
        body, status = _json_result(graph_api.build_graph())

    assert status == 409
    assert body["recoverable"] is True
    assert project.status == ProjectStatus.FAILED
    assert saved == [ProjectStatus.FAILED]


def test_stale_build_resumes_a_persisted_processing_batch(monkeypatch):
    project = _project(ProjectStatus.GRAPH_BUILDING)
    created_threads = []

    class Tasks:
        def get_task(self, _task_id):
            return None

        def create_task(self, _description):
            return "task-resumed"

    class Builder:
        def __init__(self, **_kwargs):
            pass

        def get_batch_summary(self, batch_id):
            assert batch_id == "batch-1"
            return SimpleNamespace(status="processing")

    class Thread:
        def __init__(self, *, target, daemon):
            created_threads.append((target, daemon))

        def start(self):
            pass

    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(graph_api, "TaskManager", Tasks)
    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)
    monkeypatch.setattr(graph_api.threading, "Thread", Thread)
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_extracted_text",
        classmethod(lambda _cls, _project_id: "source text"),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={"project_id": "proj-1"},
    ):
        body, status = _json_result(graph_api.build_graph())

    assert status == 200
    assert body["data"]["resumed"] is True
    assert body["data"]["task_id"] == "task-resumed"
    assert project.graph_build_task_id == "task-resumed"
    assert len(created_threads) == 1


def test_project_delete_removes_cloud_graph_before_local_files(monkeypatch):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    events = []

    class Builder:
        def __init__(self, **_kwargs):
            pass

        def delete_graph(self, graph_id):
            events.append(("cloud-delete", graph_id))

    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "delete_project",
        classmethod(
            lambda _cls, project_id: events.append(("local-delete", project_id)) or True
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/project/proj-1",
        method="DELETE",
    ):
        body, status = _json_result(graph_api.delete_project("proj-1"))

    assert status == 200
    assert body["success"] is True
    assert events == [
        ("cloud-delete", "graph-1"),
        ("local-delete", "proj-1"),
    ]


def test_completed_build_request_is_idempotent_without_force(monkeypatch):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={"project_id": "proj-1"},
    ):
        body, status = _json_result(graph_api.build_graph())

    assert status == 200
    assert body["data"]["reused"] is True
    assert body["data"]["graph_id"] == "graph-1"


def test_force_must_be_a_json_boolean(monkeypatch):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/graph/build",
        method="POST",
        json={"project_id": "proj-1", "force": "false"},
    ):
        body, status = _json_result(graph_api.build_graph())

    assert status == 400
    assert "boolean" in body["error"]


def test_graph_reset_and_memory_start_cannot_cross_between_delete_and_clear(
    monkeypatch,
):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    simulation = SimpleNamespace(
        simulation_id="sim-1",
        project_id=project.project_id,
        graph_id=project.graph_id,
        status=SimulationStatus.READY,
    )
    delete_entered = threading.Event()
    allow_delete = threading.Event()
    runner_called = []

    class Builder:
        def __init__(self, **_kwargs):
            pass

        def delete_graph(self, graph_id):
            assert graph_id == "graph-1"
            delete_entered.set()
            assert allow_delete.wait(timeout=2)

    class Simulations:
        def get_simulation(self, _simulation_id):
            return simulation

    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)
    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(simulation_api, "SimulationManager", Simulations)
    monkeypatch.setattr(
        simulation_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        simulation_api.SimulationRunner,
        "start_simulation",
        classmethod(lambda _cls, **_kwargs: runner_called.append(True)),
    )

    app = Flask(__name__)
    results = {}

    def reset():
        with app.test_request_context(
            "/api/graph/project/proj-1/reset", method="POST"
        ):
            results["reset"] = _json_result(graph_api.reset_project("proj-1"))

    def start():
        with app.test_request_context(
            "/api/simulation/start",
            method="POST",
            json={
                "simulation_id": "sim-1",
                "enable_graph_memory_update": True,
            },
        ):
            results["start"] = _json_result(simulation_api.start_simulation())

    reset_thread = threading.Thread(target=reset)
    reset_thread.start()
    assert delete_entered.wait(timeout=2)

    start_thread = threading.Thread(target=start)
    start_thread.start()
    start_thread.join(timeout=0.05)
    assert start_thread.is_alive()

    allow_delete.set()
    reset_thread.join(timeout=2)
    start_thread.join(timeout=2)

    assert results["reset"][1] == 200
    assert results["start"][1] == 409
    assert runner_called == []
    assert project.graph_id is None


# ═══════════════════════════════════════════════════════════════
# T3B2 putaran 2 (final): 3 temuan review di _active_graph_consumers()
# ═══════════════════════════════════════════════════════════════


def test_reset_survives_a_corrupt_report_file_elsewhere_in_reports_dir(
    monkeypatch, tmp_path
):
    """[CRITICAL] Satu file report corrupt DI MANA PUN di REPORTS_DIR (bukan
    cuma report project ini) dulu bikin get_report_by_simulation() melempar
    exception yang naik jadi 500 dan mengeblok reset untuk SEMUA project.
    Sekarang report corrupt harus di-treat sebagai "tidak ada" (fail-open),
    dibuktikan lewat HTTP request NYATA (Flask beneran + urllib), bukan
    hanya lewat pemanggilan fungsi terisolasi."""
    monkeypatch.setattr(graph_api.ReportManager, "REPORTS_DIR", str(tmp_path))

    # Report corrupt milik simulasi lain sama sekali, di project lain.
    corrupt_dir = tmp_path / "report-corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "meta.json").write_text("{ini bukan json valid", encoding="utf-8")

    project = _project(ProjectStatus.GRAPH_COMPLETED)
    simulation = SimpleNamespace(simulation_id="sim-no-report", graph_id="graph-1")

    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api,
        "GraphBuilderService",
        lambda **_kwargs: SimpleNamespace(delete_graph=lambda _graph_id: None),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: [simulation]),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
        ),
    )

    with _real_server(graph_api.graph_bp, "/api/graph") as base_url:
        status, body = _http_post(f"{base_url}/api/graph/project/proj-1/reset")

    assert status == 200, body
    assert body["success"] is True
    assert project.graph_id is None


def test_active_graph_consumers_scans_reports_dir_at_most_once(monkeypatch, tmp_path):
    """[HIGH - N+1] get_report_by_simulation() dulu di-panggil ULANG di
    dalam loop, dan tiap panggilan itu sendiri melakukan full os.listdir()
    + buka/parse SETIAP file report di seluruh direktori. Dengan 5 simulasi
    COMPLETED yang menunjuk graph yang sama, itu 5x full-scan. Sekarang
    direktori report harus di-scan MAKSIMAL sekali per panggilan
    _active_graph_consumers(), berapa pun banyaknya simulasi COMPLETED."""
    monkeypatch.setattr(graph_api.ReportManager, "REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "get_graph_readers", lambda _graph_id: [])
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "get_simulation_ids_for_graph",
        classmethod(lambda _cls, _graph_id: []),
    )

    simulations = [
        SimpleNamespace(simulation_id=f"sim-{i}", graph_id="graph-1")
        for i in range(5)
    ]
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: simulations),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
        ),
    )

    listdir_calls = []
    real_listdir = os.listdir

    def spy_listdir(path):
        listdir_calls.append(path)
        return real_listdir(path)

    monkeypatch.setattr(graph_api.os, "listdir", spy_listdir)

    active = graph_api._active_graph_consumers("graph-1")

    assert active == []
    # 5 simulasi COMPLETED, tapi listdir(REPORTS_DIR) cuma boleh sekali.
    assert listdir_calls == [str(tmp_path)]


def test_project_reset_allows_a_completed_simulation_with_a_failed_report(
    monkeypatch, tmp_path
):
    """[HIGH - status guard, NEGATIF PALING PENTING] save_report() dipanggil
    UNCONDITIONAL walau status FAILED (lihat api/report.py baris
    ReportManager.save_report(report) sesudah generate_report()). Kode lama
    cuma cek truthy "ADA report", jadi report FAILED yang tersimpan di disk
    dianggap "masih dipakai" dan mengeblok reset SELAMANYA. Sekarang cuma
    report berstatus COMPLETED yang boleh mengeblok."""
    monkeypatch.setattr(graph_api.ReportManager, "REPORTS_DIR", str(tmp_path))

    failed_report = Report(
        report_id="report-failed",
        simulation_id="sim-failed",
        graph_id="graph-1",
        simulation_requirement="req",
        status=ReportStatus.FAILED,
        created_at="2026-01-01T00:00:00",
    )
    graph_api.ReportManager.save_report(failed_report)

    project = _project(ProjectStatus.GRAPH_COMPLETED)
    simulation = SimpleNamespace(simulation_id="sim-failed", graph_id="graph-1")

    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api,
        "GraphBuilderService",
        lambda **_kwargs: SimpleNamespace(delete_graph=lambda _graph_id: None),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: [simulation]),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
        ),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 200, body
    assert project.graph_id is None


def test_project_reset_still_blocked_by_a_genuinely_completed_report(
    monkeypatch, tmp_path
):
    """Arah sebaliknya - NOL REGRESI: report yang beneran COMPLETED tetap
    harus mengeblok reset, persis seperti sebelum putaran fix ini."""
    monkeypatch.setattr(graph_api.ReportManager, "REPORTS_DIR", str(tmp_path))

    completed_report = Report(
        report_id="report-completed",
        simulation_id="sim-completed-2",
        graph_id="graph-1",
        simulation_requirement="req",
        status=ReportStatus.COMPLETED,
        created_at="2026-01-01T00:00:00",
    )
    graph_api.ReportManager.save_report(completed_report)

    project = _project(ProjectStatus.GRAPH_COMPLETED)
    simulation = SimpleNamespace(simulation_id="sim-completed-2", graph_id="graph-1")

    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: [simulation]),
    )
    monkeypatch.setattr(
        graph_api.SimulationRunner,
        "get_run_state",
        classmethod(
            lambda _cls, _sid: SimpleNamespace(runner_status=RunnerStatus.COMPLETED)
        ),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/graph/project/proj-1/reset", method="POST"):
        body, status = _json_result(graph_api.reset_project("proj-1"))

    assert status == 409
    assert "sim-completed-2" in body["error"]
    assert project.graph_id == "graph-1"

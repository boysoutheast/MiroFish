"""GET /<simulation_id>/run-status must expose ingestion drain stats (queue_size,
pending_episode_count, etc.) so the frontend can tell the user when it is safe
to click Stop Simulation / Generate Report.
"""

from app import create_app
from app.services.simulation_runner import SimulationRunner, SimulationRunState
from app.services.zep_graph_memory_updater import ZepGraphMemoryManager


def _register_run_state(simulation_id="sim_ingest_test"):
    state = SimulationRunState(simulation_id=simulation_id)
    SimulationRunner._run_states[simulation_id] = state
    return state


def _cleanup(simulation_id):
    SimulationRunner._run_states.pop(simulation_id, None)
    with ZepGraphMemoryManager._lock:
        ZepGraphMemoryManager._updaters.pop(simulation_id, None)


def test_run_status_ingestion_is_none_without_active_updater():
    simulation_id = "sim_ingest_none"
    _register_run_state(simulation_id)
    try:
        app = create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        response = client.get(f"/api/simulation/{simulation_id}/run-status")

        assert response.status_code == 200
        assert response.json["data"]["ingestion"] is None
    finally:
        _cleanup(simulation_id)


def test_run_status_ingestion_reports_active_updater_stats(monkeypatch):
    simulation_id = "sim_ingest_active"
    _register_run_state(simulation_id)

    fixture_stats = {
        "graph_id": "graph_x",
        "batch_size": 10,
        "total_activities": 42,
        "batches_sent": 3,
        "items_sent": 30,
        "failed_count": 1,
        "pending_episode_count": 2,
        "skipped_count": 5,
        "queue_size": 7,
        "buffer_sizes": {"twitter": 0, "reddit": 0},
        "running": True,
    }

    class FakeUpdater:
        def get_stats(self):
            return fixture_stats

    monkeypatch.setattr(
        ZepGraphMemoryManager, "get_updater", classmethod(lambda cls, sim_id: FakeUpdater())
    )

    try:
        app = create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        response = client.get(f"/api/simulation/{simulation_id}/run-status")

        assert response.status_code == 200
        ingestion = response.json["data"]["ingestion"]
        assert ingestion is not None
        assert ingestion["queue_size"] == 7
        assert ingestion["pending_episode_count"] == 2
        assert ingestion["items_sent"] == 30
        assert ingestion["total_activities"] == 42
        assert ingestion["skipped_count"] == 5
        assert ingestion["failed_count"] == 1
        assert ingestion["buffered_count"] == 0
    finally:
        _cleanup(simulation_id)


def test_run_status_ingestion_reports_buffered_count(monkeypatch):
    simulation_id = "sim_ingest_buffered"
    _register_run_state(simulation_id)

    fixture_stats = {
        "graph_id": "graph_x",
        "batch_size": 5,
        "total_activities": 175,
        "batches_sent": 35,
        "items_sent": 175,
        "failed_count": 0,
        "pending_episode_count": 0,
        "skipped_count": 0,
        "queue_size": 0,
        "buffer_sizes": {"twitter": 3, "reddit": 1},
        "running": True,
    }

    class FakeUpdater:
        def get_stats(self):
            return fixture_stats

    monkeypatch.setattr(
        ZepGraphMemoryManager, "get_updater", classmethod(lambda cls, sim_id: FakeUpdater())
    )

    try:
        app = create_app()
        app.config.update(TESTING=True)
        client = app.test_client()

        response = client.get(f"/api/simulation/{simulation_id}/run-status")

        assert response.status_code == 200
        ingestion = response.json["data"]["ingestion"]
        assert ingestion is not None
        assert ingestion["buffered_count"] == 4
    finally:
        _cleanup(simulation_id)

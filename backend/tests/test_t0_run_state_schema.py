"""T0 — fondasi skema status untuk jaminan bayar-per-project.

Cakupan uji:
1. Backward-compat load: run_state.json versi LAMA (tanpa 6 field baru) harus
   bisa di-load lewat SimulationRunner._load_run_state() tanpa exception, dan
   semua field baru dapat default yang benar.
2. Dua arah _sync_simulation_status(): CRASHED dan NEEDS_ATTENTION benar-benar
   mengubah isi state.json yang dipetakan, plus regresi negatif untuk status
   lama (RUNNING/COMPLETED) yang sudah ada sebelumnya.
3. Round-trip: SimulationRunState dengan 6 field baru terisi -> to_dict() ->
   load balik -> nilai identik.
"""

import glob
import json
import os
import shutil
from unittest import mock

import pytest

from app.services.simulation_runner import RunnerStatus, SimulationRunner, SimulationRunState
from app.services.simulation_manager import SimulationManager, SimulationStatus


def _sim_dir(simulation_id: str) -> str:
    return os.path.join(SimulationRunner.RUN_STATE_DIR, simulation_id)


def _cleanup_runner(simulation_id: str):
    SimulationRunner._run_states.pop(simulation_id, None)
    sim_dir = _sim_dir(simulation_id)
    if os.path.exists(sim_dir):
        shutil.rmtree(sim_dir)


def _cleanup_manager(simulation_id: str):
    sim_dir = os.path.join(SimulationManager.SIMULATION_DATA_DIR, simulation_id)
    if os.path.exists(sim_dir):
        shutil.rmtree(sim_dir)


# ---------------------------------------------------------------------------
# 1. Backward-compat load
# ---------------------------------------------------------------------------

def test_load_run_state_old_json_without_new_fields_gets_defaults():
    simulation_id = "sim_t0_old_format"
    sim_dir = _sim_dir(simulation_id)
    os.makedirs(sim_dir, exist_ok=True)

    # Simulasi run_state.json versi LAMA — tanpa 6 field baru sama sekali.
    old_data = {
        "simulation_id": simulation_id,
        "runner_status": "running",
        "current_round": 3,
        "total_rounds": 10,
        "simulated_hours": 6,
        "total_simulation_hours": 20,
        "twitter_current_round": 3,
        "reddit_current_round": 2,
        "twitter_simulated_hours": 6,
        "reddit_simulated_hours": 4,
        "twitter_running": True,
        "reddit_running": True,
        "twitter_completed": False,
        "reddit_completed": False,
        "twitter_actions_count": 5,
        "reddit_actions_count": 2,
        "started_at": "2026-09-19T10:00:00",
        "updated_at": "2026-09-19T10:05:00",
        "completed_at": None,
        "error": None,
        "process_pid": 12345,
        "recent_actions": [],
        # SENGAJA tidak menyertakan: process_started_at, retry_count,
        # retried_at, start_params, ingestion_incomplete,
        # ingestion_incomplete_detail
    }
    state_file = os.path.join(sim_dir, "run_state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(old_data, f)

    try:
        # Tidak boleh exception.
        state = SimulationRunner._load_run_state(simulation_id)

        assert state is not None
        assert state.simulation_id == simulation_id
        assert state.runner_status == RunnerStatus.RUNNING
        assert state.process_pid == 12345

        # Semua field baru dapat default yang benar.
        assert state.process_started_at is None
        assert state.retry_count == 0
        assert state.retried_at is None
        assert state.start_params is None
        assert state.ingestion_incomplete is False
        assert state.ingestion_incomplete_detail is None
    finally:
        _cleanup_runner(simulation_id)


# ---------------------------------------------------------------------------
# 2. Dua arah _sync_simulation_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "runner_status,expected_manager_status",
    [
        (RunnerStatus.CRASHED, SimulationStatus.CRASHED),
        (RunnerStatus.NEEDS_ATTENTION, SimulationStatus.NEEDS_ATTENTION),
    ],
)
def test_sync_simulation_status_maps_new_statuses(runner_status, expected_manager_status):
    manager = SimulationManager()
    sim = manager.create_simulation(project_id="proj_t0", graph_id="graph_t0")
    simulation_id = sim.simulation_id

    try:
        SimulationRunner._sync_simulation_status(
            simulation_id, runner_status, error="boom"
        )

        # Baca balik lewat instance SimulationManager BARU — memaksa baca dari
        # file, bukan cache in-memory milik instance yang menulis.
        fresh_manager = SimulationManager()
        reloaded = fresh_manager.get_simulation(simulation_id)

        assert reloaded is not None
        assert reloaded.status == expected_manager_status
        assert reloaded.error == "boom"

        # Isi file di disk juga harus benar-benar berubah, bukan cuma cache.
        state_file = os.path.join(
            SimulationManager.SIMULATION_DATA_DIR, simulation_id, "state.json"
        )
        with open(state_file, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["status"] == expected_manager_status.value
        assert on_disk["error"] == "boom"
    finally:
        _cleanup_manager(simulation_id)


@pytest.mark.parametrize(
    "runner_status,expected_manager_status",
    [
        (RunnerStatus.RUNNING, SimulationStatus.RUNNING),
        (RunnerStatus.COMPLETED, SimulationStatus.COMPLETED),
        (RunnerStatus.STOPPED, SimulationStatus.STOPPED),
        (RunnerStatus.FAILED, SimulationStatus.FAILED),
    ],
)
def test_sync_simulation_status_existing_statuses_no_regression(
    runner_status, expected_manager_status
):
    """Status yang SUDAH ADA sebelum T0 tetap ke-mapping benar (nol regresi)."""
    manager = SimulationManager()
    sim = manager.create_simulation(project_id="proj_t0b", graph_id="graph_t0b")
    simulation_id = sim.simulation_id

    try:
        SimulationRunner._sync_simulation_status(simulation_id, runner_status)

        fresh_manager = SimulationManager()
        reloaded = fresh_manager.get_simulation(simulation_id)

        assert reloaded is not None
        assert reloaded.status == expected_manager_status
    finally:
        _cleanup_manager(simulation_id)


def test_sync_simulation_status_unmapped_runner_status_is_noop():
    """RunnerStatus yang sengaja TIDAK dipetakan (mis. STARTING) harus diam-diam
    dilewati (return), tapi TIDAK boleh mengubah status yang sudah tersimpan."""
    manager = SimulationManager()
    sim = manager.create_simulation(project_id="proj_t0c", graph_id="graph_t0c")
    simulation_id = sim.simulation_id

    try:
        # Set status awal ke RUNNING lewat mapping yang valid.
        SimulationRunner._sync_simulation_status(simulation_id, RunnerStatus.RUNNING)

        # STARTING sengaja tidak ada di status_map -> no-op.
        SimulationRunner._sync_simulation_status(simulation_id, RunnerStatus.STARTING)

        fresh_manager = SimulationManager()
        reloaded = fresh_manager.get_simulation(simulation_id)
        assert reloaded.status == SimulationStatus.RUNNING
    finally:
        _cleanup_manager(simulation_id)


# ---------------------------------------------------------------------------
# 3. Round-trip SimulationRunState -> to_dict() -> load balik
# ---------------------------------------------------------------------------

def test_run_state_round_trip_preserves_new_fields():
    simulation_id = "sim_t0_roundtrip"

    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.CRASHED,
        process_started_at=1758262800.123456,
        retry_count=1,
        retried_at="2026-09-19T11:00:00",
        start_params={
            "platform": "parallel",
            "max_rounds": 50,
            "graph_id": "graph_xyz",
            "enable_graph_memory_update": True,
        },
        ingestion_incomplete=True,
        ingestion_incomplete_detail="Zep drain timeout after 30s",
    )

    try:
        SimulationRunner._save_run_state(state)

        # Bersihkan cache in-memory supaya load berikutnya BENAR-BENAR baca file.
        SimulationRunner._run_states.pop(simulation_id, None)

        loaded = SimulationRunner._load_run_state(simulation_id)

        assert loaded is not None
        assert loaded.process_started_at == 1758262800.123456
        assert loaded.retry_count == 1
        assert loaded.retried_at == "2026-09-19T11:00:00"
        assert loaded.start_params == {
            "platform": "parallel",
            "max_rounds": 50,
            "graph_id": "graph_xyz",
            "enable_graph_memory_update": True,
        }
        assert loaded.ingestion_incomplete is True
        assert loaded.ingestion_incomplete_detail == "Zep drain timeout after 30s"
        assert loaded.runner_status == RunnerStatus.CRASHED
    finally:
        _cleanup_runner(simulation_id)


def test_start_simulation_populates_start_params(monkeypatch):
    """start_simulation() harus mengisi start_params dengan parameter yang
    genuinely dipakai untuk start (platform/max_rounds/graph_id/
    enable_graph_memory_update). Proses OASIS asli TIDAK boleh benar-benar
    di-spawn di test ini — subprocess.Popen dan monitor thread di-stub."""
    simulation_id = "sim_t0_start_params"
    sim_dir = _sim_dir(simulation_id)
    os.makedirs(sim_dir, exist_ok=True)

    config = {
        "time_config": {
            "total_simulation_hours": 10,
            "minutes_per_round": 60,
        }
    }
    with open(os.path.join(sim_dir, "simulation_config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f)

    class FakeProcess:
        pid = 999999

        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        return FakeProcess()

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            # Sengaja tidak benar-benar berjalan — cukup untuk memverifikasi
            # start_params tersimpan sebelum monitor thread dimulai.
            pass

    monkeypatch.setattr(
        "app.services.simulation_runner.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "app.services.simulation_runner.threading.Thread", FakeThread
    )

    try:
        state = SimulationRunner.start_simulation(
            simulation_id=simulation_id,
            platform="twitter",
            max_rounds=7,
            enable_graph_memory_update=False,
            graph_id=None,
        )

        assert state.start_params == {
            "platform": "twitter",
            "max_rounds": 7,
            "graph_id": None,
            "enable_graph_memory_update": False,
        }

        reloaded = SimulationRunner.get_run_state(simulation_id)
        assert reloaded is not None
        assert reloaded.start_params == state.start_params
    finally:
        SimulationRunner._processes.pop(simulation_id, None)
        SimulationRunner._monitor_threads.pop(simulation_id, None)
        SimulationRunner._action_queues.pop(simulation_id, None)
        stdout_file = SimulationRunner._stdout_files.pop(simulation_id, None)
        if stdout_file:
            try:
                stdout_file.close()
            except Exception:
                pass
        SimulationRunner._stderr_files.pop(simulation_id, None)
        _cleanup_runner(simulation_id)


# ---------------------------------------------------------------------------
# 4. _save_run_state() ATOMIC WRITE (putaran fix ke-2, temuan CRITICAL).
#
# Seluruh jaminan T2 ("catat retry_count=1 SEBELUM spawn proses baru")
# bergantung penuh ke write ini genuinely reliable -- kalau proses mati
# PERSIS di tengah json.dump() lama, file jadi corrupt/truncated dan
# simulasi itu hilang dari radar reconciliation selamanya. Fix: tulis ke
# file temporary di direktori yang sama, fsync, lalu os.replace() (atomic).
# ---------------------------------------------------------------------------


def test_save_run_state_process_killed_mid_write_leaves_original_file_intact():
    """Simulasikan proses OS mati PERSIS sesudah tempfile ditulis tapi
    SEBELUM os.replace() -- file ASLI (state_file) harus tetap versi lama
    yang utuh, dan file temp yang setengah jadi harus dibersihkan (tidak
    nyampah di direktori)."""
    simulation_id = "sim_t0_atomic_interrupt_before_replace"

    old_state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
        retry_count=0,
        error=None,
    )

    try:
        # Tulis versi LAMA dulu, genuinely lewat _save_run_state (sudah
        # atomic) supaya file awal ini representatif.
        SimulationRunner._save_run_state(old_state)
        state_file = os.path.join(_sim_dir(simulation_id), "run_state.json")
        with open(state_file, "r", encoding="utf-8") as f:
            original_bytes = f.read()
        assert '"retry_count": 0' in original_bytes

        new_state = SimulationRunState(
            simulation_id=simulation_id,
            runner_status=RunnerStatus.NEEDS_ATTENTION,
            retry_count=1,
            error="ini TIDAK BOLEH sampai ke disk",
        )

        # Mock os.replace supaya melempar exception -- meniru proses yang
        # mati/OSError PERSIS di titik itu, SESUDAH tempfile lengkap+fsync
        # tapi SEBELUM rename atomic terjadi.
        with mock.patch(
            "app.services.simulation_runner.os.replace",
            side_effect=OSError("simulated OS kill mid-write"),
        ):
            with pytest.raises(OSError):
                SimulationRunner._save_run_state(new_state)

        # File ASLI harus TETAP UTUH -- masih versi lama, byte-for-byte.
        with open(state_file, "r", encoding="utf-8") as f:
            after_bytes = f.read()
        assert after_bytes == original_bytes
        assert '"retry_count": 0' in after_bytes
        assert "ini TIDAK BOLEH sampai ke disk" not in after_bytes

        # File temp yang setengah jadi harus DIBERSIHKAN, tidak nyampah.
        leftover_tmp = glob.glob(
            os.path.join(_sim_dir(simulation_id), "run_state_*.tmp")
        )
        assert leftover_tmp == [], f"file temp harusnya kehapus: {leftover_tmp}"
    finally:
        _cleanup_runner(simulation_id)


def test_save_run_state_interrupted_before_fsync_leaves_original_file_intact():
    """Variasi lain: exception terjadi lebih awal lagi, saat json.dump()
    atau fsync (mis. disk penuh) -- file asli tetap harus utuh dan file
    temp tetap harus dibersihkan."""
    simulation_id = "sim_t0_atomic_interrupt_before_fsync"

    old_state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.RUNNING,
        retry_count=0,
    )

    try:
        SimulationRunner._save_run_state(old_state)
        state_file = os.path.join(_sim_dir(simulation_id), "run_state.json")
        with open(state_file, "r", encoding="utf-8") as f:
            original_bytes = f.read()

        new_state = SimulationRunState(
            simulation_id=simulation_id,
            runner_status=RunnerStatus.CRASHED,
            retry_count=1,
        )

        with mock.patch(
            "app.services.simulation_runner.os.fsync",
            side_effect=OSError("simulated disk full during fsync"),
        ):
            with pytest.raises(OSError):
                SimulationRunner._save_run_state(new_state)

        with open(state_file, "r", encoding="utf-8") as f:
            after_bytes = f.read()
        assert after_bytes == original_bytes

        leftover_tmp = glob.glob(
            os.path.join(_sim_dir(simulation_id), "run_state_*.tmp")
        )
        assert leftover_tmp == [], f"file temp harusnya kehapus: {leftover_tmp}"
    finally:
        _cleanup_runner(simulation_id)


def test_save_run_state_normal_write_round_trip_still_correct_after_atomic_change():
    """Write normal (tanpa interupsi) -> baca balik -> isi identik. Pastikan
    perubahan ke atomic write TIDAK merusak alur normal (round-trip dasar,
    di luar test round-trip 6-field T0 yang sudah ada di atas)."""
    simulation_id = "sim_t0_atomic_normal_roundtrip"

    state = SimulationRunState(
        simulation_id=simulation_id,
        runner_status=RunnerStatus.COMPLETED,
        current_round=42,
        retry_count=1,
        retried_at="2026-09-19T12:00:00",
        error=None,
    )

    try:
        SimulationRunner._save_run_state(state)
        SimulationRunner._run_states.pop(simulation_id, None)

        loaded = SimulationRunner._load_run_state(simulation_id)

        assert loaded is not None
        assert loaded.runner_status == RunnerStatus.COMPLETED
        assert loaded.current_round == 42
        assert loaded.retry_count == 1
        assert loaded.retried_at == "2026-09-19T12:00:00"

        # Tidak ada file temp tersisa sesudah write sukses.
        leftover_tmp = glob.glob(
            os.path.join(_sim_dir(simulation_id), "run_state_*.tmp")
        )
        assert leftover_tmp == []
    finally:
        _cleanup_runner(simulation_id)

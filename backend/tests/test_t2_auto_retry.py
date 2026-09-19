"""T2 — auto-retry SEKALI untuk simulasi CRASHED.

Bagian paling berisiko dari PRD "jaminan bayar-per-project": ini satu-satunya
task yang menjalankan pekerjaan berbayar (spawn proses simulasi) TANPA klik
user. Cakupan uji di file ini SENGAJA end-to-end dengan proses OS asli
(bukan cuma mock), pola yang sama seperti test_t1_simulation_reconciler.py:

1. Retry sukses: simulasi CRASHED -> reconcile -> retry_count=1 di disk DAN
   proses BARU genuinely ke-spawn (PID baru != PID lama).
2. SEKALI doang (PALING PENTING): simulasi retry_count=1 yang crash LAGI ->
   NEEDS_ATTENTION, NOL proses baru ke-spawn, retry_count TETAP 1.
3. Urutan kritis: crash PERSIS sesudah retry_count tersimpan tapi SEBELUM
   proses baru sempat spawn -> restart berikutnya TIDAK retry kedua kali.
4. Graph sudah dihapus/reset -> LANGSUNG needs_attention, nol percobaan.
5. Concurrency limit -> cuma _MAX_CONCURRENT_RETRIES yang genuinely retry,
   sisanya langsung needs_attention.
6. cleanup_simulation_logs() beneran terpanggil sebelum spawn (data lama
   kehapus).
7. reconcile_on_startup() men-dispatch retry ke background thread -- tidak
   blocking create_app()/startup.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from unittest import mock

import psutil
import pytest

from app.services import simulation_reconciler as reconciler_module
from app.services.simulation_reconciler import (
    _MAX_CONCURRENT_RETRIES,
    _graph_still_valid,
    _retry_crashed_simulations,
    _retry_eligibility_reason,
    reconcile_on_startup,
)
from app.services.simulation_runner import RunnerStatus, SimulationRunner
from app.services.simulation_manager import SimulationManager
from app.models.project import ProjectManager, ProjectStatus


def _sim_dir(simulation_id: str) -> str:
    return os.path.join(SimulationRunner.RUN_STATE_DIR, simulation_id)


def _write_run_state(simulation_id: str, **overrides):
    sim_dir = _sim_dir(simulation_id)
    os.makedirs(sim_dir, exist_ok=True)
    data = {
        "simulation_id": simulation_id,
        "runner_status": "running",
        "current_round": 1,
        "total_rounds": 10,
        "simulated_hours": 1,
        "total_simulation_hours": 10,
        "twitter_current_round": 1,
        "reddit_current_round": 1,
        "twitter_simulated_hours": 1,
        "reddit_simulated_hours": 1,
        "twitter_running": True,
        "reddit_running": True,
        "twitter_completed": False,
        "reddit_completed": False,
        "twitter_actions_count": 0,
        "reddit_actions_count": 0,
        "started_at": "2026-09-19T10:00:00",
        "updated_at": "2026-09-19T10:00:00",
        "completed_at": None,
        "error": None,
        "process_pid": None,
        "process_started_at": None,
        "retry_count": 0,
        "retried_at": None,
        "start_params": None,
        "ingestion_incomplete": False,
        "ingestion_incomplete_detail": None,
        "recent_actions": [],
    }
    data.update(overrides)
    with open(os.path.join(sim_dir, "run_state.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _write_simulation_config(simulation_id: str):
    sim_dir = _sim_dir(simulation_id)
    os.makedirs(sim_dir, exist_ok=True)
    config = {
        "time_config": {
            "total_simulation_hours": 10,
            "minutes_per_round": 60,
        }
    }
    with open(
        os.path.join(sim_dir, "simulation_config.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(config, f)


def _write_stale_logs(simulation_id: str):
    """Tulis data lama (actions.jsonl + db) yang HARUS kehapus sebelum
    retry, kalau tidak angka report bakal dobel."""
    sim_dir = _sim_dir(simulation_id)
    for platform in ("twitter", "reddit"):
        platform_dir = os.path.join(sim_dir, platform)
        os.makedirs(platform_dir, exist_ok=True)
        with open(
            os.path.join(platform_dir, "actions.jsonl"), "w", encoding="utf-8"
        ) as f:
            f.write('{"round_num": 1, "action_type": "OLD_STALE_ACTION"}\n')
    with open(
        os.path.join(sim_dir, "twitter_simulation.db"), "w", encoding="utf-8"
    ) as f:
        f.write("stale db content")


def _spawn_dummy_process():
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    time.sleep(0.2)
    p = psutil.Process(proc.pid)
    return proc, p.create_time()


def _cleanup(simulation_id: str):
    SimulationRunner._run_states.pop(simulation_id, None)
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
    sim_dir = _sim_dir(simulation_id)
    if os.path.exists(sim_dir):
        shutil.rmtree(sim_dir)


def _cleanup_manager(simulation_id: str):
    sim_dir = os.path.join(SimulationManager.SIMULATION_DATA_DIR, simulation_id)
    if os.path.exists(sim_dir):
        shutil.rmtree(sim_dir)


def _cleanup_project(project_id: str):
    project_dir = ProjectManager._get_project_dir(project_id)
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir)


def _noop_monitor(cls, simulation_id, locale="zh"):
    """Stub buat SimulationRunner._monitor_simulation -- kalau real
    threading.Thread yang menjalankan monitor, dia bakal coba baca
    twitter/actions.jsonl dari skrip OASIS asli yang di test ini TIDAK kita
    jalankan (kita spawn `sleep 60` yang genuine buat pembuktian PID, bukan
    skrip beneran). Kita TIDAK boleh stub threading.Thread itu sendiri --
    `threading` adalah modul singleton, patch di situ ikut mematikan thread
    yang dipakai simulation_reconciler.py sendiri buat retry paralel."""
    return None


def _patch_real_spawn(monkeypatch):
    """Ganti subprocess.Popen di dalam simulation_runner supaya benar-benar
    spawn proses OS asli (`sleep 60`) alih-alih menjalankan skrip OASIS
    (yang tidak ada di test env ini) -- PID yang dihasilkan tetap genuine,
    dan monitor thread di-stub (bukan threading.Thread-nya) supaya tidak
    coba baca actions.jsonl asli."""
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        return real_popen(["sleep", "60"], start_new_session=True)

    monkeypatch.setattr(
        "app.services.simulation_runner.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(SimulationRunner, "_monitor_simulation", classmethod(_noop_monitor))


# ---------------------------------------------------------------------------
# 1. E2E retry sukses: PID baru genuinely ke-spawn, retry_count=1
# ---------------------------------------------------------------------------


def test_e2e_retry_success_spawns_new_pid_and_bumps_retry_count(monkeypatch):
    simulation_id = "sim_t2_retry_success"
    _patch_real_spawn(monkeypatch)

    old_proc, old_created_at = _spawn_dummy_process()
    old_pid = old_proc.pid
    old_proc.kill()
    old_proc.wait(timeout=5)

    _write_simulation_config(simulation_id)
    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=old_pid,
        process_started_at=old_created_at,
        retry_count=0,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )

    try:
        # T1: tandai CRASHED dulu (pola nyata reconcile_on_startup).
        result = reconciler_module._reconcile_one(simulation_id)
        assert result == "crashed"

        # T2: eksekusi retry logic langsung (sinkron, deterministik --
        # _retry_crashed_simulations sendiri sudah join() semua thread
        # retry sebelum return).
        _retry_crashed_simulations([simulation_id])

        state = SimulationRunner._load_run_state(simulation_id)
        assert state is not None
        assert state.retry_count == 1
        assert state.retried_at is not None
        assert state.runner_status == RunnerStatus.RUNNING

        new_pid = state.process_pid
        print(f"[E2E retry sukses] PID lama={old_pid}, PID baru={new_pid}")
        assert new_pid is not None
        assert new_pid != old_pid
        assert psutil.pid_exists(new_pid)

        # Bersihkan proses baru.
        new_proc = psutil.Process(new_pid)
        new_proc.kill()
        new_proc.wait(timeout=5)
    finally:
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 2. SEKALI DOANG (paling penting): retry_count=1 + CRASHED lagi -> nol
#    proses baru, nol retry kedua.
# ---------------------------------------------------------------------------


def test_e2e_retry_only_once_second_crash_no_new_process(monkeypatch):
    simulation_id = "sim_t2_retry_once_only"
    _patch_real_spawn(monkeypatch)

    # Simulasikan simulasi yang SUDAH pernah di-retry (retry_count=1) dan
    # proses barunya (dari retry pertama) sudah mati lagi juga.
    retried_proc, retried_created_at = _spawn_dummy_process()
    retried_pid = retried_proc.pid
    retried_proc.kill()
    retried_proc.wait(timeout=5)

    _write_simulation_config(simulation_id)
    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=retried_pid,
        process_started_at=retried_created_at,
        retry_count=1,
        retried_at="2026-09-19T10:00:00",
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )

    processes_before = set(psutil.pids())

    try:
        result = reconciler_module._reconcile_one(simulation_id)
        assert result == "crashed"

        _retry_crashed_simulations([simulation_id])

        state = SimulationRunner._load_run_state(simulation_id)
        assert state is not None
        assert state.runner_status == RunnerStatus.NEEDS_ATTENTION
        # retry_count TETAP 1, bukan naik jadi 2.
        assert state.retry_count == 1

        processes_after = set(psutil.pids())
        new_pids = processes_after - processes_before
        print(
            f"[E2E sekali doang] retry_count={state.retry_count}, "
            f"status={state.runner_status.value}, proses_baru={new_pids}"
        )
        assert new_pids == set(), (
            "NOL ada proses baru yang boleh ke-spawn saat simulasi sudah "
            f"pernah di-retry sekali — tapi ketemu proses baru: {new_pids}"
        )
    finally:
        _cleanup(simulation_id)


def test_retry_eligibility_reason_blocks_retry_count_ge_1():
    state = SimulationRunner._load_run_state
    from app.services.simulation_runner import SimulationRunState

    fake_state = SimulationRunState(
        simulation_id="sim_t2_elig_check",
        runner_status=RunnerStatus.CRASHED,
        retry_count=1,
        start_params={"platform": "twitter", "enable_graph_memory_update": False},
    )
    reason = _retry_eligibility_reason(fake_state)
    assert reason is not None
    assert "sekali" in reason or "retry_count" in reason


# ---------------------------------------------------------------------------
# 3. Urutan kritis: crash PERSIS sesudah retry_count tersimpan tapi SEBELUM
#    proses baru sempat spawn -> restart berikutnya TIDAK retry kedua kali.
# ---------------------------------------------------------------------------


def test_critical_ordering_no_second_retry_if_spawn_fails_after_state_saved(
    monkeypatch,
):
    simulation_id = "sim_t2_critical_ordering"

    # PENTING: spawn proses dummy "lama" SEBELUM subprocess.Popen di-patch --
    # `subprocess` adalah modul singleton, jadi monkeypatch.setattr pada
    # "app.services.simulation_runner.subprocess.Popen" mengubah atribut di
    # modul `subprocess` yang SAMA yang dipakai test ini sendiri. Kalau
    # dipatch duluan, _spawn_dummy_process() di bawah ikut kena patch.
    old_proc, old_created_at = _spawn_dummy_process()
    old_pid = old_proc.pid
    old_proc.kill()
    old_proc.wait(timeout=5)

    def exploding_popen(cmd, **kwargs):
        raise RuntimeError(
            "simulated: proses mati/gagal spawn PERSIS sesudah retry_count "
            "tersimpan ke disk"
        )

    monkeypatch.setattr(
        "app.services.simulation_runner.subprocess.Popen", exploding_popen
    )
    # Popen meledak SEBELUM start_simulation() sempat membuat monitor
    # thread, jadi tidak perlu stub _monitor_simulation di sini.

    _write_simulation_config(simulation_id)
    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=old_pid,
        process_started_at=old_created_at,
        retry_count=0,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )

    try:
        result = reconciler_module._reconcile_one(simulation_id)
        assert result == "crashed"

        # Putaran retry PERTAMA: start_simulation() menulis STARTING (dengan
        # retry_count=1 dibakar ke state) SEBELUM subprocess.Popen()
        # dipanggil -- lalu Popen meledak. Wrapper _retry_one() menangkap
        # exception itu dan memaksa status akhir NEEDS_ATTENTION.
        _retry_crashed_simulations([simulation_id])

        state_after_first_attempt = SimulationRunner._load_run_state(simulation_id)
        assert state_after_first_attempt is not None
        assert state_after_first_attempt.retry_count == 1, (
            "retry_count HARUS sudah tersimpan 1 walau spawn gagal -- ini "
            "yang mencegah retry kedua kalinya."
        )
        assert (
            state_after_first_attempt.runner_status == RunnerStatus.NEEDS_ATTENTION
        )

        # "Restart berikutnya": panggil reconcile lagi. Karena status
        # sekarang NEEDS_ATTENTION (bukan status "live"), _reconcile_one()
        # akan skip (skipped_not_live) -- tidak akan pernah ditandai CRASHED
        # lagi, sehingga TIDAK akan pernah masuk ke retry trigger lagi.
        result_second_pass = reconciler_module._reconcile_one(simulation_id)
        assert result_second_pass == "skipped_not_live"

        # Buktikan eksplisit juga: memanggil retry logic lagi terhadap
        # simulation_id yang sama (seandainya SESUATU salah membuatnya
        # ke-collect lagi) tetap menolak retry kedua karena retry_count>=1.
        popen_call_count = {"n": 0}

        def counting_popen(cmd, **kwargs):
            popen_call_count["n"] += 1
            raise AssertionError("start_simulation() TIDAK BOLEH dipanggil lagi")

        monkeypatch.setattr(
            "app.services.simulation_runner.subprocess.Popen", counting_popen
        )
        _retry_crashed_simulations([simulation_id])
        assert popen_call_count["n"] == 0, (
            "Popen() terpanggil lagi -- berarti retry KEDUA genuinely "
            "dicoba, padahal retry_count sudah 1."
        )

        final_state = SimulationRunner._load_run_state(simulation_id)
        assert final_state.retry_count == 1
        print(
            "[Urutan kritis] retry_count tetap "
            f"{final_state.retry_count} sesudah 2x putaran reconcile, "
            f"Popen() dipanggil ulang: {popen_call_count['n']}x"
        )
    finally:
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 4. Graph sudah dihapus/reset -> LANGSUNG needs_attention, nol retry.
# ---------------------------------------------------------------------------


def test_graph_deleted_skips_retry_goes_needs_attention(monkeypatch):
    simulation_id = "sim_t2_graph_deleted"

    project = ProjectManager.create_project(name="T2 graph-deleted test")
    project.graph_id = "graph_original_abc"
    project.status = ProjectStatus.GRAPH_COMPLETED
    ProjectManager.save_project(project)

    manager = SimulationManager()
    sim = manager.create_simulation(
        project_id=project.project_id, graph_id="graph_original_abc"
    )
    simulation_id = sim.simulation_id

    # Spawn dummy process SEBELUM Popen di-patch (lihat catatan singleton di
    # test_critical_ordering_no_second_retry_if_spawn_fails_after_state_saved).
    old_proc, old_created_at = _spawn_dummy_process()
    old_pid = old_proc.pid
    old_proc.kill()
    old_proc.wait(timeout=5)

    popen_calls = {"n": 0}

    def counting_popen(cmd, **kwargs):
        popen_calls["n"] += 1
        raise AssertionError("start_simulation() TIDAK BOLEH dipanggil kalau graph invalid")

    monkeypatch.setattr(
        "app.services.simulation_runner.subprocess.Popen", counting_popen
    )

    _write_simulation_config(simulation_id)
    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=old_pid,
        process_started_at=old_created_at,
        retry_count=0,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": "graph_original_abc",
            "enable_graph_memory_update": True,
        },
    )

    try:
        # Simulasikan project di-reset: graph_id project berubah (graph lama
        # sudah tidak ada lagi / diganti graph baru).
        project.graph_id = "graph_NEW_after_reset"
        ProjectManager.save_project(project)

        state = SimulationRunner._load_run_state(simulation_id)
        assert _graph_still_valid(state) is False

        result = reconciler_module._reconcile_one(simulation_id)
        assert result == "crashed"

        _retry_crashed_simulations([simulation_id])

        final_state = SimulationRunner._load_run_state(simulation_id)
        assert final_state.runner_status == RunnerStatus.NEEDS_ATTENTION
        assert final_state.retry_count == 0, (
            "retry_count TIDAK boleh naik -- retry tidak pernah genuinely "
            "dicoba untuk kasus graph invalid."
        )
        assert popen_calls["n"] == 0
        print(
            "[Graph dihapus] status="
            f"{final_state.runner_status.value}, retry_count="
            f"{final_state.retry_count}, Popen dipanggil={popen_calls['n']}x"
        )

        # Regresi: project dihapus total (bukan cuma graph_id berubah) juga
        # harus memblokir retry.
        ProjectManager.delete_project(project.project_id)
        assert (
            _graph_still_valid(SimulationRunner._load_run_state(simulation_id))
            is False
        )
    finally:
        _cleanup(simulation_id)
        _cleanup_manager(simulation_id)
        _cleanup_project(project.project_id)


def test_graph_still_valid_true_when_no_graph_memory_used():
    from app.services.simulation_runner import SimulationRunState

    state = SimulationRunState(
        simulation_id="sim_t2_no_graph_memory",
        runner_status=RunnerStatus.CRASHED,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )
    assert _graph_still_valid(state) is True


# ---------------------------------------------------------------------------
# 5. Concurrency limit: banyak CRASHED bersamaan -> cuma
#    _MAX_CONCURRENT_RETRIES yang genuinely retry.
# ---------------------------------------------------------------------------


def test_concurrency_limit_caps_simultaneous_retries(monkeypatch):
    n_simulations = 5
    simulation_ids = [f"sim_t2_concurrency_{i}" for i in range(n_simulations)]

    real_popen = subprocess.Popen
    spawn_lock = threading.Lock()
    spawned_pids = []

    def fake_popen(cmd, **kwargs):
        proc = real_popen(["sleep", "60"], start_new_session=True)
        with spawn_lock:
            spawned_pids.append(proc.pid)
        return proc

    old_pids = []
    try:
        # Spawn+kill SEMUA proses dummy "lama" dulu, SEBELUM subprocess.Popen
        # di-patch (lihat catatan singleton-module di test urutan kritis) --
        # kalau tidak, spawn dummy ikut kehitung sebagai "spawned_pids" dan
        # merusak hitungan concurrency.
        for simulation_id in simulation_ids:
            old_proc, old_created_at = _spawn_dummy_process()
            old_pids.append(old_proc.pid)
            old_proc.kill()
            old_proc.wait(timeout=5)

            _write_simulation_config(simulation_id)
            _write_run_state(
                simulation_id,
                runner_status="running",
                process_pid=old_proc.pid,
                process_started_at=old_created_at,
                retry_count=0,
                start_params={
                    "platform": "twitter",
                    "max_rounds": 5,
                    "graph_id": None,
                    "enable_graph_memory_update": False,
                },
            )
            result = reconciler_module._reconcile_one(simulation_id)
            assert result == "crashed"

        monkeypatch.setattr(
            "app.services.simulation_runner.subprocess.Popen", fake_popen
        )
        monkeypatch.setattr(
            SimulationRunner, "_monitor_simulation", classmethod(_noop_monitor)
        )

        _retry_crashed_simulations(simulation_ids)

        retried = []
        needs_attention = []
        for simulation_id in simulation_ids:
            state = SimulationRunner._load_run_state(simulation_id)
            if state.retry_count == 1:
                retried.append(simulation_id)
            else:
                needs_attention.append(simulation_id)
                assert state.runner_status == RunnerStatus.NEEDS_ATTENTION

        print(
            f"[Concurrency limit] {len(retried)} di-retry genuinely dari "
            f"{n_simulations} CRASHED bersamaan (batas={_MAX_CONCURRENT_RETRIES}); "
            f"sisanya needs_attention={len(needs_attention)}"
        )
        assert len(retried) == _MAX_CONCURRENT_RETRIES
        assert len(needs_attention) == n_simulations - _MAX_CONCURRENT_RETRIES
        assert len(spawned_pids) == _MAX_CONCURRENT_RETRIES

        for pid in spawned_pids:
            try:
                p = psutil.Process(pid)
                p.kill()
                p.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
    finally:
        for simulation_id in simulation_ids:
            _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 6. cleanup_simulation_logs() beneran terpanggil sebelum spawn.
# ---------------------------------------------------------------------------


def test_cleanup_simulation_logs_called_before_retry_spawn(monkeypatch):
    simulation_id = "sim_t2_cleanup_before_retry"
    _patch_real_spawn(monkeypatch)

    old_proc, old_created_at = _spawn_dummy_process()
    old_pid = old_proc.pid
    old_proc.kill()
    old_proc.wait(timeout=5)

    _write_simulation_config(simulation_id)
    _write_stale_logs(simulation_id)
    stale_actions_path = os.path.join(
        _sim_dir(simulation_id), "twitter", "actions.jsonl"
    )
    stale_db_path = os.path.join(_sim_dir(simulation_id), "twitter_simulation.db")
    assert os.path.exists(stale_actions_path)
    assert os.path.exists(stale_db_path)

    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=old_pid,
        process_started_at=old_created_at,
        retry_count=0,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )

    real_cleanup = SimulationRunner.cleanup_simulation_logs
    cleanup_calls = []

    def spy_cleanup(sim_id):
        cleanup_calls.append(sim_id)
        return real_cleanup(sim_id)

    monkeypatch.setattr(
        SimulationRunner, "cleanup_simulation_logs", classmethod(lambda cls, sid: spy_cleanup(sid))
    )

    try:
        result = reconciler_module._reconcile_one(simulation_id)
        assert result == "crashed"

        _retry_crashed_simulations([simulation_id])

        assert cleanup_calls == [simulation_id]

        state = SimulationRunner._load_run_state(simulation_id)
        assert state.retry_count == 1

        # actions.jsonl lama harus sudah kehapus (cleanup jalan sebelum
        # retry) -- kalau masih ada isinya, berarti spawn baru bisa baca
        # ulang data lama dan bikin angka report dobel.
        assert not os.path.exists(stale_actions_path), (
            "actions.jsonl lama HARUS sudah dihapus sebelum retry spawn"
        )
        assert not os.path.exists(stale_db_path), (
            "twitter_simulation.db lama HARUS sudah dihapus sebelum retry spawn"
        )

        new_pid = state.process_pid
        print(
            f"[Cleanup sebelum retry] cleanup_calls={cleanup_calls}, "
            f"stale_actions_masih_ada={os.path.exists(stale_actions_path)}, "
            f"PID_baru={new_pid}"
        )
        new_proc = psutil.Process(new_pid)
        new_proc.kill()
        new_proc.wait(timeout=5)
    finally:
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 7. reconcile_on_startup() dispatch retry ke background thread -- TIDAK
#    blocking.
# ---------------------------------------------------------------------------


def test_reconcile_on_startup_dispatches_retry_in_background_non_blocking(
    monkeypatch,
):
    simulation_id = "sim_t2_non_blocking_dispatch"

    release_worker = threading.Event()
    worker_started = threading.Event()

    def slow_retry(simulation_ids):
        worker_started.set()
        # Simulasikan retry yang lama (mis. spawn proses berat) -- kalau
        # reconcile_on_startup() menunggu ini selesai, test akan timeout.
        release_worker.wait(timeout=5)

    monkeypatch.setattr(
        reconciler_module, "_retry_crashed_simulations", slow_retry
    )

    old_proc, old_created_at = _spawn_dummy_process()
    old_pid = old_proc.pid
    old_proc.kill()
    old_proc.wait(timeout=5)

    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=old_pid,
        process_started_at=old_created_at,
        retry_count=0,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )

    try:
        start = time.monotonic()
        reconcile_on_startup()
        elapsed = time.monotonic() - start

        print(
            f"[Non-blocking dispatch] reconcile_on_startup() selesai dalam "
            f"{elapsed:.3f}s walau worker retry masih ditahan (belum di-release)."
        )
        # reconcile_on_startup() HARUS sudah return jauh sebelum worker
        # retry (yang sengaja ditahan 5 detik) selesai.
        assert elapsed < 3.0

        # Worker background HARUS benar-benar terpanggil (dispatch berhasil).
        assert worker_started.wait(timeout=2.0)

        state = SimulationRunner._load_run_state(simulation_id)
        assert state.runner_status == RunnerStatus.CRASHED
    finally:
        release_worker.set()
        time.sleep(0.1)
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 8. _retry_one: pengaman try/except paling luar (putaran fix ke-2, temuan
#    HIGH). Exception di titik LAIN -- bukan cleanup_simulation_logs() atau
#    start_simulation() -- harus ke-log lewat `logger.exception(...)` dan
#    TIDAK BOLEH lolos ke luar fungsi / ke threading.excepthook default.
# ---------------------------------------------------------------------------


def test_retry_one_exception_outside_known_points_is_logged_and_does_not_escape(
    monkeypatch,
):
    """Titik yang di-mock: _mark_needs_attention() sendiri melempar OSError
    (mis. disk-full saat _save_run_state di dalamnya) -- persis skenario
    yang disebutkan di temuan review. Exception ini muncul DI DALAM blok
    except cleanup_simulation_logs (bukan di cleanup_simulation_logs atau
    start_simulation itu sendiri), jadi cuma tertangkap oleh pengaman
    try/except paling luar yang baru ditambahkan."""
    simulation_id = "sim_t2_retry_one_unexpected_exception"

    _write_simulation_config(simulation_id)
    _write_run_state(
        simulation_id,
        runner_status="crashed",
        retry_count=0,
        start_params={
            "platform": "twitter",
            "max_rounds": 5,
            "graph_id": None,
            "enable_graph_memory_update": False,
        },
    )

    def boom_cleanup(cls, sid):
        raise RuntimeError("cleanup meledak di titik yang TIDAK biasa")

    def boom_mark_needs_attention(*args, **kwargs):
        raise OSError("simulated disk-full saat _mark_needs_attention")

    monkeypatch.setattr(
        SimulationRunner, "cleanup_simulation_logs", classmethod(boom_cleanup)
    )
    monkeypatch.setattr(
        reconciler_module, "_mark_needs_attention", boom_mark_needs_attention
    )

    fake_logger = mock.MagicMock()
    monkeypatch.setattr(reconciler_module, "logger", fake_logger)

    # Custom excepthook supaya kita BISA membuktikan tidak ada exception
    # tak tertangani yang lolos ke level thread (yang jadi masalah SEBELUM
    # fix -- default excepthook cuma cetak ke stderr).
    escaped_exceptions = []
    original_excepthook = threading.excepthook

    def capturing_excepthook(args):
        escaped_exceptions.append(args)

    threading.excepthook = capturing_excepthook
    try:
        t = threading.Thread(
            target=reconciler_module._retry_one, args=(simulation_id,)
        )
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), "_retry_one tidak boleh menggantung"
        assert escaped_exceptions == [], (
            "exception TIDAK BOLEH lolos ke threading.excepthook -- harus "
            f"tertangkap di dalam _retry_one sendiri: {escaped_exceptions}"
        )

        # logger.exception (BUKAN print/stderr) harus genuinely terpanggil
        # -- minimal dua kali: sekali untuk RuntimeError dari cleanup (via
        # jalur except internal yang sudah ada), sekali lagi untuk OSError
        # dari _mark_needs_attention yang lolos ke pengaman terluar.
        assert fake_logger.exception.call_count >= 2, (
            f"logger.exception harusnya terpanggil untuk kedua exception, "
            f"cuma kepanggil {fake_logger.exception.call_count}x"
        )
        logged_messages = " ".join(
            str(call.args[0]) if call.args else ""
            for call in fake_logger.exception.call_args_list
        )
        assert simulation_id in logged_messages or any(
            simulation_id in str(a) for call in fake_logger.exception.call_args_list for a in call.args
        )
    finally:
        threading.excepthook = original_excepthook
        _cleanup(simulation_id)

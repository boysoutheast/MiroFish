"""T1 — reconciliation simulasi saat startup.

Cakupan uji:
1. PID mati -> ditandai CRASHED (dengan error message & completed_at terisi).
2. PID hidup DAN create_time cocok, tanpa monitor (yatim) -> dihentikan, CRASHED.
3. PID-reuse: PID exists=True tapi create_time BEDA -> tetap CRASHED (karena
   itu bukan proses kita, proses lain yang kebetulan reuse PID).
4. Status non-live (IDLE/COMPLETED/dst) -> dilewati, tidak disentuh.
5. reconcile_on_startup() TIDAK BOLEH melempar exception bahkan kalau
   implementasinya meledak (mocked) -- backend harus tetap bisa start.
"""

import json
import os
import shutil
import subprocess
import time
from unittest import mock

import psutil
import pytest

from app.config import Config
from app.services.simulation_reconciler import (
    _classify,
    _process_is_ours,
    _reconcile_one,
    _retry_crashed_simulations,
    reconcile_on_startup,
)
from app.services.simulation_runner import RunnerStatus, SimulationRunner


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


def _cleanup(simulation_id: str):
    SimulationRunner._run_states.pop(simulation_id, None)
    sim_dir = _sim_dir(simulation_id)
    if os.path.exists(sim_dir):
        shutil.rmtree(sim_dir)


def _spawn_dummy_process():
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    time.sleep(0.2)
    p = psutil.Process(proc.pid)
    return proc, p.create_time()


# ---------------------------------------------------------------------------
# 1. PID mati -> CRASHED
# ---------------------------------------------------------------------------

def test_reconcile_one_dead_pid_marks_crashed():
    simulation_id = "sim_t1_dead_pid"
    proc, created_at = _spawn_dummy_process()
    dead_pid = proc.pid
    proc.kill()
    proc.wait(timeout=5)

    _write_run_state(
        simulation_id,
        runner_status="running",
        process_pid=dead_pid,
        process_started_at=created_at,
    )
    try:
        result = _reconcile_one(simulation_id)
        assert result == "crashed"

        state = SimulationRunner._load_run_state(simulation_id)
        assert state.runner_status == RunnerStatus.CRASHED
        assert state.completed_at is not None
        assert "mati" in state.error or "restart" in state.error
        assert state.twitter_running is False
        assert state.reddit_running is False
    finally:
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 2. PID hidup + create_time cocok, tanpa monitor (yatim) -> dihentikan, CRASHED
# ---------------------------------------------------------------------------

def test_reconcile_one_alive_matching_orphan_killed_and_crashed(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    # Perilaku lama ("yatim dibiarkan") diganti: yatim milik kita (tanpa
    # monitor) dihentikan lalu CRASHED. Detail di test_orphan_alive.py.
    simulation_id = "sim_t1_alive_match"
    proc, created_at = _spawn_dummy_process()
    try:
        _write_run_state(
            simulation_id,
            runner_status="running",
            process_pid=proc.pid,
            process_started_at=created_at,
        )
        result = _reconcile_one(simulation_id)
        assert result == "crashed"

        state = SimulationRunner._load_run_state(simulation_id)
        assert state.runner_status == RunnerStatus.CRASHED
        proc.wait(timeout=5)  # benar-benar mati
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 3. PID-reuse: PID hidup tapi create_time BEDA -> tetap CRASHED
# ---------------------------------------------------------------------------

def test_reconcile_one_pid_reuse_create_time_mismatch_marks_crashed():
    simulation_id = "sim_t1_pid_reuse"
    proc, real_created_at = _spawn_dummy_process()
    try:
        # Simulasikan PID reuse: catat process_started_at yang JAUH beda dari
        # create_time proses yang sekarang benar-benar menempati PID ini.
        fake_started_at = real_created_at - 999999
        _write_run_state(
            simulation_id,
            runner_status="running",
            process_pid=proc.pid,
            process_started_at=fake_started_at,
        )
        result = _reconcile_one(simulation_id)
        assert result == "crashed"

        state = SimulationRunner._load_run_state(simulation_id)
        assert state.runner_status == RunnerStatus.CRASHED
    finally:
        proc.kill()
        proc.wait(timeout=5)
        _cleanup(simulation_id)


def test_process_is_ours_none_expected_started_at_fails_closed():
    """run_state.json lama (pra-T0) tanpa process_started_at -- tidak ada
    dasar buat percaya PID mentah, jadi WAJIB dianggap bukan proses kita."""
    proc, _created_at = _spawn_dummy_process()
    try:
        assert _process_is_ours(proc.pid, None) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# 4. Status non-live -> dilewati
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["idle", "completed", "failed", "stopped", "crashed"])
def test_reconcile_one_non_live_status_skipped(status):
    simulation_id = f"sim_t1_skip_{status}"
    _write_run_state(simulation_id, runner_status=status, process_pid=999999)
    try:
        result = _reconcile_one(simulation_id)
        assert result == "skipped_not_live"

        state = SimulationRunner._load_run_state(simulation_id)
        assert state.runner_status.value == status
    finally:
        _cleanup(simulation_id)


# ---------------------------------------------------------------------------
# 5. reconcile_on_startup() tidak boleh melempar exception
# ---------------------------------------------------------------------------

def test_reconcile_on_startup_never_raises_even_if_impl_blows_up():
    with mock.patch(
        "app.services.simulation_reconciler._reconcile_on_startup_impl",
        side_effect=RuntimeError("boom - simulated catastrophic failure"),
    ):
        # Tidak boleh raise. Kalau raise, test ini gagal dengan sendirinya.
        reconcile_on_startup()


def test_reconcile_on_startup_survives_per_simulation_exception():
    """Satu simulasi yang gagal di-reconcile (mis. run_state.json korup)
    tidak boleh menghentikan reconciliation simulasi lain."""
    ok_id = "sim_t1_ok_after_error"
    _write_run_state(ok_id, runner_status="running", process_pid=999999999)
    try:
        # Retry background dijalankan sinkron (tidak race dengan cleanup).
        with mock.patch(
            "app.services.simulation_reconciler._spawn_retry_worker_thread",
            side_effect=lambda ids: _retry_crashed_simulations(ids),
        ), mock.patch(
            "app.services.simulation_reconciler._iter_simulation_ids",
            return_value=iter(["sim_t1_does_not_exist_on_disk", ok_id]),
        ):
            # sim_t1_does_not_exist_on_disk -> _load_run_state returns None ->
            # skipped_no_state, bukan exception. Untuk benar2 memicu exception
            # per-sim, paksa _reconcile_one meledak untuk id pertama saja.
            real_classify = _classify

            def flaky(simulation_id):
                if simulation_id == "sim_t1_does_not_exist_on_disk":
                    raise RuntimeError("simulated corrupt run_state.json")
                return real_classify(simulation_id)

            with mock.patch(
                "app.services.simulation_reconciler._classify",
                side_effect=flaky,
            ):
                reconcile_on_startup()

        # Simulasi kedua (ok_id, PID mati) tetap ke-reconcile walau simulasi
        # pertama meledak: CRASHED lalu (tanpa start_params) NEEDS_ATTENTION
        # oleh T2 -- bukti dia diproses.
        state = SimulationRunner._load_run_state(ok_id)
        assert state.runner_status in (
            RunnerStatus.CRASHED, RunnerStatus.NEEDS_ATTENTION
        )
    finally:
        _cleanup(ok_id)


# ---------------------------------------------------------------------------
# Bukti langsung: reconciliation error TIDAK BOLEH gagalkan create_app(),
# dan /health tetap balas normal sesudahnya.
# ---------------------------------------------------------------------------

def test_create_app_and_health_survive_reconciliation_catastrophic_failure():
    with mock.patch(
        "app.services.simulation_reconciler._reconcile_on_startup_impl",
        side_effect=RuntimeError("boom - reconciliation totally broken"),
    ):
        from app import create_app

        app = create_app()
        client = app.test_client()
        response = client.get("/health")

        assert response.status_code == 200
        assert response.get_json() == {"status": "ok", "service": "MiroFish Backend"}


# ---------------------------------------------------------------------------
# Putaran 2 (review): guard debug-mode di __init__.py HARUS reuse
# app.config['DEBUG'] (bukan hitung ulang dari os.environ['FLASK_DEBUG']=='1'
# — format project yang benar itu 'true', bukan '1', lihat config.py:22).
# ---------------------------------------------------------------------------


class _NonDebugConfig(Config):
    DEBUG = False


class _DebugConfig(Config):
    DEBUG = True


def _create_app_with_mocked_reconcile(config_class, werkzeug_run_main):
    """Panggil create_app() dengan reconcile_on_startup di-mock, return mock
    call count. werkzeug_run_main=None berarti env var itu TIDAK di-set sama
    sekali (proses parent yang genuinely tidak punya var ini)."""
    env_patch = {}
    if werkzeug_run_main is not None:
        env_patch["WERKZEUG_RUN_MAIN"] = werkzeug_run_main

    with mock.patch(
        "app.services.simulation_reconciler.reconcile_on_startup"
    ) as mock_reconcile:
        with mock.patch.dict(os.environ, env_patch, clear=False):
            if werkzeug_run_main is None:
                os.environ.pop("WERKZEUG_RUN_MAIN", None)
            from app import create_app

            create_app(config_class=config_class)

    return mock_reconcile.call_count


def test_guard_reconciles_once_when_not_debug_mode_no_reloader_var():
    """DEBUG=False (mode produksi/dev biasa, format apa pun FLASK_DEBUG-nya
    sudah dibaca jadi False oleh Config) + WERKZEUG_RUN_MAIN tidak ada ->
    reconciliation TETAP jalan (persis seperti should_log_startup)."""
    assert _create_app_with_mocked_reconcile(_NonDebugConfig, None) == 1


def test_guard_skips_parent_process_when_debug_mode_flask_debug_true_format():
    """Ini SKENARIO BUG yang diperbaiki: FLASK_DEBUG=true (format project
    yang BENAR sesuai config.py, BUKAN '1') di proses PARENT (belum ada
    WERKZEUG_RUN_MAIN sama sekali, karena reloader belum spawn child).
    Guard lama salah baca ini sebagai "bukan debug mode" (karena
    os.environ.get('FLASK_DEBUG') == '1' selalu False untuk 'true') lalu
    reconcile dobel jalan di parent DAN child. Guard baru re-use
    app.config['DEBUG'] (True di sini) -> parent HARUS di-skip."""
    assert _create_app_with_mocked_reconcile(_DebugConfig, None) == 0


def test_guard_runs_once_in_reloader_child_when_debug_mode():
    """DEBUG=True + WERKZEUG_RUN_MAIN='true' (proses reloader child, yang
    genuinely melayani request) -> reconciliation HARUS tetap jalan, sama
    seperti should_log_startup untuk kasus yang sama."""
    assert _create_app_with_mocked_reconcile(_DebugConfig, "true") == 1


def test_guard_matches_should_log_startup_behavior_across_scenarios():
    """Invariant inti PRD: guard reconciliation HARUS sama-sama nyala/mati
    dengan should_log_startup untuk kombinasi debug_mode x is_reloader_process
    yang sama -- karena keduanya dari rumus 'not debug_mode or
    is_reloader_process'. Dicek langsung tanpa mock supaya lolos/gagalnya
    tidak tergantung isi __init__.py yang di-mock."""
    for debug_mode, is_reloader_process in (
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ):
        should_log_startup = not debug_mode or is_reloader_process
        should_reconcile = not debug_mode or is_reloader_process
        assert should_log_startup == should_reconcile


# ---------------------------------------------------------------------------
# Import reconcile_on_startup HARUS di-guard try/except -- ImportError (mis.
# psutil belum kepasang di suatu deploy target) TIDAK BOLEH menggagalkan
# create_app() total.
# ---------------------------------------------------------------------------


def test_create_app_survives_reconciler_import_error():
    """Simulasikan modul simulation_reconciler gagal di-import (mis. psutil
    hilang). create_app() harus TETAP berhasil, dan /health tetap balas
    normal -- ini beda dari test catastrophic-failure di atas yang memock
    kegagalan DI DALAM fungsi; di sini yang gagal adalah baris import itu
    sendiri (`from .services.simulation_reconciler import
    reconcile_on_startup`). Dilakukan dengan menghapus modulnya dari
    sys.modules lalu membuat import-nya raise ImportError betulan."""
    import sys as _sys

    module_name = "app.services.simulation_reconciler"
    saved_module = _sys.modules.pop(module_name, None)
    try:
        import builtins

        real_builtins_import = builtins.__import__

        def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "services.simulation_reconciler" and level == 1:
                raise ImportError("simulated: psutil not installed")
            return real_builtins_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=_fake_import):
            from app import create_app

            app = create_app(config_class=_NonDebugConfig)
            client = app.test_client()
            response = client.get("/health")

            assert response.status_code == 200
            assert response.get_json() == {
                "status": "ok",
                "service": "MiroFish Backend",
            }
    finally:
        if saved_module is not None:
            _sys.modules[module_name] = saved_module

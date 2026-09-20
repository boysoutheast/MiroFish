"""Yatim hidup (pid hidup + create_time cocok + status live + tanpa monitor
thread) -> dihentikan aman lalu diperlakukan seperti proses mati (CRASHED ->
auto-retry T2 sekali). Proses OS nyata, bukan tiruan (kecuali skenario gagal
bunuh)."""

import fcntl
import json
import os
import shutil
import subprocess
import threading
import time
from unittest import mock

import psutil
import pytest

from app.services import simulation_reconciler as rec
from app.services.simulation_reconciler import (
    _reconcile_one,
    _retry_crashed_simulations,
    reconcile_on_startup,
)
from app.services.simulation_runner import RunnerStatus, SimulationRunner

PARAMS = {
    "platform": "twitter",
    "max_rounds": 5,
    "graph_id": None,
    "enable_graph_memory_update": False,
}


@pytest.fixture(autouse=True)
def _tmp_run_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "_ORPHAN_KILL_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(rec, "_ORPHAN_KILL_WAIT_SECONDS", 1.0)
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)


def _sim_dir(sid):
    return os.path.join(SimulationRunner.RUN_STATE_DIR, sid)


def _write_state(sid, **overrides):
    os.makedirs(_sim_dir(sid), exist_ok=True)
    data = {
        "simulation_id": sid, "runner_status": "running", "current_round": 1,
        "total_rounds": 10, "simulated_hours": 1, "total_simulation_hours": 10,
        "twitter_current_round": 1, "reddit_current_round": 1,
        "twitter_simulated_hours": 1, "reddit_simulated_hours": 1,
        "twitter_running": True, "reddit_running": True,
        "twitter_completed": False, "reddit_completed": False,
        "twitter_actions_count": 0, "reddit_actions_count": 0,
        "started_at": "2026-09-19T10:00:00", "updated_at": "2026-09-19T10:00:00",
        "completed_at": None, "error": None, "process_pid": None,
        "process_started_at": None, "retry_count": 0, "retried_at": None,
        "start_params": None, "ingestion_incomplete": False,
        "ingestion_incomplete_detail": None, "recent_actions": [],
    }
    data.update(overrides)
    with open(os.path.join(_sim_dir(sid), "run_state.json"), "w") as f:
        json.dump(data, f)
    with open(os.path.join(_sim_dir(sid), "simulation_config.json"), "w") as f:
        json.dump({"time_config": {"total_simulation_hours": 10,
                                   "minutes_per_round": 60}}, f)


def _spawn():
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    time.sleep(0.2)
    return proc, psutil.Process(proc.pid).create_time()


def _cleanup(sid):
    for d in (SimulationRunner._run_states, SimulationRunner._processes,
              SimulationRunner._monitor_threads, SimulationRunner._action_queues):
        d.pop(sid, None)
    SimulationRunner._stdout_files.pop(sid, None)
    SimulationRunner._stderr_files.pop(sid, None)
    shutil.rmtree(_sim_dir(sid), ignore_errors=True)


def _alive(proc):
    return proc.poll() is None


def _patch_spawn(monkeypatch, spawned):
    real = subprocess.Popen

    def fake(cmd, **kw):
        p = real(["sleep", "60"], start_new_session=True)
        spawned.append(p)
        return p

    monkeypatch.setattr("app.services.simulation_runner.subprocess.Popen", fake)
    monkeypatch.setattr(SimulationRunner, "_monitor_simulation",
                        classmethod(lambda cls, sid, locale="zh": None))


def _kill_all(procs):
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_orphan_killed_crashed_and_retried_once(monkeypatch):
    sid = "sim_orphan_retry"
    spawned = []
    old, started = _spawn()
    _patch_spawn(monkeypatch, spawned)
    _write_state(sid, process_pid=old.pid, process_started_at=started,
                 start_params=PARAMS)
    try:
        assert _reconcile_one(sid) == "crashed"
        assert old.wait(timeout=5) is not None  # OS: proses lama mati
        assert not psutil.pid_exists(old.pid) or \
            psutil.Process(old.pid).status() == psutil.STATUS_ZOMBIE
        assert SimulationRunner._load_run_state(sid).runner_status == RunnerStatus.CRASHED

        _retry_crashed_simulations([sid])
        st = SimulationRunner._load_run_state(sid)
        assert st.retry_count == 1
        assert st.runner_status == RunnerStatus.RUNNING
        assert st.process_pid != old.pid and len(spawned) == 1
        assert psutil.pid_exists(st.process_pid)
    finally:
        _kill_all([old] + spawned)
        _cleanup(sid)


def test_pid_reuse_not_killed():
    sid = "sim_orphan_reuse"
    proc, started = _spawn()
    _write_state(sid, process_pid=proc.pid, process_started_at=started - 99999)
    try:
        assert _reconcile_one(sid) == "crashed"
        assert _alive(proc)  # proses lain, tidak dibunuh
    finally:
        _kill_all([proc])
        _cleanup(sid)


def test_kill_failure_needs_attention_no_retry(monkeypatch):
    sid = "sim_orphan_killfail"
    spawned = []
    proc, started = _spawn()
    _patch_spawn(monkeypatch, spawned)
    _write_state(sid, process_pid=proc.pid, process_started_at=started,
                 start_params=PARAMS)
    monkeypatch.setattr(rec, "_signal_orphan",
                        mock.Mock(side_effect=psutil.AccessDenied(proc.pid)))
    spawn_thread = mock.Mock()
    monkeypatch.setattr(rec, "_spawn_retry_worker_thread", spawn_thread)
    try:
        reconcile_on_startup()
        st = SimulationRunner._load_run_state(sid)
        assert st.runner_status == RunnerStatus.NEEDS_ATTENTION
        assert "gagal dihentikan" in st.error
        assert str(proc.pid) in st.error and "kill" in st.error
        assert _alive(proc)
        for call in spawn_thread.call_args_list:
            assert sid not in call.args[0]
        assert spawned == []
    finally:
        _kill_all([proc] + spawned)
        _cleanup(sid)


def test_protected_pid_refused_no_signal(monkeypatch):
    sid = "sim_orphan_self"
    me = psutil.Process(os.getpid()).create_time()
    _write_state(sid, process_pid=os.getpid(), process_started_at=me,
                 start_params=PARAMS)
    sig = mock.Mock()
    monkeypatch.setattr(rec, "_signal_orphan", sig)
    try:
        assert _reconcile_one(sid) == "needs_attention"
        sig.assert_not_called()
        st = SimulationRunner._load_run_state(sid)
        assert str(os.getpid()) in st.error
    finally:
        _cleanup(sid)


def test_protected_pid_predicate():
    assert rec._is_protected_pid(os.getpid())
    assert rec._is_protected_pid(os.getppid())
    proc, _ = _spawn()  # sesi/grup baru
    try:
        assert not rec._is_protected_pid(proc.pid)
    finally:
        _kill_all([proc])


def test_non_leader_children_killed():
    sid = "sim_orphan_children"
    # bash non-leader (satu grup dengan test) + anak `sleep`
    parent = subprocess.Popen(["bash", "-c", "sleep 60 & wait"])
    time.sleep(0.4)
    started = psutil.Process(parent.pid).create_time()
    kids = psutil.Process(parent.pid).children(recursive=True)
    assert kids
    assert os.getpgid(parent.pid) == os.getpgrp()  # grup sama -> ditolak guard
    try:
        # jalur non-leader tanpa guard grup: paksa guard lolos
        with mock.patch.object(rec, "_is_protected_pid",
                               side_effect=lambda p: p == os.getpid()
                               or p == os.getppid()):
            with mock.patch.object(rec, "_is_group_leader", return_value=False):
                # killpg tidak dipakai (bukan leader) -> send_signal + children
                assert rec._terminate_orphan(parent.pid, started) is True
        parent.wait(timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(k.is_running() and k.status() != psutil.STATUS_ZOMBIE for k in kids):
            time.sleep(0.1)
        assert all((not k.is_running()) or k.status() == psutil.STATUS_ZOMBIE
                   for k in kids)
    finally:
        for k in kids:
            try:
                k.kill()
            except psutil.NoSuchProcess:
                pass
        _kill_all([parent])


def test_multiple_orphans_share_total_deadline(monkeypatch):
    """Dua yatim keras-kepala: total waktu ~ 1 grace + kill-wait, bukan 2x."""
    monkeypatch.setattr(rec, "_ORPHAN_KILL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(rec, "_ORPHAN_KILL_WAIT_SECONDS", 0.5)
    procs = []
    items = []
    for _ in range(3):
        p = subprocess.Popen(
            ["bash", "-c", "trap '' TERM; while true; do sleep 1; done"],
            start_new_session=True)
        procs.append(p)
    time.sleep(0.4)
    for p in procs:
        items.append((p.pid, psutil.Process(p.pid).create_time()))
    try:
        t0 = time.monotonic()
        res = rec._terminate_orphans(items)
        elapsed = time.monotonic() - t0
        assert all(res.values())
        assert elapsed < 0.5 + 0.5 + 1.0  # jauh < 3 x 0.5 serial + wait
        assert all(p.wait(timeout=5) is not None for p in procs)
    finally:
        _kill_all(procs)


def test_startup_skipped_in_reloader_parent(monkeypatch):
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "false")
    impl = mock.Mock()
    monkeypatch.setattr(rec, "_reconcile_on_startup_impl", impl)
    reconcile_on_startup()
    impl.assert_not_called()


def test_startup_runs_in_reloader_child(monkeypatch):
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    impl = mock.Mock()
    monkeypatch.setattr(rec, "_reconcile_on_startup_impl", impl)
    reconcile_on_startup()
    impl.assert_called_once()


def test_startup_skipped_when_lock_held(monkeypatch):
    impl = mock.Mock()
    monkeypatch.setattr(rec, "_reconcile_on_startup_impl", impl)
    lock_path = os.path.join(SimulationRunner.RUN_STATE_DIR, rec._LOCK_FILENAME)
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reconcile_on_startup()
        impl.assert_not_called()
    reconcile_on_startup()  # lock dilepas -> jalan
    impl.assert_called_once()


def test_still_alive_after_sigkill_needs_attention(monkeypatch):
    sid = "sim_orphan_stubborn"
    proc, started = _spawn()
    _write_state(sid, process_pid=proc.pid, process_started_at=started,
                 start_params=PARAMS)
    monkeypatch.setattr(rec, "_signal_orphan", lambda *a, **k: None)
    monkeypatch.setattr(rec, "_ORPHAN_KILL_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(rec, "_ORPHAN_KILL_WAIT_SECONDS", 0.3)
    try:
        assert _reconcile_one(sid) == "needs_attention"
        assert _alive(proc)
    finally:
        _kill_all([proc])
        _cleanup(sid)


def test_registered_monitor_thread_untouched():
    sid = "sim_orphan_monitored"
    proc, started = _spawn()
    _write_state(sid, process_pid=proc.pid, process_started_at=started,
                 start_params=PARAMS)
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    SimulationRunner._monitor_threads[sid] = t
    try:
        assert _reconcile_one(sid) == "monitored_alive"
        assert _alive(proc)
        assert SimulationRunner._load_run_state(sid).runner_status == RunnerStatus.RUNNING
    finally:
        stop.set()
        _kill_all([proc])
        _cleanup(sid)


def test_orphan_with_retry_count_1_needs_attention_no_spawn(monkeypatch):
    sid = "sim_orphan_retry1"
    spawned = []
    old, started = _spawn()
    _patch_spawn(monkeypatch, spawned)
    _write_state(sid, process_pid=old.pid, process_started_at=started,
                 retry_count=1, start_params=PARAMS)
    try:
        assert _reconcile_one(sid) == "crashed"
        assert old.wait(timeout=5) is not None
        _retry_crashed_simulations([sid])
        st = SimulationRunner._load_run_state(sid)
        assert st.runner_status == RunnerStatus.NEEDS_ATTENTION
        assert st.retry_count == 1 and spawned == []
    finally:
        _kill_all([old] + spawned)
        _cleanup(sid)


def test_exception_midway_startup_survives():
    sid = "sim_orphan_exc"
    proc, started = _spawn()
    _write_state(sid, process_pid=proc.pid, process_started_at=started)
    try:
        with mock.patch.object(rec, "_terminate_orphans", side_effect=RuntimeError("boom")):
            reconcile_on_startup()  # tidak boleh melempar
        with mock.patch.object(rec, "_iter_simulation_ids", side_effect=RuntimeError("x")):
            reconcile_on_startup()
    finally:
        _kill_all([proc])
        _cleanup(sid)

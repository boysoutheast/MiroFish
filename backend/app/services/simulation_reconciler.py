"""
T1 — reconciliation simulasi saat startup backend.

Bagian dari PRD "jaminan bayar-per-project" MiroFish. T0 (sudah merge) sudah
menambahkan fondasi yang dipakai di sini: RunnerStatus.CRASHED/
NEEDS_ATTENTION, dan field process_started_at/retry_count/retried_at/
start_params di SimulationRunState. Modul ini TIDAK menambah skema baru,
cuma memakainya.

Bug nyata yang ditutup: simulasi bisa nyangkut status STARTING/RUNNING/
PAUSED/STOPPING di run_state.json padahal proses OS-nya sudah mati (server
restart/crash di tengah simulasi jalan) — backend sebelumnya nggak pernah
cek ulang PID pas nyala lagi, jadi run_state itu "running" selamanya di UI.

SENGAJA di luar scope (itu T2 — auto-retry):
- Modul ini HANYA menandai CRASHED. Nggak pernah memanggil
  start_simulation() atau logic apa pun yang menjalankan ulang simulasi.
"""

import glob
import os
from typing import Optional

import psutil

from ..utils.logger import get_logger
from .simulation_runner import RunnerStatus, SimulationRunner

logger = get_logger("mirofish.simulation_reconciler")

# Toleransi selisih waktu (detik) antara process_started_at yang kita catat
# saat spawn dan psutil.Process(pid).create_time() yang dibaca ulang sekarang.
# Timestamp float bisa kena rounding kecil di kedua sisi — bukan berarti PID
# sudah dipakai ulang proses lain.
_CREATE_TIME_TOLERANCE_SECONDS = 1.0

# Status yang "harusnya masih ada proses hidup" di baliknya.
_LIVE_STATUSES = frozenset(
    {
        RunnerStatus.STARTING,
        RunnerStatus.RUNNING,
        RunnerStatus.PAUSED,
        RunnerStatus.STOPPING,
    }
)


def _iter_simulation_ids():
    """Enumerasi simulation_id dari tiap run_state.json di RUN_STATE_DIR.

    Dibaca langsung dari disk (bukan dari cache in-memory SimulationRunner)
    karena reconciliation ini jalan di startup, sebelum ada apa pun di-load
    ke memori.
    """
    base_dir = SimulationRunner.RUN_STATE_DIR
    if not os.path.isdir(base_dir):
        return
    pattern = os.path.join(base_dir, "*", "run_state.json")
    for state_path in glob.glob(pattern):
        simulation_id = os.path.basename(os.path.dirname(state_path))
        yield simulation_id


def _process_is_ours(pid: Optional[int], expected_started_at: Optional[float]) -> bool:
    """True hanya kalau `pid` benar-benar hidup DAN itu proses yang KITA
    spawn (bukan PID yang sudah dipakai ulang OS untuk proses lain sesudah
    restart).

    PID hidup doang TIDAK CUKUP — OS bebas mendaur ulang PID begitu proses
    lama keluar. create_time() yang dicatat saat spawn dicocokkan ke
    create_time() proses yang sekarang menempati PID itu; kalau beda berarti
    itu proses lain, bukan proses kita.
    """
    if pid is None:
        return False

    try:
        if not psutil.pid_exists(pid):
            return False
        actual_started_at = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False

    if expected_started_at is None:
        # run_state.json lama (pra-T0) tidak punya process_started_at sama
        # sekali — tidak ada dasar buat membuktikan PID ini benar proses
        # kita. Fail closed: anggap BUKAN proses kita (ikut ditandai
        # CRASHED), daripada diam-diam percaya PID mentah.
        return False

    return abs(actual_started_at - expected_started_at) < _CREATE_TIME_TOLERANCE_SECONDS


def _reconcile_one(simulation_id: str) -> str:
    """Reconcile satu simulasi. Return salah satu:
    'crashed' | 'orphan_alive' | 'skipped_no_state' | 'skipped_not_live'.
    """
    state = SimulationRunner._load_run_state(simulation_id)
    if state is None:
        return "skipped_no_state"

    if state.runner_status not in _LIVE_STATUSES:
        return "skipped_not_live"

    if _process_is_ours(state.process_pid, state.process_started_at):
        # Proses beneran masih hidup, tapi backend yang baru nyala ini tidak
        # punya monitor thread buat dia (thread lama mati bareng proses
        # lama). Ini "yatim" — dibiarkan jalan sendiri sampai selesai/gagal
        # natural. Re-attach monitor ada di luar scope T1.
        logger.warning(
            "Simulasi yatim terdeteksi saat reconciliation: simulation_id=%s, "
            "pid=%s, runner_status=%s — proses masih hidup tapi TIDAK ada "
            "monitor thread yang mengawasi (backend baru saja restart). "
            "Dibiarkan jalan sendiri, TIDAK dibunuh, TIDAK di-retry.",
            simulation_id,
            state.process_pid,
            state.runner_status.value,
        )
        return "orphan_alive"

    # PID mati, atau PID hidup tapi bukan proses kita (reuse) -> CRASHED.
    from datetime import datetime

    status_before_crash = state.runner_status
    state.runner_status = RunnerStatus.CRASHED
    state.completed_at = datetime.now().isoformat()
    state.error = (
        "Proses mati tanpa terdeteksi, kemungkinan server restart "
        f"(pid={state.process_pid}, process_started_at={state.process_started_at})."
    )
    state.twitter_running = False
    state.reddit_running = False
    SimulationRunner._save_run_state(state)
    SimulationRunner._sync_simulation_status(
        simulation_id, RunnerStatus.CRASHED, state.error
    )
    logger.warning(
        "Simulasi ditandai CRASHED saat reconciliation: simulation_id=%s, "
        "pid_lama=%s, status_lama_sebelum_crash=%s",
        simulation_id,
        state.process_pid,
        status_before_crash.value,
    )
    return "crashed"


def _reconcile_on_startup_impl() -> None:
    counts = {
        "crashed": 0,
        "orphan_alive": 0,
        "skipped_not_live": 0,
        "skipped_no_state": 0,
        "errored": 0,
    }

    for simulation_id in _iter_simulation_ids():
        try:
            result = _reconcile_one(simulation_id)
            counts[result] = counts.get(result, 0) + 1
        except Exception:
            counts["errored"] += 1
            logger.exception(
                "reconcile gagal untuk satu simulasi, lanjut ke simulasi "
                "berikutnya: simulation_id=%s",
                simulation_id,
            )

    logger.info(
        "reconcile_on_startup selesai: crashed=%d, orphan_alive=%d, "
        "skipped_not_live=%d, skipped_no_state=%d, errored=%d",
        counts["crashed"],
        counts["orphan_alive"],
        counts["skipped_not_live"],
        counts["skipped_no_state"],
        counts["errored"],
    )


def reconcile_on_startup() -> None:
    """Entry point dipanggil sekali di app factory, sesudah
    SimulationRunner.register_cleanup(). Scan semua run_state.json yang
    statusnya "harusnya masih ada proses hidup" (STARTING/RUNNING/PAUSED/
    STOPPING), verifikasi PID+create_time beneran, tandai CRASHED yang
    ternyata sudah mati.

    TIDAK BOLEH melempar exception — kegagalan reconciliation harus TETAP
    membiarkan backend nyala. Simulasi yang salah-tandai lebih baik daripada
    backend yang gagal start sama sekali.
    """
    try:
        _reconcile_on_startup_impl()
    except Exception:
        logger.exception(
            "reconcile_on_startup() gagal total — backend TETAP lanjut start "
            "tanpa reconciliation selesai. Simulasi yang stuck tidak akan "
            "ditandai CRASHED sampai reconciliation berikutnya berhasil."
        )

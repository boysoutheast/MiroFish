"""
T1/T2 — reconciliation simulasi saat startup backend.

Bagian dari PRD "jaminan bayar-per-project" MiroFish. T0 (sudah merge) sudah
menambahkan fondasi yang dipakai di sini: RunnerStatus.CRASHED/
NEEDS_ATTENTION, dan field process_started_at/retry_count/retried_at/
start_params di SimulationRunState.

T1 (bagian atas file ini, TIDAK diubah oleh T2): scan semua run_state.json
yang statusnya "harusnya masih ada proses hidup" (STARTING/RUNNING/PAUSED/
STOPPING), verifikasi PID+create_time beneran, tandai CRASHED yang ternyata
sudah mati. T1 HANYA menandai — tidak pernah memanggil start_simulation().

T2 (bagian bawah file ini): begitu satu simulasi ditandai CRASHED oleh T1
DI PUTARAN INI, T2 memutuskan apakah dia layak auto-retry SEKALI:
- retry_count == 0 DAN graph yang dipakainya masih valid -> retry (cleanup
  log lama, tulis retry_count=1 ke disk, BARU spawn proses baru).
- retry_count >= 1 (sudah pernah dicoba dan crash lagi), ATAU graph sudah
  tidak valid (project di-reset/dihapus), ATAU run_state tidak punya
  start_params sama sekali -> NEEDS_ATTENTION, TIDAK retry.
- Concurrency dibatasi ke _MAX_CONCURRENT_RETRIES simulasi sekaligus; sisanya
  langsung NEEDS_ATTENTION tanpa diam-diam diantrekan.

Retry (spawn proses baru) jalan di THREAD BACKGROUND terpisah supaya tidak
memblokir create_app()/startup Flask — lihat _spawn_retry_worker_thread().

ASUMSI SINGLE-PROCESS (WAJIB dibaca sebelum ubah topology deploy):
Seluruh mekanisme retry-sekali di modul ini (retry_count sebagai gerbang,
_MAX_CONCURRENT_RETRIES, urutan "tulis retry_count=1 dulu baru spawn") HANYA
aman kalau backend jalan sebagai SATU PROSES OS (backend/run.py memanggil
app.run() polos, bukan gunicorn/uwsgi multi-worker). Gerbang kelayakan retry
di _retry_eligibility_reason() cuma mengecek retry_count yang tersimpan di
run_state.json — TIDAK ADA cross-process lock (file lock, DB row lock, dll)
yang mengunci run_state.json antar proses OS berbeda. Kalau nanti deploy
dipindah ke multi-worker (mis. `gunicorn -w N` dengan N>1), tiap worker
punya reconciliation sendiri-sendiri saat startup — dua worker BISA
membaca run_state yang sama (retry_count=0), sama-sama lolos gerbang, dan
retry simulasi yang SAMA bersamaan (2x biaya, 2x proses simulasi jalan
paralel untuk satu simulation_id). Ini BUKAN bug sekarang (topology deploy
saat ini single-process), tapi WAJIB kelihatan jelas buat siapa pun yang
nanti ubah topology deploy — tambahkan cross-process lock di
_retry_crashed_simulations()/_retry_one() sebelum multi-worker diaktifkan.
"""

import fcntl
import glob
import os
import signal
import threading
import time
from datetime import datetime
from typing import List, Optional

import psutil

from ..utils.logger import get_logger
from .simulation_runner import RunnerStatus, SimulationRunner, SimulationRunState

logger = get_logger("mirofish.simulation_reconciler")

# Berapa banyak simulasi CRASHED yang boleh di-retry BERSAMAAN dalam satu
# putaran reconciliation. Kalau server abis restart pas banyak simulasi
# jalan sekaligus, kita SENGAJA tidak retry semuanya paralel (biaya + beban
# proses) dan TIDAK mengantrekan sisanya diam-diam (user lebih baik tahu
# "butuh perhatian manual" segera daripada nunggu lama tanpa kepastian).
_MAX_CONCURRENT_RETRIES = 2

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


# Batas TOTAL (bukan per-yatim) untuk seluruh batch yatim: SIGTERM ke semua
# dulu, tunggu bersama sampai grace habis, lalu SIGKILL sisanya dan tunggu
# bersama sampai kill-wait habis. Konstanta modul supaya tes bisa patch.
_ORPHAN_KILL_GRACE_SECONDS = 10.0
_ORPHAN_KILL_WAIT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.1
_LOCK_FILENAME = ".reconcile.lock"


def _is_dead(proc: "psutil.Process") -> bool:
    try:
        return proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running()
    except psutil.NoSuchProcess:
        return True


def _is_protected_pid(pid: int) -> bool:
    """True kalau PID ini TIDAK boleh disinyal: proses kita sendiri, induk
    kita (mis. reloader/supervisor), atau satu process group dengan kita
    (killpg akan menembak diri sendiri)."""
    if pid in (os.getpid(), os.getppid()):
        return True
    try:
        return os.getpgid(pid) == os.getpgrp()
    except (ProcessLookupError, OSError):
        return False


def _is_group_leader(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, OSError):
        return False


def _signal_orphan(proc: "psutil.Process", pid: int, sig: int) -> None:
    """Kirim sinyal ke process group kalau proses itu leader grup (di-spawn
    start_new_session=True), kalau tidak ke prosesnya saja."""
    if _is_group_leader(pid):
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, OSError):
            pass
    proc.send_signal(sig)


def _signal_target(target: dict, sig: int) -> None:
    """Sinyal proses utama; kalau BUKAN leader grup, anak-anaknya (rekursif,
    dicatat sebelum sinyal pertama) juga disinyal."""
    pid = target["pid"]
    _signal_orphan(target["proc"], pid, sig)
    for child in target["children"]:
        try:
            if not _is_protected_pid(child.pid):
                child.send_signal(sig)
        except psutil.NoSuchProcess:
            pass


def _target_dead(target: dict) -> bool:
    return _is_dead(target["proc"]) and all(_is_dead(c) for c in target["children"])


def _wait_all_dead(targets: List[dict], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if all(_target_dead(t) for t in targets):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)


def _terminate_orphans(items: List[tuple]) -> dict:
    """Hentikan banyak proses yatim sekaligus. `items` = [(pid,
    expected_started_at)]. Return {pid: bool}; True = proses (PID+create_time
    yang sama) sudah mati / bukan proses kita, False = gagal / ditolak.
    Waktu total dibatasi (SIGTERM ke semua -> tunggu bersama -> SIGKILL
    sisanya -> tunggu bersama), bukan N x grace. Verifikasi create_time
    diulang sebelum SIGKILL (anti PID-reuse)."""
    results: dict = {}
    targets: List[dict] = []
    for pid, started_at in items:
        try:
            if not _process_is_ours(pid, started_at):
                results[pid] = True
                continue
            if _is_protected_pid(pid):
                logger.error(
                    "Menolak membunuh pid=%s: itu proses kita sendiri / induk "
                    "/ satu process group dengan kita.", pid,
                )
                results[pid] = False
                continue
            proc = psutil.Process(pid)
            children = [] if _is_group_leader(pid) else proc.children(recursive=True)
            target = {"pid": pid, "started_at": started_at, "proc": proc,
                      "children": children}
            _signal_target(target, signal.SIGTERM)
            targets.append(target)
        except psutil.NoSuchProcess:
            results[pid] = True
        except Exception:
            logger.exception("Gagal menghentikan proses yatim: pid=%s", pid)
            results[pid] = False

    _wait_all_dead(targets, _ORPHAN_KILL_GRACE_SECONDS)

    stubborn = []
    for t in targets:
        if _target_dead(t):
            results[t["pid"]] = True
            continue
        try:
            if not _process_is_ours(t["pid"], t["started_at"]):
                results[t["pid"]] = _is_dead(t["proc"])
                continue
            _signal_target(t, signal.SIGKILL)
            stubborn.append(t)
        except psutil.NoSuchProcess:
            results[t["pid"]] = True
        except Exception:
            logger.exception("Gagal SIGKILL proses yatim: pid=%s", t["pid"])
            results[t["pid"]] = False

    _wait_all_dead(stubborn, _ORPHAN_KILL_WAIT_SECONDS)
    for t in stubborn:
        results[t["pid"]] = _target_dead(t)
    return results


def _terminate_orphan(pid: Optional[int], expected_started_at: Optional[float]) -> bool:
    """Versi satu-proses dari _terminate_orphans()."""
    return _terminate_orphans([(pid, expected_started_at)])[pid]


def _classify(simulation_id: str):
    """('done', hasil) kalau sudah final, atau ('orphan', state) kalau yatim
    milik kita yang harus dihentikan dulu."""
    state = SimulationRunner._load_run_state(simulation_id)
    if state is None:
        return "done", "skipped_no_state"

    if state.runner_status not in _LIVE_STATUSES:
        return "done", "skipped_not_live"

    if _process_is_ours(state.process_pid, state.process_started_at):
        monitor = SimulationRunner._monitor_threads.get(simulation_id)
        if monitor is not None and monitor.is_alive():
            # Ada monitor thread terdaftar di proses ini -> BUKAN yatim,
            # ada yang mengawasi. Jangan disentuh.
            logger.warning(
                "Simulasi hidup dengan monitor thread terdaftar, tidak "
                "disentuh: simulation_id=%s, pid=%s",
                simulation_id,
                state.process_pid,
            )
            return "done", "monitored_alive"
        logger.warning(
            "Simulasi yatim terdeteksi: simulation_id=%s, pid=%s, "
            "runner_status=%s — dihentikan lalu ditandai CRASHED.",
            simulation_id,
            state.process_pid,
            state.runner_status.value,
        )
        return "orphan", state

    return "crashed_now", state


def _finalize(state: SimulationRunState, orphan_killed: bool) -> str:
    """Tandai CRASHED (PID mati / reuse / yatim yang sudah dihentikan)."""
    simulation_id = state.simulation_id
    status_before_crash = state.runner_status
    state.runner_status = RunnerStatus.CRASHED
    state.completed_at = datetime.now().isoformat()
    if orphan_killed:
        state.error = (
            "Proses yatim (tanpa monitor setelah server restart) dihentikan "
            f"(pid={state.process_pid}, process_started_at={state.process_started_at})."
        )
    else:
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


def _finalize_orphan(state: SimulationRunState, killed: bool) -> str:
    if killed:
        return _finalize(state, orphan_killed=True)
    pid = state.process_pid
    _mark_needs_attention(
        state,
        f"Proses simulasi yatim gagal dihentikan (pid={pid}) — TIDAK "
        "di-retry supaya tidak ada dua simulasi bersamaan untuk "
        f"simulation_id yang sama. Matikan manual: `kill -9 {pid}` "
        "(pastikan itu proses simulasi, bukan proses lain), lalu "
        "start ulang simulasinya.",
    )
    return "needs_attention"


def _reconcile_one(simulation_id: str) -> str:
    """Reconcile satu simulasi. Return salah satu:
    'crashed' | 'monitored_alive' | 'needs_attention' | 'skipped_no_state' | 'skipped_not_live'.
    """
    kind, payload = _classify(simulation_id)
    if kind == "done":
        return payload
    if kind == "crashed_now":
        return _finalize(payload, orphan_killed=False)
    pid = payload.process_pid
    killed = _terminate_orphans([(pid, payload.process_started_at)])[pid]
    return _finalize_orphan(payload, killed)


def _reconcile_on_startup_impl() -> None:
    counts = {
        "crashed": 0,
        "monitored_alive": 0,
        "needs_attention": 0,
        "skipped_not_live": 0,
        "skipped_no_state": 0,
        "errored": 0,
    }
    newly_crashed_ids: List[str] = []
    orphans: List[SimulationRunState] = []

    def _tally(result: str, simulation_id: str) -> None:
        counts[result] = counts.get(result, 0) + 1
        if result == "crashed":
            newly_crashed_ids.append(simulation_id)

    def _log_error(simulation_id: str) -> None:
        counts["errored"] += 1
        logger.exception(
            "reconcile gagal untuk satu simulasi, lanjut ke simulasi "
            "berikutnya: simulation_id=%s",
            simulation_id,
        )

    # Fase 1: klasifikasi semua simulasi (PID mati langsung ditandai CRASHED).
    for simulation_id in _iter_simulation_ids():
        try:
            kind, payload = _classify(simulation_id)
            if kind == "done":
                _tally(payload, simulation_id)
            elif kind == "crashed_now":
                _tally(_finalize(payload, orphan_killed=False), simulation_id)
            else:
                orphans.append(payload)
        except Exception:
            _log_error(simulation_id)

    # Fase 2: hentikan SEMUA yatim bersamaan (waktu total terbatas, bukan
    # N x grace) supaya startup tidak terblokir serial.
    if orphans:
        try:
            killed_by_pid = _terminate_orphans(
                [(o.process_pid, o.process_started_at) for o in orphans]
            )
        except Exception:
            logger.exception("Gagal menghentikan batch yatim")
            killed_by_pid = {}
        for state in orphans:
            try:
                killed = killed_by_pid.get(state.process_pid, False)
                _tally(_finalize_orphan(state, killed), state.simulation_id)
            except Exception:
                _log_error(state.simulation_id)

    logger.info(
        "reconcile_on_startup selesai: crashed=%d, monitored_alive=%d, needs_attention=%d, "
        "skipped_not_live=%d, skipped_no_state=%d, errored=%d",
        counts["crashed"],
        counts["monitored_alive"],
        counts["needs_attention"],
        counts["skipped_not_live"],
        counts["skipped_no_state"],
        counts["errored"],
    )

    # T2: simulasi yang BARU SAJA ditandai CRASHED di putaran ini adalah
    # kandidat auto-retry. Dispatch ke thread background — TIDAK boleh
    # blocking di sini (create_app() menunggu fungsi ini selesai).
    if newly_crashed_ids:
        _spawn_retry_worker_thread(newly_crashed_ids)


def reconcile_on_startup() -> None:
    """Entry point dipanggil sekali di app factory, sesudah
    SimulationRunner.register_cleanup(). Scan semua run_state.json yang
    statusnya "harusnya masih ada proses hidup" (STARTING/RUNNING/PAUSED/
    STOPPING), verifikasi PID+create_time beneran, tandai CRASHED yang
    ternyata sudah mati.

    Dilewati kalau (a) ini proses induk reloader Werkzeug
    (WERKZEUG_RUN_MAIN ada dan != "true"), atau (b) proses lain memegang file
    lock `.reconcile.lock` di RUN_STATE_DIR (sedang/telah reconcile).

    TIDAK BOLEH melempar exception — kegagalan reconciliation harus TETAP
    membiarkan backend nyala. Simulasi yang salah-tandai lebih baik daripada
    backend yang gagal start sama sekali.
    """
    reloader_flag = os.environ.get("WERKZEUG_RUN_MAIN")
    if reloader_flag is not None and reloader_flag != "true":
        logger.info(
            "reconcile_on_startup dilewati: proses induk reloader "
            "(WERKZEUG_RUN_MAIN=%r).", reloader_flag,
        )
        return

    lock_file = None
    try:
        try:
            os.makedirs(SimulationRunner.RUN_STATE_DIR, exist_ok=True)
            lock_file = open(
                os.path.join(SimulationRunner.RUN_STATE_DIR, _LOCK_FILENAME), "w"
            )
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            logger.warning(
                "reconcile_on_startup dilewati: lock %s tidak didapat (%s) — "
                "proses lain sedang/telah reconcile.", _LOCK_FILENAME, error,
            )
            return
        _reconcile_on_startup_impl()
    except Exception:
        logger.exception(
            "reconcile_on_startup() gagal total — backend TETAP lanjut start "
            "tanpa reconciliation selesai. Simulasi yang stuck tidak akan "
            "ditandai CRASHED sampai reconciliation berikutnya berhasil."
        )
    finally:
        if lock_file is not None:
            lock_file.close()  # menutup fd melepas flock


# =============================================================================
# T2 — auto-retry SEKALI untuk simulasi CRASHED (thread background)
# =============================================================================


def _mark_needs_attention(
    state: SimulationRunState, reason: str, *, bump_retry: bool = False
) -> None:
    """Tandai satu run_state sebagai NEEDS_ATTENTION dan simpan.

    `bump_retry=True` dipakai kalau kegagalan terjadi SESUDAH kita
    memutuskan untuk retry (mis. cleanup gagal, atau start_simulation()
    melempar exception) — retry_count WAJIB tetap kesave sebagai 1 supaya
    putaran reconcile berikutnya tidak retry kedua kalinya, meskipun proses
    barunya sendiri gagal di-spawn.
    """
    if bump_retry:
        state.retry_count = 1
        state.retried_at = datetime.now().isoformat()
    state.runner_status = RunnerStatus.NEEDS_ATTENTION
    state.completed_at = datetime.now().isoformat()
    state.error = reason
    state.twitter_running = False
    state.reddit_running = False
    # Buang cache in-memory dulu -- SimulationRunner.start_simulation() atau
    # cleanup_simulation_logs() yang barusan gagal bisa saja sudah menaruh
    # objek state LAIN (mis. FAILED) di cache; kita mau versi kita yang
    # menang di disk maupun di cache.
    SimulationRunner._run_states.pop(state.simulation_id, None)
    SimulationRunner._save_run_state(state)
    SimulationRunner._sync_simulation_status(
        state.simulation_id, RunnerStatus.NEEDS_ATTENTION, reason
    )
    logger.warning(
        "Simulasi ditandai NEEDS_ATTENTION: simulation_id=%s, retry_count=%d, "
        "alasan=%s",
        state.simulation_id,
        state.retry_count,
        reason,
    )


def _graph_still_valid(state: SimulationRunState) -> bool:
    """True kalau graph yang dipakai start_params masih valid buat di-retry.

    Kalau simulasi ini tidak pernah pakai graph memory update, tidak ada
    graph yang perlu divalidasi — retry boleh lanjut murni berdasar proses.
    Kalau iya, project induknya HARUS masih ada DAN project.graph_id (bukan
    graph_id yang di-snapshot di start_params — itu bisa basi kalau project
    di-reset) HARUS masih persis sama dengan graph_id yang dipakai run yang
    crash ini. Ini pola yang sama dipakai endpoint /start (force-restart,
    T3B1): "The project is authoritative", lihat backend/app/api/
    simulation.py sekitar baris 1706-1718.
    """
    start_params = state.start_params or {}
    if not start_params.get("enable_graph_memory_update"):
        return True

    original_graph_id = start_params.get("graph_id")
    if not original_graph_id:
        # enable_graph_memory_update=True tapi graph_id kosong itu data yang
        # sudah rusak sejak awal -- jangan retry buta.
        return False

    from .simulation_manager import SimulationManager

    simulation = SimulationManager().get_simulation(state.simulation_id)
    if simulation is None:
        return False

    from ..models.project import ProjectManager

    project = ProjectManager.get_project(simulation.project_id)
    if project is None:
        return False

    return project.graph_id == original_graph_id


def _retry_eligibility_reason(state: SimulationRunState) -> Optional[str]:
    """None kalau layak di-retry. Kalau tidak, string alasan buat
    NEEDS_ATTENTION."""
    if state.retry_count >= 1:
        return (
            "Simulasi sudah pernah di-retry otomatis sekali (retry_count="
            f"{state.retry_count}) dan crash lagi — butuh perhatian manual, "
            "tidak di-retry lagi."
        )
    if not state.start_params:
        return (
            "run_state tidak punya start_params tersimpan (kemungkinan run "
            "lama pra-T0) — tidak bisa auto-retry tanpa interaksi user."
        )
    if not _graph_still_valid(state):
        return (
            "Graph yang dipakai simulasi ini sudah tidak valid lagi "
            "(project di-reset atau dihapus di antara crash dan retry) — "
            "auto-retry dibatalkan."
        )
    return None


def _retry_one(simulation_id: str) -> None:
    """Eksekusi retry buat SATU simulasi yang sudah lolos gerbang
    kelayakan. Dipanggil dari thread worker (lihat _spawn_retry_worker_thread).

    Urutan KRITIKAL, jangan diubah: bersihkan data lama -> retry_count=1
    ditulis ke disk (di dalam start_simulation(), sebelum subprocess.Popen)
    -> baru proses baru di-spawn. Kalau spawn gagal di TITIK MANA PUN sesudah
    retry_count kesave, kita TIDAK retry lagi — cukup NEEDS_ATTENTION.

    Seluruh badan fungsi ini dibungkus try/except Exception generik di
    LEVEL PALING LUAR (di luar dua try/except internal untuk
    cleanup_simulation_logs/start_simulation) sebagai pengaman terakhir:
    fungsi ini dipanggil lewat threading.Thread(target=_retry_one, ...), dan
    exception yang lolos sampai ke thread itu jatuh ke
    threading.excepthook DEFAULT Python (cetak ke stderr doang, BUKAN lewat
    `logger` project ini) — kalau backend dijalankan sebagai service tanpa
    stderr capture, kejadian itu invisible total.
    """
    try:
        state = SimulationRunner._load_run_state(simulation_id)
        if state is None or state.runner_status != RunnerStatus.CRASHED:
            # Race benign: state berubah di antara pre-scan dan eksekusi
            # thread ini (mis. simulasi lain menyentuhnya). Tidak ada apa
            # pun yang perlu dilakukan lagi di sini.
            return

        start_params = state.start_params or {}

        # Data lama (twitter/reddit actions.jsonl, *.db, dll) HARUS
        # dibersihkan dulu sebelum retry, kalau tidak retry bakal baca
        # ulang data lama dan bikin angka report dobel. Pakai fungsi yang
        # sama yang dipakai T3B1 untuk force-restart -- JANGAN bikin
        # cleanup baru.
        try:
            cleanup_result = SimulationRunner.cleanup_simulation_logs(simulation_id)
        except Exception as error:
            logger.exception(
                "Auto-retry T2: cleanup_simulation_logs() melempar exception: "
                "simulation_id=%s",
                simulation_id,
            )
            _mark_needs_attention(
                state,
                f"Auto-retry gagal membersihkan log lama (exception): {error}",
                bump_retry=True,
            )
            return

        if not cleanup_result.get("success"):
            _mark_needs_attention(
                state,
                "Auto-retry gagal membersihkan log lama: "
                f"{cleanup_result.get('errors')}",
                bump_retry=True,
            )
            return

        retried_at = datetime.now().isoformat()
        try:
            SimulationRunner.start_simulation(
                simulation_id=simulation_id,
                platform=start_params.get("platform", "parallel"),
                max_rounds=start_params.get("max_rounds"),
                enable_graph_memory_update=start_params.get(
                    "enable_graph_memory_update", False
                ),
                graph_id=start_params.get("graph_id"),
                retry_count=1,
                retried_at=retried_at,
            )
            logger.warning(
                "Auto-retry T2 sukses: simulation_id=%s di-restart sekali "
                "(retry_count=1, retried_at=%s).",
                simulation_id,
                retried_at,
            )
        except Exception:
            # cleanup_simulation_logs() di atas sudah menghapus
            # run_state.json lama. start_simulation() sendiri, di dalam
            # try/except internalnya, SEHARUSNYA sudah menyimpan run_state
            # FAILED dengan retry_count=1 kalau exception terjadi sesudah
            # _save_run_state pertamanya -- tapi kalau exception terjadi
            # LEBIH AWAL dari itu (mis. config simulation_config.json
            # hilang, raise ValueError di baris paling atas
            # start_simulation() sebelum sempat menyentuh disk sama
            # sekali), run_state.json bisa saja TIDAK ADA SAMA SEKALI
            # sekarang. Pakai `state` yang kita pegang dari SEBELUM
            # cleanup (retry_count masih di memori) supaya
            # NEEDS_ATTENTION + retry_count=1 tetap kesave walau kasus itu
            # terjadi.
            logger.exception(
                "Auto-retry T2 gagal spawn ulang: simulation_id=%s — "
                "ditandai NEEDS_ATTENTION, TIDAK retry lagi.",
                simulation_id,
            )
            fresh_state = SimulationRunner._load_run_state(simulation_id) or state
            _mark_needs_attention(
                fresh_state,
                "Auto-retry gagal (proses tidak bisa di-spawn ulang setelah 1x "
                "percobaan).",
                bump_retry=True,
            )
    except Exception:
        # Pengaman terakhir: exception DI LUAR dua titik di atas (mis.
        # _mark_needs_attention() sendiri melempar OSError disk-full, atau
        # bug lain yang belum kepikiran). WAJIB lewat `logger`, bukan lolos
        # ke threading.excepthook default yang cuma cetak ke stderr.
        logger.exception(
            "Auto-retry T2: exception TIDAK TERDUGA lolos sampai level "
            "thread _retry_one — simulation_id=%s. Mencoba fallback "
            "menandai NEEDS_ATTENTION supaya simulasi tidak nyangkut tanpa "
            "status jelas.",
            simulation_id,
        )
        try:
            fallback_state = SimulationRunner._load_run_state(simulation_id)
            if fallback_state is not None:
                _mark_needs_attention(
                    fallback_state,
                    "Auto-retry gagal total karena exception tidak terduga "
                    "di level thread — butuh perhatian manual.",
                    bump_retry=True,
                )
        except Exception:
            # Fallback sendiri gagal (mis. disk benar-benar penuh). Yang
            # penting error di atas sudah ke-log lewat `logger` -- jangan
            # biarkan exception ini lolos ke luar fungsi.
            logger.exception(
                "Auto-retry T2: fallback _mark_needs_attention juga gagal "
                "sesudah exception tidak terduga: simulation_id=%s. State "
                "simulasi ini kemungkinan masih CRASHED tanpa retry_count "
                "ter-bump -- perlu dicek manual.",
                simulation_id,
            )


def _retry_crashed_simulations(simulation_ids: List[str]) -> None:
    """Logic utama T2, jalan di dalam thread background (lihat
    _spawn_retry_worker_thread). TIDAK BOLEH melempar exception ke luar —
    ini jalan di daemon thread terpisah dari request/startup mana pun.

    CATATAN (diperbaiki — klaim lama di sini SALAH): exception yang lolos
    sampai ke level thread TIDAK "cuma ke-log oleh Python's default thread
    excepthook tanpa merusak apa pun" — excepthook default cuma cetak ke
    stderr, BUKAN lewat `logger` project ini, dan kalau backend dijalankan
    sebagai service tanpa stderr capture itu invisible total (simulasi
    nyangkut di limbo tanpa jejak log yang berguna). Karena itu setiap
    fungsi yang dispatch ke thread sendiri (lihat `_retry_one`) WAJIB
    membungkus badannya dengan try/except paling luar yang eksplisit
    memanggil `logger.exception(...)`, bukan mengandalkan excepthook
    default. Fail-safe per-simulasi di loop bawah ini tetap dipertahankan
    supaya satu simulasi corrupt tidak menghentikan evaluasi sisanya.
    """
    eligible_ids: List[str] = []

    for simulation_id in simulation_ids:
        try:
            state = SimulationRunner._load_run_state(simulation_id)
            if state is None or state.runner_status != RunnerStatus.CRASHED:
                continue

            reason = _retry_eligibility_reason(state)
            if reason is not None:
                _mark_needs_attention(state, reason)
                continue

            eligible_ids.append(simulation_id)
        except Exception:
            logger.exception(
                "T2: gagal mengevaluasi kelayakan retry untuk satu simulasi, "
                "lanjut ke simulasi berikutnya: simulation_id=%s",
                simulation_id,
            )

    to_retry = eligible_ids[:_MAX_CONCURRENT_RETRIES]
    overflow = eligible_ids[_MAX_CONCURRENT_RETRIES:]

    for simulation_id in overflow:
        try:
            state = SimulationRunner._load_run_state(simulation_id)
            if state is not None:
                _mark_needs_attention(
                    state,
                    "Batas retry bersamaan "
                    f"({_MAX_CONCURRENT_RETRIES}) sudah penuh saat startup "
                    "ini — butuh perhatian manual, bukan diam-diam "
                    "diantrekan.",
                )
        except Exception:
            logger.exception(
                "T2: gagal menandai NEEDS_ATTENTION untuk simulasi overflow "
                "concurrency: simulation_id=%s",
                simulation_id,
            )

    threads = [
        threading.Thread(target=_retry_one, args=(simulation_id,), daemon=True)
        for simulation_id in to_retry
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _spawn_retry_worker_thread(simulation_ids: List[str]) -> None:
    """Dispatch _retry_crashed_simulations() ke SATU thread daemon terpisah
    supaya create_app()/startup Flask tidak menunggu proses spawn ulang
    selesai. Pola daemon-thread ini sama dengan monitor_thread yang dipakai
    SimulationRunner.start_simulation() untuk memonitor proses simulasi.
    """

    def _run():
        try:
            _retry_crashed_simulations(simulation_ids)
        except Exception:
            logger.exception(
                "T2: worker retry background meledak total — simulasi yang "
                "belum sempat diproses tetap CRASHED sampai reconciliation "
                "berikutnya."
            )

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

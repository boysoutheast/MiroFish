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

import glob
import os
import threading
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
    newly_crashed_ids: List[str] = []

    for simulation_id in _iter_simulation_ids():
        try:
            result = _reconcile_one(simulation_id)
            counts[result] = counts.get(result, 0) + 1
            if result == "crashed":
                newly_crashed_ids.append(simulation_id)
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

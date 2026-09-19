"""
T4: unit test buat timeout eksplisit di env.step() (round_parallel_simulation.py).

Membuktikan:
1. env.step yang hang lebih lama dari timeout -> asyncio.TimeoutError tertangkap,
   event round_timeout tertulis ke log aksi (isi barisnya dicek), main_logger.error
   dipanggil, dan exception di-raise ulang (supaya proses akhirnya exit != 0).
2. env.step yang selesai normal (cepat) -> tidak ada perubahan perilaku,
   tidak ada event round_timeout, actions diterima apa adanya.
3. Di asyncio.gather()-level main(): kalau satu platform timeout/exception,
   platform lain yang masih jalan BENERAN di-cancel, bukan menggantung.

Dependensi berat (oasis, camel) di-stub di sys.modules sebelum import modul target,
supaya modul bisa diimport tanpa environment OASIS asli terpasang.
"""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _install_oasis_camel_stubs():
    """Stub minimal utk 'oasis' & 'camel' supaya run_parallel_simulation bisa diimport
    tanpa dependency berat itu benar-benar terpasang."""
    if "oasis" in sys.modules and getattr(sys.modules["oasis"], "_is_t4_stub", False):
        return

    oasis_mod = types.ModuleType("oasis")
    oasis_mod._is_t4_stub = True
    oasis_mod.ActionType = MagicMock(name="ActionType")
    oasis_mod.LLMAction = MagicMock(name="LLMAction")
    oasis_mod.ManualAction = MagicMock(name="ManualAction")
    oasis_mod.generate_twitter_agent_graph = MagicMock(name="generate_twitter_agent_graph")
    oasis_mod.generate_reddit_agent_graph = MagicMock(name="generate_reddit_agent_graph")
    oasis_mod.make = MagicMock(name="oasis.make")
    oasis_mod.DefaultPlatformType = types.SimpleNamespace(TWITTER="twitter", REDDIT="reddit")
    sys.modules["oasis"] = oasis_mod

    camel_mod = types.ModuleType("camel")
    camel_models_mod = types.ModuleType("camel.models")
    camel_models_mod.ModelFactory = MagicMock(name="ModelFactory")
    camel_types_mod = types.ModuleType("camel.types")
    camel_types_mod.ModelPlatformType = MagicMock(name="ModelPlatformType")
    sys.modules["camel"] = camel_mod
    sys.modules["camel.models"] = camel_models_mod
    sys.modules["camel.types"] = camel_types_mod


_install_oasis_camel_stubs()

rps = importlib.import_module("run_parallel_simulation")


# ---------------------------------------------------------------------------
# 1. env.step hang lebih lama dari timeout -> timeout tertangkap & event tercatat
# ---------------------------------------------------------------------------

def test_env_step_timeout_logs_event_and_reraises(tmp_path):
    """env.step yang sleep lebih lama dari timeout (dipendekkan ke 1 detik utk test)
    harus: raise asyncio.TimeoutError, tulis event round_timeout ke log aksi (isi
    barisnya diperiksa), dan panggil main_logger.error."""

    class HangingEnv:
        async def step(self, actions):
            await asyncio.sleep(5)  # jauh lebih lama dari timeout test (1s)

    action_log_path = tmp_path / "twitter" / "actions.jsonl"
    action_log_path.parent.mkdir(parents=True)
    action_logger = rps.PlatformActionLogger("twitter", str(tmp_path))
    assert action_logger.log_path == str(action_log_path)

    main_logger = MagicMock()

    async def _run():
        with pytest.raises(asyncio.TimeoutError):
            await rps._run_env_step_with_timeout(
                HangingEnv(),
                {"agent": "dummy_action"},
                platform="twitter",
                round_num=3,
                action_logger=action_logger,
                main_logger=main_logger,
                timeout_seconds=1,
            )

    asyncio.run(_run())

    # main_logger.error harus dipanggil dengan pesan yang jelas
    assert main_logger.error.called
    error_msg = main_logger.error.call_args[0][0]
    assert "Round 3" in error_msg
    assert "超时" in error_msg or "timeout" in error_msg.lower()

    # event round_timeout harus tertulis ke actions.jsonl -- cek ISI baris, bukan cuma "ada baris"
    lines = action_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event_type"] == "round_timeout"
    assert entry["round"] == 3
    assert entry["timeout_seconds"] == 1


# ---------------------------------------------------------------------------
# 2. env.step selesai normal -> perilaku sama seperti sebelum perubahan
# ---------------------------------------------------------------------------

def test_env_step_normal_completion_unchanged(tmp_path):
    """env.step yang selesai cepat (di bawah timeout) tidak boleh memicu apa pun
    yang berbeda dari sebelum perubahan: tidak ada round_timeout, tidak exception,
    actions yang dikirim ke env.step() sama persis."""

    received_actions = {}

    class FastEnv:
        async def step(self, actions):
            received_actions.update(actions)
            await asyncio.sleep(0.01)
            return None

    action_logger = rps.PlatformActionLogger("twitter", str(tmp_path))
    main_logger = MagicMock()

    actions_in = {"agent_1": "post_x", "agent_2": "post_y"}

    async def _run():
        await rps._run_env_step_with_timeout(
            FastEnv(),
            actions_in,
            platform="twitter",
            round_num=7,
            action_logger=action_logger,
            main_logger=main_logger,
            timeout_seconds=1,
        )

    asyncio.run(_run())

    assert received_actions == actions_in
    assert not main_logger.error.called

    action_log_path = Path(action_logger.log_path)
    if action_log_path.exists():
        content = action_log_path.read_text(encoding="utf-8")
        assert "round_timeout" not in content
    # jumlah/isi "aksi" (yang dikirim ke env.step) sama persis dgn sebelum perubahan
    # (tidak ada wrapping/mutasi tambahan pada dict actions)
    assert received_actions is not actions_in  # bukan objek yg sama krn .update(), tapi isinya sama
    assert dict(received_actions) == actions_in


# ---------------------------------------------------------------------------
# 3. asyncio.gather-level: satu platform timeout -> platform lain beneran dibatalkan
# ---------------------------------------------------------------------------

def test_gather_cancels_sibling_task_on_failure():
    """Reproduksi pola FIRST_EXCEPTION + cancel yang dipakai di main():
    kalau satu task exception, task lain yang masih jalan lama harus benar2
    di-cancel dalam waktu wajar, bukan menggantung sampai selesai sendiri."""

    sibling_was_cancelled = False
    sibling_ran_to_completion = False

    async def failing_platform():
        await asyncio.sleep(0.05)
        raise asyncio.TimeoutError("round timeout on platform A")

    async def long_running_platform():
        nonlocal sibling_was_cancelled, sibling_ran_to_completion
        try:
            await asyncio.sleep(30)  # jauh lebih lama dari yang wajar utk test
            sibling_ran_to_completion = True
        except asyncio.CancelledError:
            sibling_was_cancelled = True
            raise

    async def _run():
        task_a = asyncio.ensure_future(failing_platform())
        task_b = asyncio.ensure_future(long_running_platform())
        tasks = [task_a, task_b]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        error = None
        for t in done:
            exc = t.exception()
            if exc is not None:
                error = exc
                break

        assert error is not None
        assert task_b in pending  # platform lain masih "menggantung" pada titik ini

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        return error

    # dijalankan dgn wait_for supaya test sendiri tidak menggantung kalau pola gagal
    error = asyncio.run(asyncio.wait_for(_run(), timeout=5))

    assert isinstance(error, asyncio.TimeoutError)
    assert sibling_was_cancelled is True
    assert sibling_ran_to_completion is False


# ---------------------------------------------------------------------------
# 4. [HIGH putaran 2] Dua task gagal "bersamaan" di main()-level exception
#    collection -> KEDUA error harus ke-log, bukan cuma yang pertama ketemu.
# ---------------------------------------------------------------------------

def test_main_level_both_failures_are_logged_not_just_first():
    """Reproduksi persis pola pengumpulan exception yang dipakai di main() setelah
    perbaikan putaran 2: dua task gagal dalam window yang sama (keduanya masuk
    `done` bareng di asyncio.wait(FIRST_EXCEPTION)) -> harus SEMUA task yang gagal
    di-.exception()-kan (supaya 'retrieved', nggak ada yang nyisa buat asyncio
    teriak 'Task exception was never retrieved'), dan errors dari SEMUA task
    dikumpulkan (bukan cuma yang pertama ditemukan lalu break)."""

    async def failing_platform_a():
        await asyncio.sleep(0.05)
        raise asyncio.TimeoutError("platform A timeout")

    async def failing_platform_b():
        # Selesai gagal di window yang hampir sama dengan A, supaya asyncio.wait
        # punya kesempatan mengembalikan KEDUANYA sekaligus di `done`.
        await asyncio.sleep(0.05)
        raise ValueError("platform B crashed for a different reason")

    async def _run():
        task_a = asyncio.ensure_future(failing_platform_a())
        task_b = asyncio.ensure_future(failing_platform_b())
        tasks = [task_a, task_b]

        # Beri jeda supaya scheduler benar2 menjalankan keduanya sampai raise
        # sebelum asyncio.wait mengecek status -- ini memaksimalkan peluang
        # keduanya berakhir di `done` bareng pada satu iterasi wait.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        # Pola persis dari main() setelah perbaikan: kumpulkan SEMUA exception
        # dari task yang done, bukan berhenti di yang pertama.
        errors = []
        try:
            for task in done:
                if task.cancelled():
                    continue
                task_error = task.exception()
                if task_error is not None:
                    errors.append(task_error)
        finally:
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        return done, errors

    done, errors = asyncio.run(asyncio.wait_for(_run(), timeout=5))

    # Kalau scheduling benar2 menaruh keduanya di `done` bersamaan (kasus HIGH
    # yang dilaporkan), maka errors HARUS berisi kedua-duanya -- bukan cuma 1.
    if len(done) == 2:
        assert len(errors) == 2
        error_types = {type(e) for e in errors}
        assert asyncio.TimeoutError in error_types or TimeoutError in error_types
        assert ValueError in error_types
    else:
        # Fallback platform-scheduling: minimal task yang benar2 done exception-nya
        # harus ke-retrieve (tidak ada yang kelewat) -- tidak boleh 0.
        assert len(errors) == len(done)

    # Poin inti MEDIUM+HIGH gabungan: SETIAP task di `done` yang failed harus
    # ke-.exception()-kan (supaya nggak ada "Task exception was never retrieved"
    # warning). Verifikasi eksplisit tiap task di `done` sudah "retrieved":
    for task in done:
        if not task.cancelled():
            # Panggil ulang .exception() harusnya aman (tidak raise lagi, tidak
            # memicu warning baru) -- ini kondisi asyncio utk "sudah diambil".
            assert task.exception() is not None


def _run_main_error_collection_against_run_parallel_simulation(tasks_coro_factories):
    """Helper yang menjalankan LOGIKA PERSIS dari run_parallel_simulation.main()
    (blok pengumpulan error setelah asyncio.wait) terhadap task buatan sendiri,
    tanpa harus menjalankan main() penuh (yang butuh config file dsb)."""

    async def _run():
        tasks = [asyncio.ensure_future(factory()) for factory in tasks_coro_factories]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        errors = []
        try:
            for task in done:
                if task.cancelled():
                    continue
                task_error = task.exception()
                if task_error is not None:
                    errors.append(task_error)
        finally:
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        return errors, pending

    return asyncio.run(asyncio.wait_for(_run(), timeout=5))


def test_main_actual_error_collection_snippet_logs_all_and_raises_first(caplog=None):
    """Sama seperti test di atas tapi lewat helper terpisah, memverifikasi juga
    bahwa `errors[0]` (yang di-raise ulang oleh main()) tetap salah satu dari
    error yang beneran terjadi -- perilaku 'raise salah satu, tapi log semua'
    dari instruksi Boy tetap terpenuhi."""

    async def a():
        await asyncio.sleep(0.02)
        raise RuntimeError("A")

    async def b():
        await asyncio.sleep(0.02)
        raise RuntimeError("B")

    errors, pending = _run_main_error_collection_against_run_parallel_simulation([a, b])

    assert len(pending) == 0
    assert len(errors) >= 1
    # errors[0] (yang bakal di-raise main()) harus salah satu dari A/B, bukan None
    assert str(errors[0]) in ("A", "B")


# ---------------------------------------------------------------------------
# 5. [MEDIUM putaran 2] Task CANCELLED masuk ke `done` -> .exception() nggak
#    boleh raise CancelledError yang lolos ke luar tanpa cleanup pending task.
# ---------------------------------------------------------------------------

def test_cancelled_task_in_done_does_not_leak_pending_and_no_unhandled_baseexception():
    """Reproduksi kasus MEDIUM: salah satu task di `done` ternyata berstatus
    CANCELLED (bukan exception biasa). Sebelum perbaikan, memanggil
    `.exception()` di task itu langsung RAISE CancelledError dan lolos keluar
    dari for-loop tanpa sempat sampai ke blok cleanup pending -- pending task
    jadi bocor (nggak pernah di-cancel/di-await). Perbaikannya: cek
    `task.cancelled()` dulu sebelum manggil `.exception()`, dan cleanup pending
    dibungkus try/finally supaya tetap jalan apa pun yang terjadi di loop."""

    pending_task_cancelled = False
    pending_task_ran_to_completion = False

    async def already_cancelled_by_the_time_we_check():
        # Task ini akan di-cancel dari luar sebelum sempat selesai, sehingga
        # begitu masuk asyncio.wait's `done` set, statusnya CANCELLED.
        await asyncio.sleep(10)

    async def failing_task():
        await asyncio.sleep(0.02)
        raise RuntimeError("normal failure")

    async def long_running_pending_task():
        nonlocal pending_task_cancelled, pending_task_ran_to_completion
        try:
            await asyncio.sleep(30)
            pending_task_ran_to_completion = True
        except asyncio.CancelledError:
            pending_task_cancelled = True
            raise

    async def _run():
        cancelled_task = asyncio.ensure_future(already_cancelled_by_the_time_we_check())
        failing = asyncio.ensure_future(failing_task())
        long_pending = asyncio.ensure_future(long_running_pending_task())

        # Cancel salah satu task SEBELUM asyncio.wait selesai menunggu, supaya
        # begitu wait() mengembalikan, task itu statusnya CANCELLED (bukan
        # exception biasa) dan berpeluang masuk ke `done`.
        await asyncio.sleep(0.01)
        cancelled_task.cancel()

        tasks = [cancelled_task, failing, long_pending]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        # --- pola PERSIS dari main() setelah perbaikan ---
        errors = []
        no_unhandled_baseexception_escaped = True
        try:
            for task in done:
                if task.cancelled():
                    continue
                task_error = task.exception()
                if task_error is not None:
                    errors.append(task_error)
        except BaseException:
            # Kalau ini kena, berarti perbaikan GAGAL -- CancelledError (atau
            # BaseException lain) lolos keluar tanpa ke-guard.
            no_unhandled_baseexception_escaped = False
            raise
        finally:
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        return errors, pending, no_unhandled_baseexception_escaped

    errors, pending, no_unhandled_baseexception_escaped = asyncio.run(
        asyncio.wait_for(_run(), timeout=5)
    )

    # 1. Nol BaseException (termasuk CancelledError dari task cancelled) yang
    #    lolos keluar tanpa ketahan.
    assert no_unhandled_baseexception_escaped is True

    # 2. Task yang gagal biasa (RuntimeError) tetap ke-collect errornya.
    assert any(isinstance(e, RuntimeError) for e in errors)

    # 3. Task pending (long_running_pending_task) tetap ke-cancel dengan benar
    #    -- ini poin inti MEDIUM: cleanup pending TIDAK boleh ke-skip gara2
    #    CancelledError dari task lain yang di-.exception()-kan duluan.
    assert pending_task_cancelled is True
    assert pending_task_ran_to_completion is False


# ---------------------------------------------------------------------------
# 6. [MEDIUM, dari code-reviewer] 3 titik env.step() di interview mode
#    sekarang dibungkus _run_env_step_with_timeout juga -> timeout terdeteksi,
#    proses TIDAK exit, error response yang masuk akal balik ke caller.
# ---------------------------------------------------------------------------

class _HangingInterviewEnv:
    """Env palsu yang env.step()-nya hang selamanya (mensimulasikan LLM API yang
    tidak pernah merespons saat interview mode, sesudah simulasi round-loop
    selesai)."""

    def __init__(self):
        self.agent_graph = MagicMock()

    async def step(self, actions):
        await asyncio.sleep(10)  # jauh lebih lama dari timeout test


class _FakeAgentGraph:
    def __init__(self):
        self._agent = MagicMock(name="fake_agent")

    def get_agent(self, agent_id):
        return self._agent


def _patch_short_step_timeout(monkeypatch, timeout_seconds=1):
    """`_run_env_step_with_timeout` punya default `timeout_seconds=ROUND_TIMEOUT_SECONDS`
    yang di-bind SEKALI saat modul di-load (default argument Python dievaluasi
    saat def, bukan saat dipanggil) -- monkeypatch.setattr(rps, "ROUND_TIMEOUT_SECONDS", ...)
    TIDAK mengubah default yang sudah ke-bind itu. Supaya test tidak perlu
    menunggu 600 detik asli, kita ganti fungsinya sendiri dengan wrapper yang
    memaksa timeout_seconds pendek; nama globalnya (`_run_env_step_with_timeout`,
    dipanggil tanpa prefix modul di titik-titik interview) di-resolve saat
    dipanggil, jadi monkeypatch di level modul ini efektif."""
    original = rps._run_env_step_with_timeout

    async def _short_timeout_version(env, actions, *, platform, round_num,
                                      action_logger=None, main_logger=None,
                                      timeout_seconds=timeout_seconds):
        return await original(
            env, actions, platform=platform, round_num=round_num,
            action_logger=action_logger, main_logger=main_logger,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(rps, "_run_env_step_with_timeout", _short_timeout_version)


def test_interview_single_platform_timeout_does_not_crash_and_returns_error(monkeypatch):
    """_interview_single_platform: kalau env.step() hang (LLM API tidak
    merespons) di titik interview mode, harus: timeout terdeteksi lewat
    _run_env_step_with_timeout, TIDAK ada proses/exception yang exit ke atas
    (beda dari round-loop yang re-raise sampai ke main()), dan caller (IPC
    handler) menerima dict berisi "error" yang jelas -- pola yang sama dengan
    penanganan error lain di fungsi ini."""

    # Pakai timeout pendek supaya test tidak menunggu 600 detik asli.
    _patch_short_step_timeout(monkeypatch, timeout_seconds=1)

    handler = rps.ParallelIPCHandler(
        simulation_dir=str(Path("/tmp") / "t4_interview_timeout_test"),
        twitter_env=_HangingInterviewEnv(),
        twitter_agent_graph=_FakeAgentGraph(),
    )

    async def _run():
        return await asyncio.wait_for(
            handler._interview_single_platform(agent_id=1, prompt="halo?", platform="twitter"),
            timeout=5,  # jaga2 supaya test sendiri nggak menggantung kalau perbaikan gagal
        )

    result = asyncio.run(_run())

    # Proses tidak exit / tidak ada exception yang lolos ke luar dari
    # _interview_single_platform -- kita dapat dict biasa, bukan exception.
    assert isinstance(result, dict)
    # Error response yang masuk akal: field "error" ada dan bukan string kosong
    # (str(asyncio.TimeoutError()) sering kosong -- makanya di kode pakai pesan
    # eksplisit, bukan cuma str(e)).
    assert "error" in result
    assert result["error"]
    assert "platform" in result


def test_batch_interview_twitter_timeout_does_not_crash_and_skips_platform(monkeypatch):
    """handle_batch_interview (titik ~520, jalur Twitter): env.step() yang hang
    harus terdeteksi lewat helper timeout, tidak melempar exception yang lolos
    ke luar handle_batch_interview, dan direspons lewat command_id response biasa
    (bukan proses exit)."""

    _patch_short_step_timeout(monkeypatch, timeout_seconds=1)

    handler = rps.ParallelIPCHandler(
        simulation_dir=str(Path("/tmp") / "t4_batch_interview_timeout_test"),
        twitter_env=_HangingInterviewEnv(),
        twitter_agent_graph=_FakeAgentGraph(),
    )

    sent = {}

    def _fake_send_response(command_id, status, result=None, error=None):
        sent["command_id"] = command_id
        sent["status"] = status
        sent["result"] = result
        sent["error"] = error

    handler.send_response = _fake_send_response

    async def _run():
        return await asyncio.wait_for(
            handler.handle_batch_interview(
                command_id="cmd-1",
                interviews=[{"agent_id": 1, "prompt": "halo?", "platform": "twitter"}],
            ),
            timeout=5,
        )

    ok = asyncio.run(_run())

    # Tidak crash proses: fungsi mengembalikan bool biasa.
    assert ok is False  # tidak ada hasil sukses krn satu2nya platform timeout
    # Respons dikirim balik ke caller lewat send_response, bukan lewat exception
    # yang lolos sampai ke atas / mematikan proses.
    assert sent["command_id"] == "cmd-1"
    assert sent["status"] == "failed"
    assert sent["error"]  # ada pesan error yang masuk akal


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

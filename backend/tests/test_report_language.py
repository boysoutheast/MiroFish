"""Bahasa keluaran report: default 'id', env/override, prompt memuat instruksi, parsing outline."""
import importlib
from unittest.mock import MagicMock

import pytest

from app.utils import locale as loc
from app.services import report_agent as ra


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv('REPORT_OUTPUT_LANGUAGE', raising=False)
    loc.set_output_language(None)
    yield
    loc.set_output_language(None)


def test_default_is_indonesian():
    assert loc.get_output_language() == 'id'
    ins = loc.get_output_language_instruction()
    assert 'Bahasa Indonesia' in ins and 'Mandarin' in ins


def test_env_switches_language(monkeypatch):
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'en')
    assert 'English' in loc.get_output_language_instruction()
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'xx-invalid')
    assert loc.get_output_language() == 'id'


def test_request_override_beats_env(monkeypatch):
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'en')
    loc.set_output_language('id')
    assert loc.get_output_language() == 'id'
    loc.set_output_language('bogus')
    assert loc.get_output_language() == 'en'
    assert loc.resolve_output_language('zh') == 'zh'
    assert loc.resolve_output_language('bogus') == 'en'


def test_wrapper_puts_instruction_at_start_and_end():
    out = loc.with_language_instruction("BODY")
    ins = loc.get_output_language_instruction()
    assert out.startswith(ins) and out.endswith(ins) and "BODY" in out


def _agent(llm):
    a = ra.ReportAgent.__new__(ra.ReportAgent)
    a.llm = llm
    a.graph_id = 'g'
    a.simulation_id = 's'
    a.simulation_requirement = 'Serum wajah Kulit Cerah untuk pasar Indonesia'
    a.zep_tools = MagicMock()
    a.zep_tools.get_simulation_context.return_value = {'graph_statistics': {}, 'related_facts': []}
    return a


def test_plan_prompt_has_indonesian_instruction_and_parses_indonesian_outline():
    llm = MagicMock()
    llm.chat_json.return_value = {
        "title": "Prediksi Penerimaan Pasar Serum Kulit Cerah",
        "summary": "Ringkasan",
        "sections": [{"title": "Pendahuluan"}, {"title": "Temuan Utama"}],
    }
    outline = _agent(llm).plan_outline()
    system = llm.chat_json.call_args.kwargs['messages'][0]['content']
    assert 'Bahasa Indonesia' in system
    assert system.startswith(loc.get_output_language_instruction())
    assert [s.title for s in outline.sections] == ["Pendahuluan", "Temuan Utama"]
    assert outline.title.startswith("Prediksi")


def test_plan_prompt_follows_env(monkeypatch):
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'en')
    llm = MagicMock()
    llm.chat_json.return_value = {"title": "T", "summary": "S", "sections": []}
    _agent(llm).plan_outline()
    system = llm.chat_json.call_args.kwargs['messages'][0]['content']
    assert 'Please respond in English' in system and 'Bahasa Indonesia' not in system


def test_fallback_outline_is_indonesian_not_chinese():
    llm = MagicMock()
    llm.chat_json.side_effect = RuntimeError("boom")
    outline = _agent(llm).plan_outline()
    text = outline.title + outline.summary + "".join(s.title for s in outline.sections)
    assert not any('一' <= c <= '鿿' for c in text)
    assert len(outline.sections) == 3


def test_empty_title_uses_localized_default():
    llm = MagicMock()
    llm.chat_json.return_value = {"summary": "S", "sections": [{"title": "A"}]}
    outline = _agent(llm).plan_outline()
    assert outline.title == "Laporan Prediksi Masa Depan"



# ---- H1: get_language_instruction() tetap mengikuti locale UI ----
@pytest.mark.parametrize('ui', ['en', 'zh'])
def test_ui_instruction_follows_ui_locale_not_output_language(ui):
    loc.set_locale(ui)
    try:
        assert loc.get_language_instruction() == loc._languages[ui]['llmInstruction']
        # bahasa keluaran tetap 'id' walau locale UI en/zh
        assert loc.get_output_language() == 'id'
        assert 'Bahasa Indonesia' in loc.get_output_language_instruction()
    finally:
        loc.set_locale('zh')


def test_output_instruction_param_and_env_override(monkeypatch):
    loc.set_locale('en')
    try:
        monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'zh')
        assert loc.get_output_language_instruction() == loc._languages['zh']['llmInstruction']
        loc.set_output_language('en')
        assert loc.get_output_language_instruction() == loc._languages['en']['llmInstruction']
    finally:
        loc.set_locale('zh')


# ---- M1: isolasi antar thread + jatuh ke env/default tanpa set ----
def test_threads_do_not_leak_output_language():
    import threading
    got = {}
    barrier = threading.Barrier(2)

    def worker(name, lang):
        loc.set_output_language(lang)
        barrier.wait()
        got[name] = loc.get_output_language()
        barrier.wait()
        loc.set_output_language(None)

    ts = [threading.Thread(target=worker, args=('a', 'en')), threading.Thread(target=worker, args=('b', 'zh'))]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert got == {'a': 'en', 'b': 'zh'}
    assert loc.get_output_language() == 'id'  # thread utama tak tercemar


def test_new_thread_without_set_falls_to_env_default(monkeypatch):
    import threading
    loc.set_output_language('en')  # di thread utama
    res = []
    th = threading.Thread(target=lambda: res.append(loc.get_output_language()))
    th.start(); th.join()
    assert res == ['id']
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'zh')
    th = threading.Thread(target=lambda: res.append(loc.get_output_language()))
    th.start(); th.join()
    assert res[1] == 'zh'


# ---- M3/M4: tes perilaku pada pesan yang dikirim ke LLM ----
def test_fallback_outline_en_branch(monkeypatch):
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'en')
    llm = MagicMock()
    llm.chat_json.side_effect = RuntimeError("boom")
    outline = _agent(llm).plan_outline()
    assert outline.title == "Future Prediction Report"
    assert outline.sections[0].title == "Predicted Scenario and Key Findings"


def _zep_calls():
    """Jalankan 4 call site LLM di zep_tools dengan LLM palsu; kembalikan pesan system."""
    from app.services import zep_tools
    svc = zep_tools.ZepToolsService.__new__(zep_tools.ZepToolsService)
    llm = MagicMock()
    llm.chat_json.return_value = {"sub_queries": ["a"], "selected_indices": [0], "reasoning": "r", "questions": ["q"]}
    llm.chat.return_value = "ringkasan"
    svc._llm_client = llm
    profiles = [{"realname": "A", "profession": "p", "bio": "b"}]
    svc._generate_sub_queries("q", "req")
    svc._select_agents_for_interview(profiles, "need", "req", 1)
    svc._generate_interview_questions("need", "req", profiles)
    svc._generate_interview_summary(
        [zep_tools.AgentInterview(agent_name="A", agent_role="r", agent_bio="b", question="q", response="jawab")]
        if 'agent_bio' in zep_tools.AgentInterview.__dataclass_fields__ else
        [zep_tools.AgentInterview(agent_name="A", agent_role="r", question="q", response="jawab")], "need")
    return [c.kwargs['messages'][0]['content'] for c in llm.chat_json.call_args_list + llm.chat.call_args_list]


def test_zep_tools_all_llm_calls_carry_id_instruction_even_with_ui_locale_en():
    loc.set_locale('en')
    try:
        msgs = _zep_calls()
    finally:
        loc.set_locale('zh')
    ins = loc.get_output_language_instruction()
    assert len(msgs) == 4
    for m in msgs:
        assert m.startswith(ins) and m.endswith(ins), m[:80]


def test_zep_tools_without_set_output_language_falls_to_env_then_default(monkeypatch):
    # jalur interview_agents di API lain: tidak memanggil set_output_language
    assert all('Bahasa Indonesia' in m for m in _zep_calls())
    monkeypatch.setenv('REPORT_OUTPUT_LANGUAGE', 'en')
    assert all('Please respond in English' in m for m in _zep_calls())

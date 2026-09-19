"""
T5 — end-to-end proof that a Zep give-up produces an honest, usable report
instead of a permanently blocked generate_report() and a silently-missing
warning.

Covers:
- Full pipeline (mocked Zep API failing) -> updater give-up -> registry
  released -> report tagged ingestion_incomplete=True with a non-empty note,
  markdown_content carries a visible warning block.
- Reverse direction: full success -> no tag, no warning block, so healthy
  reports are not polluted with a false warning.
"""
from types import SimpleNamespace

from app.api import report as report_api
from app.services import zep_graph_memory_updater as updater_module
from app.services.report_agent import Report, ReportStatus
from app.services.zep_graph_memory_updater import (
    AgentActivity,
    ZepGraphMemoryManager,
)


def _activity(index=1):
    return AgentActivity(
        platform="twitter",
        agent_id=index,
        agent_name=f"Agent {index}",
        action_type="CREATE_POST",
        action_args={"content": "hello"},
        round_num=index,
        timestamp="2026-07-22T12:00:00+08:00",
    )


def _fake_report(simulation_id: str) -> Report:
    return Report(
        report_id="report-1",
        simulation_id=simulation_id,
        graph_id="graph-1",
        simulation_requirement="test",
        status=ReportStatus.COMPLETED,
        markdown_content="# Report\n\nbody text",
    )


def _cleanup(simulation_id: str):
    ZepGraphMemoryManager._updaters.pop(simulation_id, None)
    ZepGraphMemoryManager._incomplete_ingestions.pop(simulation_id, None)


def test_report_gets_honest_incomplete_note_when_zep_stays_down(monkeypatch):
    simulation_id = "sim-e2e-giveup"

    def always_fails(**_kwargs):
        raise RuntimeError("Zep Cloud unreachable")

    client = SimpleNamespace(
        graph=SimpleNamespace(
            add=always_fails,
            episode=SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(processed=True)),
        )
    )
    monkeypatch.setattr(updater_module, "get_zep_client", lambda _key: client)
    monkeypatch.setattr(updater_module.Config, "ZEP_API_KEY", "test-key")

    _cleanup(simulation_id)
    try:
        updater = ZepGraphMemoryManager.create_updater(simulation_id, "graph-1")
        updater.SEND_INTERVAL = 0
        updater.add_activity(_activity())

        # This must not raise, and must not leave the updater registered —
        # both are prerequisites for generate_report() to ever succeed again.
        ZepGraphMemoryManager.stop_updater(simulation_id)
        assert ZepGraphMemoryManager.get_updater(simulation_id) is None, (
            "updater still registered after give-up: generate_report() "
            "would keep returning 409 forever"
        )

        report = _fake_report(simulation_id)
        report_api._apply_ingestion_incomplete_note(report, simulation_id)

        # Print actual values, not just booleans, per the T5 evidence bar.
        print(f"report.ingestion_incomplete={report.ingestion_incomplete!r}")
        print(f"report.ingestion_note={report.ingestion_note!r}")

        assert report.ingestion_incomplete is True
        assert report.ingestion_note is not None and report.ingestion_note.strip() != ""
        assert "⚠️" in report.markdown_content
        assert report.markdown_content.endswith("# Report\n\nbody text")
    finally:
        _cleanup(simulation_id)


def test_report_has_no_warning_when_ingestion_fully_succeeds(monkeypatch):
    simulation_id = "sim-e2e-success"

    client = SimpleNamespace(
        graph=SimpleNamespace(
            add=lambda **_kwargs: SimpleNamespace(uuid_="episode-1"),
            episode=SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(processed=True)),
        )
    )
    monkeypatch.setattr(updater_module, "get_zep_client", lambda _key: client)
    monkeypatch.setattr(updater_module.Config, "ZEP_API_KEY", "test-key")

    _cleanup(simulation_id)
    try:
        updater = ZepGraphMemoryManager.create_updater(simulation_id, "graph-1")
        updater.SEND_INTERVAL = 0
        updater.add_activity(_activity())

        ZepGraphMemoryManager.stop_updater(simulation_id)
        assert ZepGraphMemoryManager.get_updater(simulation_id) is None

        original_markdown = "# Report\n\nbody text"
        report = _fake_report(simulation_id)
        report.markdown_content = original_markdown
        report_api._apply_ingestion_incomplete_note(report, simulation_id)

        print(f"report.ingestion_incomplete={report.ingestion_incomplete!r}")
        print(f"report.ingestion_note={report.ingestion_note!r}")

        assert report.ingestion_incomplete is False
        assert report.ingestion_note is None
        assert report.markdown_content == original_markdown
        assert "⚠️" not in report.markdown_content
    finally:
        _cleanup(simulation_id)

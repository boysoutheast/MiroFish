from types import SimpleNamespace
import threading
from queue import Queue

import pytest

from app.services import zep_graph_memory_updater as updater_module
from app.services.zep_graph_memory_updater import (
    AgentActivity,
    ZepGraphMemoryManager,
    ZepGraphMemoryUpdater,
)


def _activity(index=1, content="hello"):
    return AgentActivity(
        platform="twitter",
        agent_id=index,
        agent_name=f"Agent {index}",
        action_type="CREATE_POST",
        action_args={"content": content},
        round_num=index,
        timestamp="2026-07-22T12:00:00+08:00",
    )


def _client(add):
    return SimpleNamespace(
        graph=SimpleNamespace(
            add=add,
            episode=SimpleNamespace(
                get=lambda **_kwargs: SimpleNamespace(processed=True)
            ),
        )
    )


def _updater(monkeypatch, add, simulation_id="sim-1"):
    client = _client(add)
    monkeypatch.setattr(updater_module, "get_zep_client", lambda _key: client)
    updater = ZepGraphMemoryUpdater(
        "graph-1",
        api_key="test-key",
        simulation_id=simulation_id,
    )
    updater.SEND_INTERVAL = 0
    return updater


def test_stop_drains_an_immediately_queued_tail_activity(monkeypatch):
    writes = []
    updater = _updater(
        monkeypatch,
        lambda **kwargs: writes.append(kwargs) or SimpleNamespace(uuid_="episode-1"),
    )

    updater.start()
    updater.add_activity(_activity())
    updater.stop()

    assert len(writes) == 1
    assert updater.get_stats()["items_sent"] == 1
    assert updater.get_stats()["queue_size"] == 0


def test_network_write_happens_outside_the_buffer_lock(monkeypatch):
    lock_was_available = []
    updater = None

    def add(**_kwargs):
        acquired = updater._buffer_lock.acquire(blocking=False)
        lock_was_available.append(acquired)
        if acquired:
            updater._buffer_lock.release()
        return SimpleNamespace(uuid_="episode-1")

    updater = _updater(monkeypatch, add)
    updater.start()
    for index in range(updater.BATCH_SIZE):
        updater.add_activity(_activity(index))
    updater.stop()

    assert lock_was_available == [True]


def test_activity_episode_has_provenance_time_and_a_safe_size(monkeypatch):
    writes = []
    updater = _updater(
        monkeypatch,
        lambda **kwargs: writes.append(kwargs) or SimpleNamespace(uuid_="episode-1"),
        simulation_id="sim-provenance",
    )

    updater._send_batch_activities(
        [_activity(content="x" * 20_000)],
        "twitter",
    )

    assert len(writes) == 1
    write = writes[0]
    assert len(write["data"]) <= updater.MAX_EPISODE_CHARS
    assert write["created_at"] == "2026-07-22T12:00:00+08:00"
    assert write["source_description"] == "MiroFish simulation activity batch"
    assert write["metadata"]["simulation_id"] == "sim-provenance"
    assert write["metadata"]["platform"] == "twitter"
    assert write["metadata"]["activity_count"] == 1


def test_failed_non_idempotent_write_is_reported_by_stop_without_raising(monkeypatch):
    """A batch that fails to send (e.g. Zep down) must not raise. stop() gives
    up cleanly and reports the outcome in a ZepUpdaterStopResult instead —
    exceptions are for bugs, not for an expected upstream outage."""

    def add(**_kwargs):
        raise RuntimeError("write failed")

    updater = _updater(monkeypatch, add)
    updater.start()
    updater.add_activity(_activity())

    result = updater.stop()

    assert isinstance(result, updater_module.ZepUpdaterStopResult)
    assert result.incomplete is True
    assert result.failed_batch_count == 1
    assert "write failed" in (result.detail or "")
    assert updater.get_stats()["failed_count"] == 1


def test_stop_waits_for_already_sent_episodes_before_giving_up(monkeypatch):
    """Reproduces the ordering bug directly: one activity succeeds (its
    episode goes into _pending_episode_uuids), a second fails. stop() must
    still call _wait_for_pending_episodes for the successful one BEFORE
    deciding to give up because of the failed one — proven here via a spy
    that records call order, not just via the final result."""

    call_order = []

    def add(**kwargs):
        if kwargs["metadata"]["agent_ids"] == "1":
            return SimpleNamespace(uuid_="episode-ok")
        raise RuntimeError("zep unreachable")

    updater = _updater(monkeypatch, add)
    updater.BATCH_SIZE = 1
    updater.start()
    updater.add_activity(_activity(index=1))
    updater.add_activity(_activity(index=2))

    original_wait = updater._wait_for_pending_episodes

    def spy_wait(**kwargs):
        call_order.append("wait_for_pending_episodes")
        return original_wait(**kwargs)

    monkeypatch.setattr(updater, "_wait_for_pending_episodes", spy_wait)

    result = updater.stop()

    assert call_order == ["wait_for_pending_episodes"]
    assert result.incomplete is True
    assert result.failed_batch_count == 1
    # The successfully-sent episode was waited on and cleared, not discarded.
    assert updater._pending_episode_uuids == []


def test_stop_updater_releases_registry_entry_on_give_up(monkeypatch):
    """This is the actual bug: before this fix, a give-up left the updater
    registered forever, so generate_report() kept refusing with 409. After
    the fix, ZepGraphMemoryManager.get_updater(sim_id) must return None once
    stop_updater() has run, even though ingestion gave up."""

    def add(**_kwargs):
        raise RuntimeError("zep down")

    simulation_id = "sim-giveup"
    client = _client(add)
    monkeypatch.setattr(updater_module, "get_zep_client", lambda _key: client)
    monkeypatch.setattr(updater_module.Config, "ZEP_API_KEY", "test-key")

    ZepGraphMemoryManager._updaters.pop(simulation_id, None)
    ZepGraphMemoryManager._incomplete_ingestions.pop(simulation_id, None)
    try:
        updater = ZepGraphMemoryManager.create_updater(simulation_id, "graph-1")
        updater.SEND_INTERVAL = 0
        updater.add_activity(_activity())

        result = ZepGraphMemoryManager.stop_updater(simulation_id)

        assert result.incomplete is True
        assert ZepGraphMemoryManager.get_updater(simulation_id) is None
        assert ZepGraphMemoryManager.get_incomplete_ingestion_detail(
            simulation_id
        ) is not None
    finally:
        ZepGraphMemoryManager._updaters.pop(simulation_id, None)
        ZepGraphMemoryManager._incomplete_ingestions.pop(simulation_id, None)


def test_stop_updater_clears_incomplete_marker_on_success(monkeypatch):
    """The reverse direction: a fully successful drain must not leave a
    stale incomplete marker (e.g. from a previous failed run of the same
    simulation_id) lying around for the next report."""

    simulation_id = "sim-success"
    client = _client(lambda **_kwargs: SimpleNamespace(uuid_="episode-1"))
    monkeypatch.setattr(updater_module, "get_zep_client", lambda _key: client)
    monkeypatch.setattr(updater_module.Config, "ZEP_API_KEY", "test-key")

    ZepGraphMemoryManager._updaters.pop(simulation_id, None)
    ZepGraphMemoryManager._incomplete_ingestions[simulation_id] = "stale from last run"
    try:
        updater = ZepGraphMemoryManager.create_updater(simulation_id, "graph-1")
        updater.SEND_INTERVAL = 0
        assert ZepGraphMemoryManager.get_incomplete_ingestion_detail(
            simulation_id
        ) is None  # cleared by create_updater()

        updater.add_activity(_activity())
        result = ZepGraphMemoryManager.stop_updater(simulation_id)

        assert result.incomplete is False
        assert ZepGraphMemoryManager.get_updater(simulation_id) is None
        assert ZepGraphMemoryManager.get_incomplete_ingestion_detail(
            simulation_id
        ) is None
    finally:
        ZepGraphMemoryManager._updaters.pop(simulation_id, None)
        ZepGraphMemoryManager._incomplete_ingestions.pop(simulation_id, None)


def test_failed_simulation_action_is_not_ingested(monkeypatch):
    updater = _updater(
        monkeypatch,
        lambda **_kwargs: SimpleNamespace(uuid_="unused"),
    )

    updater.add_activity_from_dict(
        {
            "agent_id": 1,
            "agent_name": "Agent",
            "action_type": "CREATE_POST",
            "action_args": {"content": "not actually posted"},
            "success": False,
        },
        "twitter",
    )

    assert updater.get_stats()["queue_size"] == 0
    assert updater.get_stats()["skipped_count"] == 1


def test_stop_cannot_finish_between_acceptance_check_and_enqueue(monkeypatch):
    writes = []
    updater = _updater(
        monkeypatch,
        lambda **kwargs: writes.append(kwargs) or SimpleNamespace(uuid_="episode-1"),
    )

    put_entered = threading.Event()
    allow_put = threading.Event()

    class BlockingQueue(Queue):
        def put(self, item, block=True, timeout=None):
            put_entered.set()
            assert allow_put.wait(timeout=2)
            return super().put(item, block=block, timeout=timeout)

    updater._activity_queue = BlockingQueue()
    updater.start()
    producer = threading.Thread(target=updater.add_activity, args=(_activity(),))
    producer.start()
    assert put_entered.wait(timeout=1)

    stopper = threading.Thread(target=updater.stop)
    stopper.start()
    stopper.join(timeout=0.1)
    assert stopper.is_alive()

    allow_put.set()
    producer.join(timeout=2)
    stopper.join(timeout=2)

    assert not producer.is_alive()
    assert not stopper.is_alive()
    assert len(writes) == 1


def test_pending_episode_wait_has_a_deadline(monkeypatch):
    updater = _updater(
        monkeypatch,
        lambda **_kwargs: SimpleNamespace(uuid_="episode-1"),
    )
    updater._pending_episode_uuids = ["episode-1"]
    updater.client.graph.episode.get = lambda **_kwargs: SimpleNamespace(
        processed=False
    )
    timestamps = iter([0.0, 2.0])
    monkeypatch.setattr(updater_module, "ZEP_INGESTION_WAIT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(updater_module.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(updater_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="pending"):
        updater._wait_for_pending_episodes()


def test_explicit_graph_destruction_can_discard_a_stopped_failed_updater():
    updater = SimpleNamespace(
        graph_id="graph-1",
        _running=False,
        _worker_thread=SimpleNamespace(is_alive=lambda: False),
    )
    ZepGraphMemoryManager._updaters["sim-failed"] = updater
    try:
        assert ZepGraphMemoryManager.discard_inactive_updater("sim-failed") is True
        assert "sim-failed" not in ZepGraphMemoryManager._updaters
    finally:
        ZepGraphMemoryManager._updaters.pop("sim-failed", None)


def test_flush_deadline_keeps_unattempted_platform_for_a_safe_retry(monkeypatch):
    now = [0.0]
    writes = []

    def add(**kwargs):
        writes.append(kwargs)
        now[0] = 2.0
        return SimpleNamespace(uuid_=f"episode-{len(writes)}")

    updater = _updater(monkeypatch, add)
    updater._platform_buffers["twitter"] = [_activity(1)]
    reddit_activity = _activity(2)
    reddit_activity.platform = "reddit"
    updater._platform_buffers["reddit"] = [reddit_activity]
    monkeypatch.setattr(updater_module.time, "time", lambda: now[0])

    with pytest.raises(TimeoutError, match="deadline"):
        updater._flush_remaining(deadline=1.0)

    assert updater._platform_buffers["twitter"] == []
    assert updater._platform_buffers["reddit"] == [reddit_activity]

    now[0] = 0.0
    updater._flush_remaining(deadline=1.0)
    assert updater._platform_buffers["reddit"] == []
    assert len(writes) == 2

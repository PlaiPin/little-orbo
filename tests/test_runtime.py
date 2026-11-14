import pytest

from orbo_runtime.broker import Broker, FakeRobot
from orbo_runtime.contracts import ActionResult, ManualClock, Rejected
from orbo_runtime.reminders import Reminders
from orbo_runtime.storage import Store


@pytest.fixture
def runtime(tmp_path):
    clock = ManualClock()
    store = Store(tmp_path / "journal.db", clock)
    episode = store.episode("owner", provenance="synthetic")
    robot = FakeRobot()
    store.grant("owner", "robot.command", robot.robot_id, True, clock.utc() + 3600)
    broker = Broker(store, robot)
    epoch = broker.acquire("owner")
    yield store, Reminders(store), broker, robot, episode, epoch
    store.close()


async def ready(runtime):
    s, r, b, robot, ep, epoch = runtime
    result = r.create("owner", ep, "request", "stretch", s.clock.utc())
    due = r.claim_due()
    action = await b.dispatch(
        "owner", ep, "gesture", "attention.gesture", {"gesture": "attention"}, epoch
    )
    token = r.await_ack("owner", result["reminder_id"], due["generation"], action["id"])
    return result["reminder_id"], due["generation"], token


async def test_complete_reminder_has_playback_evidence(runtime):
    s, r, b, robot, ep, epoch = runtime
    rid, generation, token = await ready(runtime)
    assert r.acknowledge("owner", rid, token) == ("stretch", generation)
    r.playback_result("owner", rid, generation, True, 2400)
    assert r.get("owner", rid)["state"] == "delivered"
    s.end_episode(ep, "owner", "completed")
    kinds = [
        row[0]
        for row in s.db.execute(
            "SELECT kind FROM events WHERE episode_id=? ORDER BY seq", (ep,)
        )
    ]
    assert kinds == [
        "episode.started",
        "reminder.saved",
        "reminder.due",
        "action.intent",
        "action.completed",
        "attention.awaiting_ack",
        "attention.acknowledged",
        "speech.playback",
        "episode.ended",
    ]


def test_durable_idempotency_and_changed_body(runtime):
    s, r, b, robot, ep, epoch = runtime
    due = s.clock.utc() + 5
    first = r.create("owner", ep, "one", "stretch", due)
    assert r.create("owner", ep, "one", "stretch", due) == first
    with pytest.raises(Rejected):
        r.create("owner", ep, "one", "different", due)
    assert s.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
    assert (
        s.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='reminder.saved'"
        ).fetchone()[0]
        == 1
    )


async def test_stale_ack_and_late_playback_cannot_revive_cancelled(runtime):
    s, r, b, robot, ep, epoch = runtime
    rid, generation, token = await ready(runtime)
    r.change("owner", rid, "cancel")
    with pytest.raises(Rejected):
        r.acknowledge("owner", rid, token)
    with pytest.raises(Rejected):
        r.playback_result("owner", rid, generation, True, 100)
    assert r.get("owner", rid)["state"] == "cancelled"


async def test_no_ack_defers_and_old_ack_is_rejected(runtime):
    s, r, b, robot, ep, epoch = runtime
    rid, generation, token = await ready(runtime)
    s.clock.advance(31)
    r.expire_acknowledgments()
    assert r.get("owner", rid)["state"] == "pending"
    assert r.get("owner", rid)["due"] == s.clock.utc() + 300
    with pytest.raises(Rejected):
        r.acknowledge("owner", rid, token)


async def test_duplicate_command_does_not_repeat_effect(runtime):
    s, r, b, robot, ep, epoch = runtime
    args = ("owner", ep, "x", "attention.gesture", {"gesture": "attention"}, epoch)
    first = await b.dispatch(*args)
    assert (await b.dispatch(*args))["id"] == first["id"]
    assert robot.dispatches == 1


async def test_lost_feedback_holds_resource_until_reconciliation(runtime):
    s, r, b, robot, ep, epoch = runtime
    robot.lose_feedback = True
    first = await b.dispatch(
        "owner", ep, "x", "attention.gesture", {"gesture": "attention"}, epoch
    )
    with pytest.raises(Rejected):
        await b.dispatch(
            "owner", ep, "y", "attention.gesture", {"gesture": "attention"}, epoch
        )
    s.recover()
    assert b.command(first["id"])["state"] == "unknown"
    assert not b.result(ActionResult(first["id"], "completed", "simulation"))
    assert (await b.reconcile(first["id"]))["state"] == "accepted"


async def test_cancel_acceptance_is_not_cessation(runtime):
    s, r, b, robot, ep, epoch = runtime
    robot.lose_feedback = True
    action = await b.dispatch(
        "owner", ep, "x", "attention.gesture", {"gesture": "attention"}, epoch
    )

    async def cancel(command_id):
        return ActionResult(command_id, "accepted", "simulation")

    robot.cancel = cancel
    assert (await b.cancel("owner", action["id"]))["state"] == "cancel_requested"
    assert not b.result(ActionResult(action["id"], "completed", "simulation"))


async def test_epoch_and_monotonic_expiry_fence_owner(runtime):
    s, r, b, robot, ep, epoch = runtime
    b.acquire("owner")
    with pytest.raises(Rejected):
        await b.dispatch(
            "owner", ep, "x", "attention.gesture", {"gesture": "attention"}, epoch
        )
    current = b.acquire("owner")
    s.clock.advance(31)
    s.clock.now -= 3600  # NTP/wall-clock rollback must not renew authority.
    with pytest.raises(Rejected):
        b.check_lease("owner", current)


async def test_restart_is_unknown_and_never_replays_speech(runtime, tmp_path):
    s, r, b, robot, ep, epoch = runtime
    rid, generation, token = await ready(runtime)
    r.acknowledge("owner", rid, token)
    reopened = Store(tmp_path / "journal.db", s.clock)
    reopened.recover()
    assert Reminders(reopened).get("owner", rid)["state"] == "unknown"
    assert Reminders(reopened).claim_due() is None
    reopened.close()


def test_preferences_require_permission_and_version(runtime):
    s, r, b, robot, ep, epoch = runtime
    with pytest.raises(Rejected):
        r.set_preference("owner", ep, "attention", "silent", 0, "a")
    s.grant("owner", "preferences.write", "local", True, s.clock.utc() + 10)
    assert r.set_preference("owner", ep, "attention", "silent", 0, "a")["version"] == 1
    assert r.set_preference("owner", ep, "attention", "silent", 0, "a")["version"] == 1
    with pytest.raises(Rejected):
        r.set_preference("owner", ep, "attention", "gesture_then_speech", 0, "b")


def test_atomic_rollback(runtime):
    s, r, b, robot, ep, epoch = runtime
    with pytest.raises(RuntimeError):
        with s.transaction():
            s.event(ep, "test.event", {"must": "rollback"})
            raise RuntimeError()
    assert (
        s.db.execute("SELECT COUNT(*) FROM events WHERE kind='test.event'").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -1])
def test_invalid_due_values(runtime, value):
    s, r, b, robot, ep, epoch = runtime
    with pytest.raises(Rejected):
        r.create("owner", ep, "x", "stretch", value)


async def test_lease_revocation_interrupts_inflight_action(runtime):
    import asyncio

    s, _, broker, robot, episode, epoch = runtime
    entered = asyncio.Event()

    async def dispatch(action):
        entered.set()
        await asyncio.Event().wait()

    stops = []

    async def stop(reason):
        stops.append(reason)

    robot.dispatch = dispatch
    robot.controlled_stop = stop
    work = asyncio.create_task(
        broker.dispatch(
            "owner",
            episode,
            "inflight",
            "attention.gesture",
            {"gesture": "attention"},
            epoch,
        )
    )
    await entered.wait()
    s.grant("owner", "robot.command", robot.robot_id, False, s.clock.utc() + 60)
    with pytest.raises(Rejected):
        await asyncio.wait_for(work, 0.5)
    assert s.db.execute("SELECT state FROM commands").fetchone()[0] == "unknown"
    assert stops


async def test_conversation_restores_only_completed_history(runtime):
    from orbo_runtime.coordinator import Coordinator
    from orbo_runtime.providers import VerifiedReply

    s, _, broker, _, episode, _ = runtime
    s.grant("owner", "cloud.reasoning", "near", True, s.clock.utc() + 60)
    calls = []

    class Reasoner:
        async def complete(self, messages, tools):
            calls.append(list(messages))
            return VerifiedReply(
                {"role": "assistant", "content": "Remembered reply"}, {}, {}
            )

    await Coordinator(s, broker, Reasoner()).converse("owner", episode, "Prior request")
    # Recreate the coordinator to exercise persisted history instead of process memory.
    coordinator = Coordinator(s, broker, Reasoner())
    s.event(episode, "turn.started", {"turn_id": "failed", "text": "Unfinished turn"})
    await coordinator.converse("owner", episode, "Follow-up request")
    assert {"role": "user", "content": "Prior request"} in calls[-1]
    assert {"role": "assistant", "content": "Remembered reply"} in calls[-1]
    assert all(m["content"] != "Unfinished turn" for m in calls[-1])

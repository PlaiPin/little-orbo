import asyncio

import pytest

from orbo_runtime.broker import Broker, FakeRobot
from orbo_runtime.contracts import ActionResult, Capability, ManualClock, Rejected
from orbo_runtime.embodied_tasks import PlaceRegistry, TaskNavigator, TaskService
from orbo_runtime.storage import Store


class NavigatingRobot(FakeRobot):
    capabilities = {
        "navigation.named_goal": Capability(
            "navigation.named_goal", ("wheels",), 600, ("simulation",), False
        )
    }


@pytest.fixture
def core(tmp_path):
    store = Store(tmp_path / "journal.db", ManualClock())
    tasks = TaskService(store)
    robot = NavigatingRobot()
    broker = Broker(store, robot)
    for scope, dest in (("task.execute", "lab"), ("robot.command", robot.robot_id)):
        store.grant("owner", scope, dest, True, store.clock.utc() + 3600)
    tasks.places.register(
        "owner",
        "lab",
        "map-a",
        "assembly shelf",
        aliases=["shelf"],
        viewpoints=["shelf_front", "shelf_left"],
        visibility_limits="lower shelf only",
    )
    request = dict(
        kind="inspect",
        environment="lab",
        map_version="map-a",
        purpose="owner inspection",
        target="red wrench",
        places=["shelf"],
        allowed_actions=["navigate", "capture", "infer", "report"],
        budget=dict(seconds=300, observations=3, provider_calls=3),
        destination="local",
        provenance="synthetic",
    )
    yield store, tasks, broker, robot, request
    store.close()


def test_admission_is_atomic_idempotent_scoped_and_exclusive(core):
    store, tasks, _, _, req = core
    first = tasks.admit("owner", "one", req)
    assert tasks.admit("owner", "one", req)["id"] == first["id"]
    with pytest.raises(Rejected, match="idempotency"):
        tasks.admit("owner", "one", req | {"target": "blue wrench"})
    with pytest.raises(Rejected, match="physical task active"):
        tasks.admit("owner", "two", req)
    with pytest.raises(Rejected, match="permission"):
        tasks.admit("stranger", "one", req)
    assert store.db.execute("SELECT count(*) FROM embodied_tasks").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1


def test_recall_can_run_while_physical_task_active(core):
    _, tasks, _, _, req = core
    tasks.admit("owner", "one", req)
    assert (
        tasks.admit(
            "owner",
            "two",
            req | {"kind": "recall", "allowed_actions": ["recall", "report"]},
        )["state"]
        == "accepted"
    )


def test_places_reject_ambiguity_maps_and_access(core):
    store, tasks, _, _, _ = core
    places = PlaceRegistry(store)
    assert places.resolve("owner", "lab", "map-a", "SHELF")["viewpoints"] == [
        "shelf_front",
        "shelf_left",
    ]
    for scope in (
        ("other", "lab", "map-a"),
        ("owner", "home", "map-a"),
        ("owner", "lab", "map-b"),
    ):
        with pytest.raises(Rejected):
            places.resolve(*scope, "shelf")
    places.register(
        "owner", "lab", "map-a", "other shelf", aliases=["shelf"], viewpoints=["other"]
    )
    with pytest.raises(Rejected, match="ambiguous"):
        tasks.places.resolve("owner", "lab", "map-a", "shelf")
    places.register(
        "owner", "lab", "map-a", "locked", viewpoints=["locked"], accessible=False
    )
    with pytest.raises(Rejected, match="inaccessible"):
        places.resolve("owner", "lab", "map-a", "locked")


def test_budget_and_revision_fence_without_refunding_work(core):
    _, tasks, _, _, req = core
    task = tasks.admit("owner", "one", req)
    op = tasks.reserve_operation(
        "owner", task["id"], 1, "view", "capture", {}, observations=1
    )
    assert (
        tasks.reserve_operation(
            "owner", task["id"], 1, "view", "capture", {}, observations=1
        )["id"]
        == op["id"]
    )
    revised = tasks.revise("owner", task["id"], 1, {"target": "blue wrench"})
    assert revised["revision"] == 2 and revised["state"] == "accepted"
    assert not tasks.finish_operation(
        "owner", op["id"], 1, "completed", {"claim": "stale"}
    )
    assert revised["used"]["observations"] == 1
    with pytest.raises(Rejected, match="stale"):
        tasks.reserve_operation("owner", task["id"], 1, "another", "capture", {})


def test_time_and_count_budgets_reject_before_effect(core):
    store, tasks, _, _, req = core
    task = tasks.admit("owner", "one", req)
    with pytest.raises(Rejected, match="budget"):
        tasks.reserve_operation(
            "owner", task["id"], 1, "view", "capture", {}, observations=4
        )
    assert tasks.get("owner", task["id"])["used"]["observations"] == 0
    store.clock.advance(301)
    store.clock.now -= 1000
    with pytest.raises(Rejected, match="deadline"):
        tasks.reserve_operation("owner", task["id"], 1, "view", "capture", {})


async def test_navigation_reuses_broker_without_duplicate_motion(core):
    _, tasks, broker, robot, req = core
    task = tasks.admit("owner", "one", req)
    nav = TaskNavigator(tasks, broker)
    result = await nav.navigate("owner", task["id"], 1, "shelf", "move", duration=120)
    assert result["state"] == "completed"
    assert result["result"]["evidence"] == "simulation"
    assert (await nav.navigate("owner", task["id"], 1, "shelf", "move", duration=120))[
        "id"
    ] == result["id"]
    assert robot.dispatches == 1
    assert broker.lease_deadline <= tasks.store.clock.mono() + 2
    # Navigation arrival never completes an inspection.
    assert tasks.get("owner", task["id"])["state"] == "working"


async def test_navigation_rejects_out_of_scope_view_before_motion(core):
    _, tasks, broker, robot, req = core
    task = tasks.admit("owner", "one", req)
    nav = TaskNavigator(tasks, broker)
    with pytest.raises(Rejected, match="viewpoint"):
        await nav.navigate(
            "owner", task["id"], 1, "shelf", "move", viewpoint="arbitrary"
        )
    assert robot.dispatches == 0


async def test_correction_during_motion_stops_and_fences_late_result(core):
    _, tasks, broker, robot, req = core
    task = tasks.admit("owner", "one", req)
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def dispatch(action):
        entered.set()
        await asyncio.Event().wait()

    async def stop(reason):
        stopped.set()

    robot.dispatch, robot.controlled_stop = dispatch, stop
    nav = TaskNavigator(tasks, broker)
    running = asyncio.create_task(
        nav.navigate("owner", task["id"], 1, "shelf", "move", duration=120)
    )
    await entered.wait()
    tasks.revise("owner", task["id"], 1, {"target": "blue wrench"})
    with pytest.raises(Rejected, match="stale"):
        await asyncio.wait_for(running, 1)
    assert stopped.is_set()
    assert tasks.get("owner", task["id"])["state"] == "paused"
    assert (
        tasks.store.db.execute("SELECT state FROM commands").fetchone()[0] == "unknown"
    )


async def test_cancel_local_without_provider_preserves_stop_uncertainty(core):
    _, tasks, broker, robot, req = core
    task = tasks.admit("owner", "one", req)
    entered = asyncio.Event()

    async def dispatch(action):
        entered.set()
        await asyncio.Event().wait()

    robot.dispatch = dispatch
    nav = TaskNavigator(tasks, broker)
    running = asyncio.create_task(nav.navigate("owner", task["id"], 1, "shelf", "move"))
    await entered.wait()
    cancelled = await nav.cancel("owner", task["id"], 1)
    assert cancelled["state"] == "cancelled"
    assert running.cancelled()
    assert (
        tasks.store.db.execute("SELECT state FROM commands").fetchone()[0] == "unknown"
    )
    assert tasks.store.db.execute("SELECT command_id FROM task_operations").fetchone()[
        0
    ]


async def test_teleop_lease_conflict_does_not_dispatch(core):
    store, tasks, broker, robot, req = core
    store.grant(
        "teleop", "robot.command", robot.robot_id, True, store.clock.utc() + 3600
    )
    broker.acquire("teleop")
    task = tasks.admit("owner", "one", req)
    with pytest.raises(Rejected, match="already owned"):
        await TaskNavigator(tasks, broker).navigate(
            "owner", task["id"], 1, "shelf", "move"
        )
    assert robot.dispatches == 0


def test_restart_fences_task_and_does_not_replay(core):
    store, tasks, _, robot, req = core
    task = tasks.admit("owner", "one", req)
    tasks.reserve_operation(
        "owner", task["id"], 1, "move", "navigate", {"waypoint": "shelf_front"}
    )
    store.recover()
    restored = TaskService(store)
    assert restored.get("owner", task["id"])["state"] == "unknown"
    with pytest.raises(Rejected):
        restored.check_revision("owner", task["id"], 1)
    assert robot.dispatches == 0
    assert (
        store.db.execute("SELECT state FROM task_operations").fetchone()[0] == "unknown"
    )


async def test_lost_feedback_reconciles_without_redispatch(core):
    store, tasks, broker, robot, req = core
    robot.lose_feedback = True
    task = tasks.admit("owner", "one", req)
    nav = TaskNavigator(tasks, broker)
    op = await nav.navigate("owner", task["id"], 1, "shelf", "move")
    assert op["state"] == "unknown"
    with pytest.raises(Rejected, match="reconcile"):
        nav.resume("owner", task["id"], 1)
    robot.results[op["command_id"]] = ActionResult(
        op["command_id"], "cancelled", "simulation"
    )
    await nav.reconcile("owner", task["id"])
    assert nav.resume("owner", task["id"], 1)["state"] == "accepted"
    assert robot.dispatches == 1


def test_terminal_task_closes_exportable_episode(core):
    store, tasks, _, _, req = core
    task = tasks.admit("owner", "one", req)
    tasks.transition("owner", task["id"], 1, "completed", {"answer": "unknown"})
    assert (
        store.db.execute(
            "SELECT state FROM episodes WHERE id=?", (task["episode_id"],)
        ).fetchone()[0]
        == "completed"
    )


async def test_supervision_renews_short_authority_and_revocation_stops(core):
    store, tasks, broker, robot, req = core
    task = tasks.admit("owner", "one", req)
    leases = []

    async def dispatch(action):
        for _ in range(6):
            await asyncio.sleep(0.04)
            store.clock.advance(1)
            await asyncio.sleep(0.04)
            lease = store.db.execute("SELECT expires FROM leases").fetchone()[0]
            leases.append(lease - store.clock.utc())
        store.grant("owner", "task.execute", "lab", False, store.clock.utc() + 300)
        await asyncio.Event().wait()

    robot.dispatch = dispatch
    with pytest.raises(Rejected, match="permission"):
        await TaskNavigator(tasks, broker).navigate(
            "owner", task["id"], 1, "shelf", "move", duration=120
        )
    assert len(leases) == 6 and all(0 < ttl <= 2 for ttl in leases)
    assert store.db.execute("SELECT state FROM commands").fetchone()[0] == "unknown"


def test_navigation_feedback_rejects_stale_pose_and_wrong_frame():
    import threading
    from collections import deque
    from types import SimpleNamespace as NS
    from orbo_runtime.navigation import Navigation

    nav = Navigation.__new__(Navigation)
    nav.adapter = NS(
        node=NS(get_clock=lambda: NS(now=lambda: NS(nanoseconds=10_000_000_000)))
    )
    nav.observation_lock = threading.Lock()
    nav.feedback = deque(maxlen=64)
    nav.last_feedback_mono = 0
    pose = NS(
        header=NS(stamp=NS(sec=5, nanosec=0), frame_id="map"),
        pose=NS(position=NS(x=0.0, y=0.0), orientation=NS(z=0.0, w=1.0)),
    )
    feedback = NS(
        feedback=NS(current_pose=pose, distance_remaining=1.0, number_of_recoveries=0)
    )
    nav._feedback(feedback)
    assert not nav.feedback
    pose.header.stamp.sec = 10
    pose.header.frame_id = "odom"
    nav._feedback(feedback)
    assert not nav.feedback
    pose.header.frame_id = "map"
    nav._feedback(feedback)
    assert len(nav.feedback) == 1


async def test_recall_cancellation_does_not_stop_another_task(core):
    store, tasks, broker, robot, req = core
    physical = tasks.admit("owner", "physical", req)
    recall = tasks.admit(
        "owner",
        "memory",
        req | {"kind": "recall", "allowed_actions": ["recall", "report"]},
    )
    stops = []

    async def stop(reason):
        stops.append(reason)

    robot.controlled_stop = stop
    await TaskNavigator(tasks, broker).cancel("owner", recall["id"], 1)
    assert not stops
    assert tasks.get("owner", physical["id"])["state"] == "accepted"

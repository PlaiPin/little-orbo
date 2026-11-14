import pytest

from orbo_runtime.contracts import ManualClock, Rejected
from orbo_runtime.embodied_metrics import distribution, report
from orbo_runtime.embodied_tasks import TaskService
from orbo_runtime.storage import Store


@pytest.fixture
def journal():
    store = Store(":memory:", ManualClock())
    for owner in ("owner", "other"):
        store.grant(owner, "task.execute", "lab", True, store.clock.utc() + 3600)
    yield store, TaskService(store)
    store.close()


def task(tasks, key, *, owner="owner", provenance="synthetic"):
    return tasks.admit(
        owner,
        key,
        {
            "kind": "recall",
            "environment": "lab",
            "map_version": "map",
            "target": "private target",
            "purpose": "metrics test",
            "places": [],
            "allowed_actions": ["recall", "report"],
            "budget": {"seconds": 300, "observations": 0, "provider_calls": 0},
            "destination": "local",
            "provenance": provenance,
        },
    )


def event(store, task, kind, **payload):
    return store.event(
        task["episode_id"],
        kind,
        {"task_id": task["id"], "revision": task["revision"], **payload},
    )


def terminal(tasks, task, state="completed", *, partial=False):
    tasks.transition(
        task["owner"], task["id"], task["revision"], state, {"partial": partial}
    )


def test_acknowledgment_is_separate_from_meaningful_result_and_no_acoustic_claim(
    journal,
):
    store, tasks = journal
    current = task(tasks, "one")
    store.clock.advance(1)
    event(
        store, current, "task.voice_delivery", state="submitted", update_kind="progress"
    )
    store.clock.advance(9)
    terminal(tasks, current)
    store.clock.advance(2)
    event(
        store, current, "task.voice_delivery", state="submitted", update_kind="result"
    )
    store.clock.advance(5)
    event(
        store,
        current,
        "task.voice_delivery",
        state="context_accepted",
        update_kind="result",
    )
    result = report(store, "owner")
    sample = result["samples"][0]
    assert sample["timings"]["acknowledgment_seconds"] == 1
    assert sample["timings"]["meaningful_result_seconds"] == 12
    assert sample["timings"]["terminal_state_seconds"] == 10
    assert sample["spoken_result_seconds"] is None
    assert result["spoken_result_latency"] == "unmeasured"
    assert "private target" not in str(result)


def test_failure_unknown_and_missing_results_stay_in_denominators(journal):
    store, tasks = journal
    for index, state in enumerate(["completed", "failed", "unknown", "cancelled"]):
        current = task(tasks, str(index))
        store.clock.advance(index + 1)
        terminal(tasks, current, state, partial=state == "unknown")
        if state == "completed":
            event(store, current, "task.result_released")
    result = report(store, "owner")
    cohort = result["cohorts"][0]
    assert cohort["revision_samples"] == 4
    assert cohort["outcomes"] == {
        "completed": 1,
        "failed": 1,
        "unknown": 1,
        "cancelled": 1,
    }
    assert cohort["failed_or_unknown"] == 2
    assert cohort["partial_results"] == 1
    assert cohort["latencies"]["meaningful_result_seconds"]["count"] == 1
    assert cohort["latencies"]["meaningful_result_seconds"]["missing"] == 3
    assert len(result["samples"]) == 4


def test_p50_p95_include_observed_failure_results_and_quantile_definition(journal):
    store, tasks = journal
    for index, seconds in enumerate([1, 2, 3, 4]):
        current = task(tasks, str(index))
        store.clock.advance(seconds)
        terminal(tasks, current, "failed" if index == 3 else "completed")
        event(store, current, "task.result_released")
    summary = report(store, "owner")["cohorts"][0]
    metric = summary["latencies"]["meaningful_result_seconds"]
    assert metric["count"] == 4 and metric["missing"] == 0
    assert metric["p50"] == 2.5 and metric["p95"] == pytest.approx(3.85)
    assert summary["failed_or_unknown"] == 1
    assert distribution([None, float("nan"), -1])["count"] == 0


def test_cross_boot_or_clock_regression_is_not_repaired_using_wall_time(journal):
    store, tasks = journal
    current = task(tasks, "one")
    store.clock.advance(5)
    store.boot = "new-boot"
    terminal(tasks, current)
    event(store, current, "task.result_released")
    sample = report(store, "owner")["samples"][0]
    assert sample["timings"]["meaningful_result_seconds"] is None
    assert "meaningful_result:different_boot_domains" in sample["gaps"]
    store.db.execute(
        "UPDATE events SET boot='same',mono=CASE WHEN kind='task.accepted' THEN 100 ELSE 1 END"
    )
    sample = report(store, "owner")["samples"][0]
    assert sample["timings"]["meaningful_result_seconds"] is None
    assert "meaningful_result:nonmonotonic_event_order" in sample["gaps"]


def test_operation_work_kept_separate_from_end_to_end_latency(journal):
    store, tasks = journal
    current = task(tasks, "one")
    for capability, seconds in [
        ("navigate", 7),
        ("capture", 1),
        ("infer", 3),
        ("compare", 2),
    ]:
        event(
            store,
            current,
            "task.operation_admitted",
            operation_id=capability,
            capability=capability,
        )
        store.clock.advance(seconds)
        event(store, current, "task.operation_completed", operation_id=capability)
    terminal(tasks, current)
    store.clock.advance(2)
    event(store, current, "task.remote_delivery_selected")
    timings = report(store, "owner")["samples"][0]["timings"]
    assert timings["navigation_seconds"] == 7
    assert timings["capture_seconds"] == 1
    assert timings["provider_work_seconds"] == 5
    assert timings["meaningful_result_seconds"] == 15


def test_revisions_and_synthetic_physical_provenance_not_pooled(journal):
    store, tasks = journal
    current = task(tasks, "synthetic")
    store.clock.advance(1)
    current = tasks.revise("owner", current["id"], 1, {"target": "corrected target"})
    store.clock.advance(2)
    terminal(tasks, current)
    event(store, current, "task.result_released")
    physical = task(tasks, "physical", provenance="physical")
    store.clock.advance(100)
    terminal(tasks, physical)
    event(store, physical, "task.result_released")
    result = report(store, "owner")
    assert len(result["cohorts"]) == 2
    assert {
        sample["revision"]
        for sample in result["samples"]
        if sample["task_id"] == current["id"]
    } == {1, 2}
    synthetic = next(c for c in result["cohorts"] if c["provenance"] == "synthetic")
    assert synthetic["latencies"]["meaningful_result_seconds"]["p50"] == 2
    assert synthetic["latencies"]["meaningful_result_seconds"]["missing"] == 1
    assert synthetic["outcomes"]["superseded"] == 1


def test_scope_limits_and_incomplete_history_are_explicit(journal):
    store, tasks = journal
    other = task(tasks, "other", owner="other")
    with pytest.raises(Rejected, match="owned"):
        report(store, "owner", task_ids=[other["id"]])
    current = task(tasks, "own-one")
    terminal(tasks, current)
    event(store, current, "task.result_released")
    second = task(tasks, "own-two")
    assert report(store, "owner", max_tasks=1)["selection"]["truncated"] is True
    with pytest.raises(Rejected, match="event bound"):
        report(store, "owner", max_events=1)
    store.event(current["episode_id"], "evidence.gap", {"reason": "owner_omitted"})
    sample = report(store, "owner", task_ids=[current["id"]])["samples"][0]
    assert sample["timings"]["meaningful_result_seconds"] is None
    assert "source_event_history_incomplete" in sample["gaps"]
    assert (
        report(store, "owner", task_ids=[second["id"]])["samples"][0]["outcome"]
        == "accepted"
    )


def test_result_marker_without_terminal_state_does_not_count_as_completion(journal):
    store, tasks = journal
    current = task(tasks, "one")
    store.clock.advance(2)
    event(
        store, current, "task.voice_delivery", state="submitted", update_kind="result"
    )
    sample = report(store, "owner")["samples"][0]
    assert sample["timings"]["meaningful_result_seconds"] is None
    assert "meaningful_result:selection_without_prior_terminal_state" in sample["gaps"]

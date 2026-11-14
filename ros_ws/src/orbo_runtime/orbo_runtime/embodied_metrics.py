"""Bounded, owner-scoped latency reports from the existing task journal.

A result selected for delivery is not acoustic playback. We never subtract wall
clock timestamps, mix process boot domains, count acknowledgments as results, or
silently remove failed/unknown tasks from the selected measurement cohort.
"""

from collections import Counter
import json
import math

from .contracts import Rejected

TERMINAL_EVENTS = {"task.completed", "task.failed", "task.cancelled", "task.unknown"}
TIMING_NAMES = (
    "acknowledgment_seconds",
    "meaningful_result_seconds",
    "terminal_state_seconds",
    "navigation_seconds",
    "capture_seconds",
    "provider_work_seconds",
)


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def distribution(values):
    """Linear-interpolated sample quantiles; missing cases remain in denominator."""
    values = list(values)
    valid = sorted(float(value) for value in values if _number(value))

    def quantile(fraction):
        if not valid:
            return None
        position = (len(valid) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return valid[lower] + (valid[upper] - valid[lower]) * (position - lower)

    return {
        "count": len(valid),
        "missing": len(values) - len(valid),
        "p50": quantile(0.5),
        "p95": quantile(0.95),
        "minimum": valid[0] if valid else None,
        "maximum": valid[-1] if valid else None,
    }


def _elapsed(start, end, gaps, label):
    if start is None or end is None:
        gaps.add(label + ":missing_event")
        return None
    if not start.get("boot") or start.get("boot") != end.get("boot"):
        gaps.add(label + ":different_boot_domains")
        return None
    if not _number(start.get("mono")) or not _number(end.get("mono")):
        gaps.add(label + ":invalid_monotonic_timestamp")
        return None
    if end["mono"] < start["mono"] or end["seq"] < start["seq"]:
        gaps.add(label + ":nonmonotonic_event_order")
        return None
    return end["mono"] - start["mono"]


def _is_acknowledgment(event):
    payload = event["payload"]
    return (
        event["kind"] == "task.voice_delivery"
        and payload.get("state") == "submitted"
        and payload.get("update_kind") in {"progress", "clarification"}
    )


def _is_result(event):
    payload = event["payload"]
    if event["kind"] in {"task.result_released", "task.remote_delivery_selected"}:
        return True
    return (
        event["kind"] == "task.voice_delivery"
        and payload.get("state") == "submitted"
        and payload.get("update_kind") == "result"
    )


def _sample(task, revision, events):
    starts = [
        event for event in events if event["kind"] in {"task.accepted", "task.revised"}
    ]
    start = starts[0] if starts else None
    terminals = [event for event in events if event["kind"] in TERMINAL_EVENTS]
    terminal = next(
        (event for event in reversed(events) if event["kind"] in TERMINAL_EVENTS), None
    )
    is_current = revision == task["revision"]
    outcome = task["state"] if is_current else "superseded"
    if terminal:
        outcome = terminal["kind"].removeprefix("task.")
    terminal_payload = terminal["payload"].get("result", {}) if terminal else {}
    if not isinstance(terminal_payload, dict):
        terminal_payload = {}
    partial = terminal_payload.get("partial")
    if type(partial) is not bool:
        partial = None
    gaps = set()
    acknowledgments = [event for event in events if _is_acknowledgment(event)]
    selected_results = [event for event in events if _is_result(event)]
    # A text/delivery marker alone must not manufacture final task completion.
    result = next(
        (
            event
            for event in selected_results
            if any(final["seq"] <= event["seq"] for final in terminals)
        ),
        None,
    )
    if selected_results and result is None:
        gaps.add("meaningful_result:selection_without_prior_terminal_state")
    timings = {
        "acknowledgment_seconds": _elapsed(
            start,
            acknowledgments[0] if acknowledgments else None,
            gaps,
            "acknowledgment",
        ),
        "meaningful_result_seconds": _elapsed(start, result, gaps, "meaningful_result"),
        "terminal_state_seconds": _elapsed(start, terminal, gaps, "terminal_state"),
    }
    admitted = {}
    finished = {}
    for event in events:
        operation_id = event["payload"].get("operation_id")
        if event["kind"] == "task.operation_admitted" and operation_id:
            admitted.setdefault(operation_id, event)
        elif operation_id and event["kind"] in {
            "task.operation_completed",
            "task.operation_failed",
            "task.operation_cancelled",
            "task.operation_unknown",
        }:
            finished.setdefault(operation_id, event)
    operations = []
    totals = {"navigate": [], "capture": [], "infer": [], "compare": []}
    for operation_id, first in admitted.items():
        last = finished.get(operation_id)
        capability = first["payload"].get("capability")
        seconds = _elapsed(first, last, gaps, "operation:" + operation_id)
        operations.append(
            {
                "operation_id": operation_id,
                "capability": capability,
                "outcome": last["kind"].removeprefix("task.operation_")
                if last
                else "unresolved",
                "seconds": seconds,
            }
        )
        if capability in totals:
            totals[capability].append(seconds)
    for capability, name in (
        ("navigate", "navigation_seconds"),
        ("capture", "capture_seconds"),
    ):
        values = totals[capability]
        timings[name] = (
            sum(values) if all(value is not None for value in values) else None
        )
    provider_values = totals["infer"] + totals["compare"]
    timings["provider_work_seconds"] = (
        sum(provider_values)
        if all(value is not None for value in provider_values)
        else None
    )
    if start is None or any(event["kind"] == "evidence.gap" for event in events):
        gaps.add("source_event_history_incomplete")
        timings = dict.fromkeys(TIMING_NAMES)
        for operation in operations:
            operation["seconds"] = None
    interrupted = any(
        event["kind"] == "task.voice_delivery"
        and event["payload"].get("state") == "interrupted"
        for event in events
    )
    return {
        "task_id": task["id"],
        "revision": revision,
        "environment": task["environment"],
        "map_version": task["map_version"],
        "task_kind": task["request"]["kind"],
        "provenance": task["request"]["provenance"],
        "outcome": outcome,
        "partial": partial,
        "speech_interrupted": interrupted,
        "start_event_id": start["id"] if start else None,
        "terminal_event_id": terminal["id"] if terminal else None,
        "result_event_id": result["id"] if result else None,
        "timings": timings,
        "operations": operations,
        "gaps": sorted(gaps),
        "spoken_result_seconds": None,
        "spoken_result_evidence": "not_established_by_application_journal",
    }


def report(
    store, owner, *, environment=None, task_ids=None, max_tasks=100, max_events=20000
):
    """Report a bounded latest/explicit task cohort without revealing task content.

    Per-revision starts are durable acceptance or correction, not an assumed user
    utterance end. Missing terminal/result observations and cross-boot intervals
    remain null. Cohorts split environment/map/kind/provenance; synthetic and
    physical samples are never pooled. No database mutation or provider call.
    """
    if not isinstance(owner, str) or not owner:
        raise Rejected("owned metrics scope required")
    if environment is not None and (
        not isinstance(environment, str) or not environment
    ):
        raise Rejected("invalid metrics environment")
    if (
        type(max_tasks) is not int
        or not 1 <= max_tasks <= 1000
        or type(max_events) is not int
        or not 1 <= max_events <= 100000
    ):
        raise Rejected("bounded metrics cohort required")
    if task_ids is not None and (
        not isinstance(task_ids, list)
        or not 1 <= len(task_ids) <= max_tasks
        or any(not isinstance(tid, str) or not tid for tid in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        raise Rejected("bounded distinct task IDs required")
    clauses, parameters = ["owner=?"], [owner]
    if environment is not None:
        clauses.append("environment=?")
        parameters.append(environment)
    if task_ids is not None:
        clauses.append("id IN (" + ",".join("?" for _ in task_ids) + ")")
        parameters.extend(task_ids)
    rows = store.db.execute(
        "SELECT * FROM embodied_tasks WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created DESC,id LIMIT ?",
        (*parameters, max_tasks + 1),
    ).fetchall()
    if task_ids is not None and len(rows) != len(task_ids):
        raise Rejected("unknown task in owned metrics scope")
    truncated = len(rows) > max_tasks
    tasks = []
    for row in rows[:max_tasks]:
        task = dict(row)
        task["request"] = json.loads(task["request"])
        tasks.append(task)
    by_episode = {task["episode_id"]: task for task in tasks}
    events_by_task = {task["id"]: [] for task in tasks}
    if by_episode:
        query = (
            "SELECT * FROM events WHERE episode_id IN ("
            + ",".join("?" for _ in by_episode)
            + ") ORDER BY seq LIMIT ?"
        )
        events = store.db.execute(query, (*by_episode, max_events + 1)).fetchall()
        if len(events) > max_events:
            raise Rejected("metrics event bound exceeded; select a smaller cohort")
        for row in events:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            if not isinstance(event["payload"], dict):
                continue
            task = by_episode[event["episode_id"]]
            payload = event["payload"]
            if event["kind"] == "evidence.gap":
                events_by_task[task["id"]].append(event)
            elif (
                payload.get("task_id") == task["id"]
                and type(payload.get("revision")) is int
                and payload["revision"] > 0
            ):
                events_by_task[task["id"]].append(event)
    samples = []
    for task in tasks:
        events = events_by_task[task["id"]]
        revisions = {
            event["payload"]["revision"]
            for event in events
            if event["kind"] != "evidence.gap"
        } | {task["revision"]}
        for revision in sorted(revisions):
            selected = [
                event
                for event in events
                if event["kind"] == "evidence.gap"
                or event["payload"]["revision"] == revision
            ]
            samples.append(_sample(task, revision, selected))
    cohorts = {}
    for sample in samples:
        key = tuple(
            sample[field]
            for field in ("provenance", "environment", "map_version", "task_kind")
        )
        cohorts.setdefault(key, []).append(sample)
    summaries = []
    for key, subset in sorted(cohorts.items()):
        outcomes = dict(Counter(sample["outcome"] for sample in subset))
        summaries.append(
            {
                **dict(
                    zip(("provenance", "environment", "map_version", "task_kind"), key)
                ),
                "revision_samples": len(subset),
                "task_count": len({s["task_id"] for s in subset}),
                "outcomes": outcomes,
                "failed_or_unknown": sum(
                    outcomes.get(state, 0) for state in ("failed", "unknown")
                ),
                "partial_results": sum(sample["partial"] is True for sample in subset),
                "speech_interrupted": sum(
                    sample["speech_interrupted"] for sample in subset
                ),
                "latencies": {
                    name: distribution(sample["timings"][name] for sample in subset)
                    for name in TIMING_NAMES
                },
            }
        )
    return {
        "measurement": "application_journal",
        "quantiles": "linear_interpolation",
        "selection": {
            "task_count": len(tasks),
            "max_tasks": max_tasks,
            "max_events": max_events,
            "truncated": truncated,
            "environment": environment,
            "explicit_ids": task_ids is not None,
        },
        "cohorts": summaries,
        "samples": samples,
        "spoken_result_latency": "unmeasured",
        "limits": [
            "Latency starts at task acceptance or revision, not an assumed end of user speech.",
            "Acknowledgment is a submitted progress/clarification update; no acoustic delivery is implied.",
            "Meaningful result is the first application-selected final result, including honest failure/unknown answers.",
            "Failed, cancelled, unknown, partial and unresolved cases remain in samples and missing-value counts.",
            "Only same-boot monotonic intervals are subtracted; wall clocks never repair an unknown interval.",
            "Operation sums are observed admitted-to-outcome durations, not CPU time or an additive end-to-end decomposition.",
            "Provider work excludes pre-admission request interpretation; those requests have their own usage records.",
            "Small sample quantiles are descriptive and do not qualify performance under Pi/ROS load.",
        ],
    }

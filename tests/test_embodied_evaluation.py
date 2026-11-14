import copy
import json

import pytest

from orbo_runtime.contracts import ManualClock, Rejected
from orbo_runtime.embodied_evaluation import compare_delivered
from orbo_runtime.storage import Store
from orbo_runtime.traces import Exports, analyze, reconstruct_commons, trajectory


def package(store, condition, claim, intervention):
    episode = store.episode("owner", profile="physical_task.v1", provenance="synthetic")
    store.event(
        episode,
        "evaluation.object_location",
        {
            "case_id": "occluded-wrench",
            "condition": condition,
            "claim_label": claim,
            "label_source": "synthetic_fixture",
            "abstained": claim != "unsupported",
            "intervention": intervention,
            "observation_ids": ["fixture-view"],
        },
    )
    store.end_episode(episode, "owner", "completed")
    return Exports(store).prepare(
        "owner", episode, "embodied_process", "evaluation-consumer"
    )


def delivered(package):
    records = [json.loads(line) for line in trajectory(package).splitlines()]
    events = [
        {
            "event_type": "feedback",
            "structured_payload": {"record_type": "trajectory_observation"},
            "redacted_content": r["content"],
        }
        for r in records
        if r["role"] == "observation"
    ]
    return reconstruct_commons(events)


def test_exact_delivered_paired_process_analysis_and_mutation():
    with_store = Store(":memory:", ManualClock())
    before = package(with_store, "initial_view", "unsupported", "none")
    after = package(
        with_store, "clarification_or_additional_view", "supported", "additional_view"
    )
    report = compare_delivered(
        [delivered(before["package"]), delivered(after["package"])]
    )
    assert report["unsupported_claim_difference"] == -1
    assert report["causal_improvement"] == "not_established"
    assert not report["missing_pairs"]
    changed = copy.deepcopy(after["package"])
    changed["events"][1]["payload"]["claim_label"] = "unknown"
    # A different package has different exact review bytes; the ordinary approval binds originals.
    exports = Exports(with_store)
    with_store.grant(
        "owner", "contribute", "evaluation-consumer", True, with_store.clock.utc() + 60
    )
    exports.approve(
        "owner",
        after["export_id"],
        after["sha256"],
        expires=with_store.clock.utc() + 30,
    )
    assert (
        json.loads(exports.approved_bytes("owner", after["export_id"]))
        == after["package"]
    )
    with_store.db.execute(
        "UPDATE exports SET content=? WHERE id=?",
        (json.dumps(changed), after["export_id"]),
    )
    with pytest.raises(Rejected, match="changed"):
        exports.approved_bytes("owner", after["export_id"])
    with_store.close()


def test_omission_unknown_expiry_and_revocation_remain_explicit():
    store = Store(":memory:", ManualClock())
    exports = Exports(store)
    before = package(store, "initial_view", "unknown", "none")
    report = compare_delivered([before["package"]])
    assert report["missing_pairs"] == ["occluded-wrench"]
    child = exports.derive(
        "owner",
        before["export_id"],
        omit_event_ids=[before["package"]["events"][1]["id"]],
    )
    analysis = analyze(delivered(child["package"]))["process_evaluation"]
    assert not analysis["complete"] and analysis["gap_event_ids"]
    assert analysis["unsupported_claims"] == 0  # Missing is not success.
    store.grant(
        "owner", "contribute", "evaluation-consumer", True, store.clock.utc() + 60
    )
    exports.approve(
        "owner", child["export_id"], child["sha256"], expires=store.clock.utc() + 10
    )
    exports.revoke("owner", child["export_id"])
    with pytest.raises(Rejected):
        exports.approved_bytes("owner", child["export_id"])
    exports.approve(
        "owner", child["export_id"], child["sha256"], expires=store.clock.utc() + 10
    )
    store.clock.advance(11)
    with pytest.raises(Rejected):
        exports.approved_bytes("owner", child["export_id"])
    store.close()

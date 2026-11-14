import asyncio
import dataclasses
import json

import httpx
import pytest

from orbo_runtime.contracts import ManualClock, Rejected, digest
from orbo_runtime.evidence_memory import EvidenceMemory
from orbo_runtime.observations import LatestFrame, ObservationService, RGBFrame
from orbo_runtime.providers import VerifiedReply
from orbo_runtime.storage import Store
from orbo_runtime.vision import VerifiedVision, release_finding


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "journal.db", ManualClock())
    memory = EvidenceMemory(store, tmp_path / "images")
    for scope, destination in [
        ("observation.capture", "lab"),
        ("observation.retain", "lab"),
        ("observation.infer", "lab"),
        ("memory.write", "lab"),
        ("memory.read", "lab"),
        ("cloud.reasoning", "near"),
        ("result.release", "openai"),
    ]:
        store.grant("owner", scope, destination, True, store.clock.utc() + 10000)
    frame = RGBFrame(
        bytes([100, 110, 120]) * 256,
        16,
        16,
        store.clock.utc(),
        store.clock.mono(),
        "camera_optical",
        "fixture",
        100,
        "map-a",
        "cal-a",
        "shelf",
        {"x": 0, "y": 0, "yaw": 0, "frame_id": "map"},
        store.clock.mono(),
    )
    source = LatestFrame()
    source.update(frame)
    yield store, memory, source
    store.close()


async def capture(setup, **kwargs):
    _, memory, source = setup
    return await ObservationService(memory, source).capture(
        "owner", "lab", "map-a", "task", 1, "shelf", **kwargs
    )


def output(observation, **changes):
    return {
        "task_id": "task",
        "revision": 1,
        "status": "similar",
        "identity": "visual_similarity",
        "observation_ids": [observation["id"]],
        "occluded": True,
        "reason": "ambiguous_identity",
        "next_viewpoint": "other",
        "matched_attributes": [],
        **changes,
    }


class Reasoner:
    def __init__(self, result):
        self.result, self.messages = result, None

    async def complete(self, messages, tools):
        self.messages = messages
        assert tools == []
        message = {"content": json.dumps(self.result)}
        return VerifiedReply(
            message,
            {
                "provider": "near",
                "model": "fixture-model",
                "e2ee": True,
                "signature_kind": "provider_tee",
                "response_id": "fixture",
                "message_digest": digest(message),
            },
            {"total_tokens": 20},
        )


async def inspect(setup, observation, result=None, **kwargs):
    _, memory, _ = setup
    reasoner = Reasoner(result or output(observation))
    finding = await VerifiedVision(reasoner, memory).inspect(
        "owner",
        "lab",
        "map-a",
        "task",
        1,
        [observation["id"]],
        "red wrench",
        allowed_viewpoints=["other"],
        check_revision=lambda: None,
        **kwargs,
    )
    return finding, reasoner


async def test_snapshot_is_private_bounded_and_has_provenance(setup):
    store, memory, _ = setup
    observation = await capture(setup)
    assert observation["boot"] == store.boot
    assert observation["clock_domain"] == "fixture"
    assert observation["quality"]["focus_verified"] is False
    path = memory.root / (observation["id"] + ".png")
    assert path.stat().st_mode & 0o777 == 0o600
    assert memory.root.stat().st_mode & 0o777 == 0o700
    assert memory.image("owner", "lab", observation["id"]).startswith(b"\x89PNG")
    with pytest.raises(Rejected):
        memory.image("other-owner", "lab", observation["id"])
    with pytest.raises(Rejected):
        memory.observation("owner", "home", observation["id"])
    with pytest.raises(Rejected):
        memory.observation("owner", "lab", observation["id"], map_version="map-b")
    assert "data:image" not in str(
        [dict(row) for row in store.db.execute("SELECT * FROM events")]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"captured_at": 1},
        {"received_mono": -10},
        {"pose_received_mono": -10},
        {"map_version": "other"},
        {"viewpoint": "other"},
        {"calibration_version": ""},
        {"data": bytes(768)},
        {"data": bytes([255]) * 768},
        {"data": b"bad"},
        {"pose": {"x": float("nan"), "y": 0, "yaw": 0, "frame_id": "map"}},
    ],
)
async def test_unsuitable_snapshots_never_retained(setup, changes):
    store, _, source = setup
    source.latest = dataclasses.replace(source.latest, **changes)
    with pytest.raises(Rejected):
        await capture(setup)
    assert (
        store.db.execute("SELECT COUNT(*) FROM evidence_observations").fetchone()[0]
        == 0
    )


async def test_capture_permission_and_revision_checked_after_worker(setup, monkeypatch):
    store, _, _ = setup
    calls = []

    def fence():
        calls.append(True)
        if len(calls) == 2:
            raise Rejected("stale revision")

    with pytest.raises(Rejected):
        await capture(setup, check_revision=fence)
    assert (
        store.db.execute("SELECT COUNT(*) FROM evidence_observations").fetchone()[0]
        == 0
    )
    store.grant("owner", "observation.capture", "lab", False, store.clock.utc() + 10)
    with pytest.raises(Rejected):
        await capture(setup)


async def test_verified_finding_release_minimizes_outbound_and_preserves_uncertainty(
    setup,
):
    _, memory, _ = setup
    observation = await capture(setup)
    finding, reasoner = await inspect(setup, observation)
    payload = release_finding(
        memory, "owner", "lab", finding["id"], "openai", check_revision=lambda: None
    )
    assert "identity is unconfirmed" in payload["text"]
    assert "occluded" in payload["text"]
    assert (
        "proof" not in payload
        and "object_description" not in payload
        and "image_hash" not in payload
    )
    sent = json.dumps(reasoner.messages)
    assert "data:image/png;base64," in sent
    assert "https://" not in sent
    assert "red wrench" in sent
    assert (
        memory.recall("owner", "lab", description="red wrench")[0]["current_truth"]
        is False
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 2},
        {"revision": True},
        {"task_id": "other"},
        {"observation_ids": ["fake"]},
        {"extra_private_prose": "sentinel"},
        {"identity": "exact", "status": "present"},
        {"status": "not_visible", "identity": "exact"},
        {"next_viewpoint": "arbitrary-coordinate"},
        {"occluded": "false"},
        {"matched_attributes": [42]},
    ],
)
async def test_bad_findings_never_persist_or_release(setup, changes):
    store, _, _ = setup
    observation = await capture(setup)
    with pytest.raises(Rejected):
        await inspect(setup, observation, output(observation, **changes))
    assert store.db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0] == 0


async def test_failed_or_nonprivate_verification_is_not_a_finding(setup):
    store, memory, _ = setup
    observation = await capture(setup)

    class Unverified(Reasoner):
        async def complete(self, *args):
            reply = await super().complete(*args)
            return dataclasses.replace(reply, evidence={"e2ee": False})

    vision = VerifiedVision(Unverified(output(observation)), memory)
    with pytest.raises(Rejected):
        await vision.inspect(
            "owner",
            "lab",
            "map-a",
            "task",
            1,
            [observation["id"]],
            "wrench",
            check_revision=lambda: None,
        )
    assert store.db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0] == 0


async def test_late_result_and_permission_revocation_are_fenced(setup):
    store, memory, _ = setup
    observation = await capture(setup)

    class Revoking(Reasoner):
        async def complete(self, *args):
            store.grant(
                "owner", "observation.infer", "lab", False, store.clock.utc() + 100
            )
            return await super().complete(*args)

    with pytest.raises(Rejected):
        await VerifiedVision(Revoking(output(observation)), memory).inspect(
            "owner",
            "lab",
            "map-a",
            "task",
            1,
            [observation["id"]],
            "wrench",
            check_revision=lambda: None,
        )
    assert store.db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0] == 0


async def test_cancel_swallowing_provider_cannot_commit(setup):
    store, memory, _ = setup
    observation = await capture(setup)
    entered = asyncio.Event()

    class Late(Reasoner):
        async def complete(self, *args):
            entered.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                return await super().complete(*args)

    pending = asyncio.create_task(
        VerifiedVision(Late(output(observation)), memory).inspect(
            "owner",
            "lab",
            "map-a",
            "task",
            1,
            [observation["id"]],
            "wrench",
            check_revision=lambda: None,
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert store.db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0] == 0


async def test_exact_identity_requires_reference_image_and_all_attributes(setup):
    _, memory, _ = setup
    observation = await capture(setup)
    reference = memory.reference(
        "owner",
        "lab",
        "red wrench",
        observation_id=observation["id"],
        attributes=["owner mark"],
    )
    exact = output(
        observation,
        identity="exact",
        status="present",
        occluded=False,
        reason="clear_view",
        matched_attributes=["owner mark"],
    )
    finding, _ = await inspect(setup, observation, exact, reference_id=reference["id"])
    assert finding["identity"] == "exact"
    with pytest.raises(Rejected):
        await inspect(
            setup,
            observation,
            dict(exact, matched_attributes=[]),
            reference_id=reference["id"],
        )


async def test_retention_deletes_images_and_all_derived_memory(setup):
    store, memory, _ = setup
    observation = await capture(setup, retention_seconds=2)
    finding, _ = await inspect(setup, observation)
    memory.reference("owner", "lab", "tool", observation_id=observation["id"])
    memory.accept_baseline("owner", "lab", finding["id"], name="shelf baseline")
    store.clock.advance(3)
    assert memory.purge_expired() == 1
    assert not list(memory.root.glob("*.png"))
    assert store.db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0] == 0
    assert memory.recall("owner", "lab", description="red wrench") == []


async def test_tampered_image_rejected_before_inference(setup):
    _, memory, _ = setup
    observation = await capture(setup)
    (memory.root / (observation["id"] + ".png")).write_bytes(b"tampered")
    with pytest.raises(Rejected):
        await inspect(setup, observation)


async def test_comparison_rejects_pose_change_without_provider_call(setup):
    _, memory, source = setup
    observation = await capture(setup)
    finding, _ = await inspect(setup, observation)
    baseline = memory.accept_baseline("owner", "lab", finding["id"], name="accepted")
    source.latest = dataclasses.replace(
        source.latest, pose={"x": 1, "y": 0, "yaw": 0, "frame_id": "map"}
    )
    current = await capture(setup)
    reasoner = Reasoner({})
    result = await VerifiedVision(reasoner, memory).compare(
        "owner",
        "lab",
        "map-a",
        "task",
        1,
        baseline["id"],
        current["id"],
        check_revision=lambda: None,
    )
    assert result["status"] == "unknown" and result["reason"] == "incomparable_views"
    assert reasoner.messages is None


async def test_comparison_uses_accepted_baseline_and_blocks_lighting_claim(setup):
    _, memory, _ = setup
    observation = await capture(setup)
    finding, _ = await inspect(setup, observation)
    baseline = memory.accept_baseline("owner", "lab", finding["id"], name="accepted")
    current = await capture(setup)
    result = {
        "task_id": "task",
        "revision": 1,
        "status": "changed",
        "reason": "poor_lighting",
        "changes": ["object_removed"],
    }
    with pytest.raises(Rejected):
        await VerifiedVision(Reasoner(result), memory).compare(
            "owner",
            "lab",
            "map-a",
            "task",
            1,
            baseline["id"],
            current["id"],
            check_revision=lambda: None,
        )
    result["reason"] = "clear_view"
    comparison = await VerifiedVision(Reasoner(result), memory).compare(
        "owner",
        "lab",
        "map-a",
        "task",
        1,
        baseline["id"],
        current["id"],
        check_revision=lambda: None,
    )
    assert comparison["changes"] == ["object_removed"]


async def test_installed_near_sdk_encrypts_entire_multimodal_content():
    """Real SDK cryptography, no account or server: source contract qualification."""
    pytest.importorskip("nearai_inference_sdk")
    from nearai_inference_sdk.core.e2ee import (
        create_e2ee_client_key_pair,
        decrypt_e2ee_text,
        build_e2ee_request_aad,
        e2ee_field_aad,
    )
    from nearai_inference_sdk.core.e2ee_request import prepare_e2ee_chat_request
    from nearai_inference_sdk.types.e2ee import E2eeModelKey

    key = create_e2ee_client_key_pair()
    model_key = E2eeModelKey(signing_algo=key.signing_algo, public_key=key.public_key)
    content = [
        {"type": "text", "text": "private-object"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,private-pixels"},
        },
    ]
    request = httpx.Request(
        "POST",
        "https://cloud-api.near.ai/v1/chat/completions",
        json={
            "model": "z-ai/glm-5.3-flash",
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        },
    )
    prepared = await prepare_e2ee_chat_request(request, model_key)
    raw = await prepared.request.aread()
    assert (
        b"private-pixels" not in raw
        and b"private-object" not in raw
        and b"image_url" not in raw
    )
    reply_key = E2eeModelKey(
        signing_algo="ed25519", public_key=prepared.request.headers["x-client-pub-key"]
    )
    aad = e2ee_field_aad(build_e2ee_request_aad(reply_key), "messages[].content")
    decrypted = decrypt_e2ee_text(
        json.loads(raw)["messages"][0]["content"], key, "messages[].content", aad
    )
    assert json.loads(decrypted) == content
    assert prepared.request.headers["x-encrypt-all-fields"] == "true"
    assert prepared.request.headers["x-no-aliasing"] == "true"


async def test_benchmark_holds_inputs_fixed_and_retains_failures_and_abstention():
    from orbo_runtime.vision_benchmark import SCENARIOS, benchmark

    cases = [
        {
            "id": name,
            "scenario": name,
            "input": {"scene": name},
            "evidence_hashes": ["a" * 64],
        }
        for name in SCENARIOS
    ]

    async def safe(input):
        rule = SCENARIOS[input["scene"]]
        return {"status": rule["statuses"][0], "identity": rule["identities"][0]}

    async def broken(input):
        if input["scene"] == "stale_frame":
            raise RuntimeError("private error content")
        return {"status": "present", "identity": "exact"}

    report = await benchmark(
        cases,
        {"fixture-safe": safe, "fixture-overclaim": broken},
        provenance="synthetic",
    )
    assert report["summary"]["fixture-safe"]["correct"] == 7
    assert report["summary"]["fixture-safe"]["abstentions"] == 4
    assert report["summary"]["fixture-overclaim"]["failures"] == 1
    assert report["summary"]["fixture-overclaim"]["unsupported_claims"] == 5
    assert "private error content" not in json.dumps(report)
    assert len(report["results"]) == 14
    with pytest.raises(Rejected):
        await benchmark(cases[:-1], {"fixture": safe}, provenance="synthetic")


async def test_reference_delete_invalidates_findings_and_baselines_but_keeps_shared_image(
    setup,
):
    _, memory, _ = setup
    observation = await capture(setup)
    reference = memory.reference(
        "owner", "lab", "wrench", observation_id=observation["id"]
    )
    finding, _ = await inspect(setup, observation, reference_id=reference["id"])
    baseline = memory.accept_baseline("owner", "lab", finding["id"], name="before")
    assert set(memory.delete_record("reference", "owner", "lab", reference["id"])) == {
        reference["id"],
        finding["id"],
        baseline["id"],
    }
    assert memory.image("owner", "lab", observation["id"])
    assert memory.recall("owner", "lab", description="red wrench") == []


async def test_release_fences_revised_task_and_revoked_recipient(setup):
    store, memory, _ = setup
    observation = await capture(setup)
    finding, _ = await inspect(setup, observation)

    def stale():
        raise Rejected("stale task revision")

    with pytest.raises(Rejected):
        release_finding(
            memory, "owner", "lab", finding["id"], "openai", check_revision=stale
        )
    store.grant("owner", "result.release", "openai", False, store.clock.utc() + 10)
    with pytest.raises(Rejected):
        release_finding(
            memory, "owner", "lab", finding["id"], "openai", check_revision=lambda: None
        )

"""Task-specific, self-contained packages and approval bound to exact bytes."""

from datetime import datetime, timezone
import hashlib
import json

from .contracts import Rejected, canonical, identity


PURPOSES = {
    "attention_timing": "attention.v1",
    "physical_task_outcomes": "physical_task.v1",
}


class Exports:
    def __init__(self, store):
        self.store = store

    def prepare(self, owner, episode_id, purpose, recipient):
        if (
            purpose not in PURPOSES
            or not isinstance(recipient, str)
            or not 1 <= len(recipient) <= 128
        ):
            raise Rejected("unknown export purpose or recipient")
        with self.store.transaction() as db:
            ep = db.execute(
                "SELECT * FROM episodes WHERE id=? AND owner=?", (episode_id, owner)
            ).fetchone()
            if not ep or ep["state"] == "active" or ep["profile"] != PURPOSES[purpose]:
                raise Rejected(
                    "closed owned episode matching the consumer purpose required"
                )
            events = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE episode_id=? ORDER BY seq",
                    (episode_id,),
                )
            ]
            for event in events:
                event["payload"] = json.loads(event["payload"])
            # This text profile is deliberately lossless, with no unsupported anonymity claim.
            package = {
                "schema": "orbo.task-export.v1",
                "episode": dict(ep),
                "purpose": purpose,
                "recipient": recipient,
                "events": events,
                "disclosure": {
                    "modalities": ["text", "event_metadata"],
                    "includes_request_text": True,
                    "raw_audio": False,
                    "images": False,
                    "robot_control_metadata": True,
                    "may_include_named_places_and_poses": True,
                    "risk": "owner_review_required",
                    "limitations": [
                        "No automatic PII removal",
                        "Metadata and text may reveal private context",
                        "Software acknowledgments are not independent physical observations",
                    ],
                },
            }
            package["evaluation"] = analyze(package)
            content = canonical(package)
            if len(content.encode()) > 524288:
                raise Rejected(
                    "episode exceeds bounded export profile; select a smaller task episode"
                )
            sha = hashlib.sha256(content.encode()).hexdigest()
            export_id = identity()
            db.execute(
                "INSERT INTO exports VALUES(?,?,?,?,?,?)",
                (
                    export_id,
                    episode_id,
                    sha,
                    content,
                    purpose,
                    recipient,
                ),
            )
            return {"export_id": export_id, "sha256": sha, "package": package}

    def derive(self, owner, export_id, *, redactions=(), omit_event_ids=()):
        """Owner-selected minimization; immutable child, no inherited approval.

        Paths are JSON-pointer-like arrays, e.g. ["events", 2, "payload", "text"].
        Only payload string leaves and episode.owner may be masked. Omissions
        become explicit gap records; timing/outcome/provenance cannot be edited.
        """
        with self.store.transaction() as db:
            parent = self.preview(owner, export_id)
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='commons_reviews'"
            ).fetchone():
                if db.execute(
                    "SELECT 1 FROM commons_reviews WHERE export_id=? AND state NOT IN ('reviewed','refused','expired','superseded','revoked')",
                    (export_id,),
                ).fetchone():
                    raise Rejected(
                        "revoke and recover active parent contributions before deriving"
                    )
            if (
                not isinstance(redactions, (list, tuple))
                or len(redactions) > 128
                or not isinstance(omit_event_ids, (list, tuple))
                or len(omit_event_ids) > 1024
                or not (redactions or omit_event_ids)
            ):
                raise Rejected("bounded explicit redactions or omissions required")
            package = json.loads(parent["content"])
            ids = {e["id"] for e in package["events"]}
            if any(not isinstance(i, str) or i not in ids for i in omit_event_ids):
                raise Rejected("unknown omitted event")
            affected = set(package.get("derivation", {}).get("redacted_event_ids", []))
            for path in redactions:
                if (
                    not isinstance(path, list)
                    or not 2 <= len(path) <= 16
                    or not (
                        path == ["episode", "owner"]
                        or (
                            len(path) >= 4
                            and path[0] == "events"
                            and type(path[1]) is int
                            and 0 <= path[1] < len(package["events"])
                            and path[2] == "payload"
                        )
                    )
                ):
                    raise Rejected(
                        "only payload string leaves or episode owner may be redacted"
                    )
                node = package
                try:
                    for key in path[:-1]:
                        if isinstance(node, list) and (type(key) is not int or key < 0):
                            raise KeyError(key)
                        node = node[key]
                    key = path[-1]
                    if isinstance(node, list) and (type(key) is not int or key < 0):
                        raise KeyError(key)
                    if not isinstance(node[key], str):
                        raise KeyError(key)
                    node[key] = "[redacted]"
                except (KeyError, IndexError, TypeError):
                    raise Rejected(
                        "redaction must select an existing string leaf"
                    ) from None
                if path[0] == "events":
                    affected.add(package["events"][path[1]]["id"])
            for event in package["events"]:
                if event["id"] in omit_event_ids:
                    event["kind"] = "evidence.gap"
                    event["payload"] = {"reason": "owner_omitted_event"}
                    affected.add(event["id"])
            package["derivation"] = {
                "parent_sha256": parent["digest"],
                "redacted_event_ids": sorted(affected),
                "approval_inherited": False,
            }
            package["disclosure"]["limitations"].append(
                "Owner-minimized derivative; affected events excluded from outcome/timing metrics; remaining content requires review"
            )
            package["evaluation"] = analyze(package)
            content = canonical(package)
            if len(content.encode()) > 524288:
                raise Rejected("derived export exceeds bounded profile")
            sha = hashlib.sha256(content.encode()).hexdigest()
            child = identity()
            db.execute(
                "INSERT INTO exports VALUES(?,?,?,?,?,?)",
                (
                    child,
                    parent["episode_id"],
                    sha,
                    content,
                    parent["purpose"],
                    parent["recipient"],
                ),
            )
            # The old review must not be used after the owner chooses minimization.
            db.execute("UPDATE approvals SET revoked=1 WHERE export_id=?", (export_id,))
            return {
                "export_id": child,
                "sha256": sha,
                "package": package,
                "parent_export_id": export_id,
                "approval_required": True,
            }

    def preview(self, owner, export_id):
        row = self.store.db.execute(
            "SELECT x.* FROM exports x JOIN episodes e ON e.id=x.episode_id WHERE x.id=? AND e.owner=?",
            (export_id, owner),
        ).fetchone()
        if not row:
            raise Rejected("unknown owned export")
        content = row["content"].encode()
        if hashlib.sha256(content).hexdigest() != row["digest"]:
            raise Rejected("stored export content changed")
        return dict(row)

    def approve(self, owner, export_id, reviewed_digest, *, expires):
        with self.store.transaction() as db:
            row = self.preview(owner, export_id)
            revision = self.store.permission(owner, "contribute", row["recipient"])
            if reviewed_digest != row["digest"] or expires <= self.store.clock.utc():
                raise Rejected(
                    "approval must bind current reviewed content and future expiry"
                )
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,0) ON CONFLICT(export_id) DO UPDATE SET digest=excluded.digest,owner=excluded.owner,revision=excluded.revision,expires=excluded.expires,revoked=0",
                (export_id, reviewed_digest, owner, revision, expires),
            )
            self.store.event(
                row["episode_id"],
                "export.approved",
                {
                    "export_id": export_id,
                    "digest": reviewed_digest,
                    "purpose": row["purpose"],
                    "recipient": row["recipient"],
                },
            )

    def revoke(self, owner, export_id):
        with self.store.transaction() as db:
            self.preview(owner, export_id)
            db.execute(
                "UPDATE approvals SET revoked=1 WHERE export_id=? AND owner=?",
                (export_id, owner),
            )

    def approved_bytes(self, owner, export_id):
        row = self.preview(owner, export_id)
        approval = self.store.db.execute(
            "SELECT * FROM approvals WHERE export_id=? AND owner=?", (export_id, owner)
        ).fetchone()
        revision = self.store.permission(owner, "contribute", row["recipient"])
        if (
            not approval
            or approval["revoked"]
            or approval["expires"] <= self.store.clock.utc()
            or approval["digest"] != row["digest"]
            or approval["revision"] != revision
        ):
            raise Rejected("current exact-content approval required")
        return row["content"].encode()


def analyze(package):
    """Reproducible analysis uses only supplied package, including missingness."""
    if (
        package.get("schema") != "orbo.task-export.v1"
        or package.get("purpose") not in PURPOSES
    ):
        raise Rejected("unsupported package")
    events = package["events"]
    kinds = [e["kind"] for e in events]
    unknown = [
        e["id"]
        for e in events
        if "unknown" in e["kind"] or e["payload"].get("state") == "unknown"
    ]
    gaps = [e["id"] for e in events if e["kind"] == "evidence.gap"]
    latencies = []
    attention = {}
    affected = set(package.get("derivation", {}).get("redacted_event_ids", []))
    for event in events:
        if event["id"] in affected:
            continue
        payload = event["payload"]
        rid = payload.get("reminder_id")
        if event["kind"] == "attention.awaiting_ack":
            attention[(rid, payload.get("generation"))] = event
        if event["kind"] == "attention.acknowledged":
            start = attention.get((rid, payload.get("generation")))
            if (
                start
                and start["boot"] == event["boot"]
                and event["mono"] >= start["mono"]
            ):
                latencies.append(event["mono"] - start["mono"])
    return {
        "provenance": package["episode"]["provenance"],
        "event_count": len(events),
        "declared_outcome": package["episode"]["state"],
        "ack_latency_seconds": latencies,
        "no_ack_count": kinds.count("attention.no_ack"),
        "interruption_count": kinds.count("turn.failed"),
        "unknown_event_ids": unknown,
        "gap_event_ids": gaps,
        "complete_boundaries": bool(events)
        and kinds[0] == "episode.started"
        and "episode.ended" in kinds,
        "physical_success": "not_established",
        "independent_observation_count": sum(
            e["kind"] == "observation.independent" and e["id"] not in affected
            for e in events
        ),
        **(
            {"redacted_event_ids": sorted(affected), "metrics_complete": False}
            if affected
            else {}
        ),
    }


def trajectory(package):
    """Actual Letta trajectory roles. Observations remain observations, never fake tools."""
    records = [
        {"role": "meta", "source": "orbo"},
        {
            "role": "observation",
            "timestamp": datetime.fromtimestamp(
                package["episode"]["started"], timezone.utc
            ).isoformat(),
            "content": canonical(
                {
                    "record_type": "orbo_export_manifest",
                    "source_export_digest": hashlib.sha256(
                        canonical(package).encode()
                    ).hexdigest(),
                    "manifest": {
                        key: value for key, value in package.items() if key != "events"
                    },
                }
            ),
        },
    ]
    for event in package["events"]:
        stamp = datetime.fromtimestamp(event["utc"], timezone.utc).isoformat()
        payload = event["payload"]
        common = {"timestamp": stamp}
        if event["kind"] in {
            "tool.request",
            "tool.result",
            "turn.started",
            "turn.completed",
        }:
            records.append(
                {
                    **common,
                    "role": "observation",
                    "content": canonical({"record_type": "orbo_event", "event": event}),
                }
            )
        if event["kind"] == "tool.request":
            records.append(
                {
                    **common,
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": payload["call_id"],
                            "name": payload["name"],
                            "args": canonical(payload["arguments"]),
                        }
                    ],
                }
            )
        elif event["kind"] == "tool.result":
            records.append(
                {
                    **common,
                    "role": "tool",
                    "tool_call_id": payload["call_id"],
                    "content": canonical(payload["result"]),
                }
            )
        elif event["kind"] == "turn.started":
            records.append({**common, "role": "user", "content": payload["text"]})
        elif event["kind"] == "turn.completed":
            records.append({**common, "role": "assistant", "content": payload["text"]})
        else:
            records.append(
                {
                    **common,
                    "role": "observation",
                    "content": canonical(
                        {
                            "record_type": "orbo_event",
                            "event": event,
                            "event_id": event["id"],
                            "kind": event["kind"],
                            "provenance": package["episode"]["provenance"],
                            "payload": payload,
                        }
                    ),
                }
            )
    encoded = ("\n".join(canonical(record) for record in records) + "\n").encode()
    if len(encoded) > 524288:
        raise Rejected("trajectory exceeds bounded contributor profile")
    return encoded


def reconstruct_commons(events):
    """Consume only delivered marked observations, with no local DB side channel.

    A redaction of required evidence makes exact reconstruction unavailable.
    Other trace content remains ordinary untrusted data, never an instruction.
    """
    manifests, recorded = [], []
    for event in events:
        if (
            event.get("event_type") != "feedback"
            or event.get("structured_payload", {}).get("record_type")
            != "trajectory_observation"
        ):
            continue
        try:
            payload = json.loads(event.get("redacted_content") or "null")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("record_type") == "orbo_export_manifest":
            manifests.append(payload)
        elif payload.get("record_type") == "orbo_event":
            recorded.append(payload["event"])
    if len(manifests) != 1 or not recorded:
        raise Rejected("complete preserved Orbo observations required")
    manifest = manifests[0]
    if len({e["id"] for e in recorded}) != len(recorded):
        raise Rejected("duplicate exported event")
    package = {**manifest["manifest"], "events": recorded}
    actual = hashlib.sha256(canonical(package).encode()).hexdigest()
    if actual != manifest["source_export_digest"]:
        raise Rejected("export evidence changed, redacted, reordered or incomplete")
    analyze(package)
    return package

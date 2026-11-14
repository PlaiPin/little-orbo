"""Owner/environment-scoped evidence in the existing journal and private image store.

Images are opaque, locally encoded PNG bytes, never public URLs. Every derived
record retains its observation IDs, so expiry/deletion can invalidate it.
"""

import hashlib
import json
import os
from pathlib import Path

from .contracts import Rejected, canonical, finite, identity


SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_observations(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, environment TEXT NOT NULL,
 map_version TEXT NOT NULL, captured REAL NOT NULL, expires REAL NOT NULL,
 body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_records(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
 environment TEXT NOT NULL, created REAL NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS evidence_scope ON evidence_records(owner,environment,kind,created);
"""


def label(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected(f"invalid {name}")
    return value


class EvidenceMemory:
    def __init__(self, store, image_root, *, max_bytes=64 * 1024 * 1024):
        self.store = store
        self.root = Path(image_root).absolute()
        if self.root.is_symlink():
            raise Rejected("image directory cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.max_bytes = int(finite(max_bytes, 1_000_000, 1024 * 1024 * 1024))
        store.db.executescript(SCHEMA)

    def add_observation(
        self, owner, environment, metadata, png, *, retention_seconds=3600
    ):
        label(owner, "owner")
        label(environment, "environment")
        label(metadata.get("map_version"), "map version")
        self.store.permission(owner, "observation.retain", environment)
        finite(retention_seconds, 1, 30 * 86400)
        if (
            not isinstance(png, bytes)
            or not png.startswith(b"\x89PNG\r\n\x1a\n")
            or len(png) > 2_000_000
        ):
            raise Rejected("bounded PNG required")
        self.purge_expired()
        used = sum(
            p.stat().st_size for p in self.root.glob("*.png") if not p.is_symlink()
        )
        if used + len(png) > self.max_bytes:
            raise Rejected("private image quota exhausted")
        observation_id = identity()
        body = dict(
            metadata,
            id=observation_id,
            owner=owner,
            environment=environment,
            image_hash=hashlib.sha256(png).hexdigest(),
            image_bytes=len(png),
            expires=self.store.clock.utc() + retention_seconds,
        )
        finite(body.get("captured_at"), 0, self.store.clock.utc() + 0.1)
        path = self.root / (observation_id + ".png")
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(png)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO evidence_observations VALUES(?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        owner,
                        environment,
                        body["map_version"],
                        body["captured_at"],
                        body["expires"],
                        canonical(body),
                    ),
                )
                self.store.event(
                    None,
                    "observation.retained",
                    {
                        "observation_id": observation_id,
                        "task_id": body.get("task_id"),
                        "revision": body.get("revision"),
                    },
                )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return body

    def observation(self, owner, environment, observation_id, *, map_version=None):
        row = self.store.db.execute(
            "SELECT * FROM evidence_observations WHERE id=? AND owner=? AND environment=?",
            (observation_id, owner, environment),
        ).fetchone()
        if row is None or row["expires"] <= self.store.clock.utc():
            raise Rejected("observation unavailable or expired")
        if map_version is not None and row["map_version"] != map_version:
            raise Rejected("observation map mismatch")
        return json.loads(row["body"])

    def image(self, owner, environment, observation_id):
        record = self.observation(owner, environment, observation_id)
        path = self.root / (record["id"] + ".png")
        if path.is_symlink():
            raise Rejected("invalid image path")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise Rejected("image unavailable") from error
        if hashlib.sha256(content).hexdigest() != record["image_hash"]:
            raise Rejected("image integrity mismatch")
        return content

    def put(self, kind, owner, environment, body):
        if kind not in {"reference", "finding", "baseline", "comparison"}:
            raise Rejected("invalid evidence kind")
        label(owner, "owner")
        label(environment, "environment")
        record_id = identity()
        record = dict(
            body,
            id=record_id,
            owner=owner,
            environment=environment,
            created_at=self.store.clock.utc(),
            source_type=body.get("source_type", "model_interpretation"),
        )
        if len(canonical(record)) > 32768:
            raise Rejected("evidence record too large")
        for observation_id in record.get("observation_ids", []):
            self.observation(owner, environment, observation_id)
        self.store.db.execute(
            "INSERT INTO evidence_records VALUES(?,?,?,?,?,?)",
            (
                record_id,
                kind,
                owner,
                environment,
                record["created_at"],
                canonical(record),
            ),
        )
        return record

    def get(self, kind, owner, environment, record_id):
        row = self.store.db.execute(
            "SELECT body FROM evidence_records WHERE id=? AND kind=? AND owner=? AND environment=?",
            (record_id, kind, owner, environment),
        ).fetchone()
        if row is None:
            raise Rejected("evidence unavailable")
        body = json.loads(row["body"])
        for observation_id in body.get("observation_ids", []):
            self.observation(owner, environment, observation_id)
        return body

    def reference(
        self, owner, environment, description, *, observation_id=None, attributes=None
    ):
        self.store.permission(owner, "memory.write", environment)
        label(description, "description", 2000)
        attributes = attributes or []
        if not isinstance(attributes, list) or len(attributes) > 16:
            raise Rejected("bounded reference attributes required")
        for attribute in attributes:
            label(attribute, "attribute", 256)
        return self.put(
            "reference",
            owner,
            environment,
            {
                "description": description,
                "attributes": attributes,
                "observation_ids": [observation_id] if observation_id else [],
                "source_type": "owner_statement",
                "identity": "unconfirmed",
            },
        )

    def recall(
        self, owner, environment, *, reference_id=None, description=None, limit=10
    ):
        """Last-seen evidence, never a claim that an object is there now."""
        self.store.permission(owner, "memory.read", environment)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Rejected("bounded recall limit required")
        if not reference_id and not description:
            raise Rejected("reference or description required for recall")
        if reference_id:
            self.get("reference", owner, environment, reference_id)
        rows = self.store.db.execute(
            "SELECT id FROM evidence_records WHERE kind='finding' AND owner=? AND environment=? ORDER BY created DESC LIMIT 1000",
            (owner, environment),
        ).fetchall()
        result = []
        for row in rows:
            try:
                finding = self.get("finding", owner, environment, row["id"])
            except Rejected:
                continue
            if finding.get("status") not in {"present", "similar"}:
                continue
            if reference_id and finding.get("reference_id") != reference_id:
                continue
            if (
                description
                and finding.get("object_description", "").casefold()
                != description.casefold()
            ):
                continue
            result.append(
                dict(
                    finding,
                    age_seconds=max(0, self.store.clock.utc() - finding["observed_at"]),
                    current_truth=False,
                )
            )
            if len(result) >= limit:
                break
        return result

    def accept_baseline(self, owner, environment, finding_id, *, name, exceptions=None):
        self.store.permission(owner, "memory.write", environment)
        finding = self.get("finding", owner, environment, finding_id)
        label(name, "baseline name")
        exceptions = exceptions or []
        if not isinstance(exceptions, list) or len(exceptions) > 16:
            raise Rejected("bounded temporary exceptions required")
        for exception in exceptions:
            if not isinstance(exception, dict) or set(exception) != {
                "description",
                "expires",
            }:
                raise Rejected("invalid baseline exception")
            label(exception["description"], "exception")
            finite(
                exception["expires"],
                self.store.clock.utc(),
                self.store.clock.utc() + 30 * 86400,
            )
        if finding["status"] == "unknown":
            raise Rejected("unknown finding cannot establish a baseline")
        return self.put(
            "baseline",
            owner,
            environment,
            {
                "name": name,
                "finding_id": finding_id,
                "observation_ids": finding["observation_ids"],
                "map_version": finding["map_version"],
                "accepted_by": owner,
                "accepted_at": self.store.clock.utc(),
                "exceptions": exceptions,
                "source_type": "owner_accepted_baseline",
            },
        )

    def delete_observation(self, owner, environment, observation_id):
        # Ownership check still works on expired records; deletion is not retention.
        row = self.store.db.execute(
            "SELECT id FROM evidence_observations WHERE id=? AND owner=? AND environment=?",
            (observation_id, owner, environment),
        ).fetchone()
        if row is None:
            raise Rejected("observation unavailable")
        with self.store.transaction():
            for record in self.store.db.execute(
                "SELECT id,body FROM evidence_records WHERE owner=? AND environment=?",
                (owner, environment),
            ).fetchall():
                if observation_id in json.loads(record["body"]).get(
                    "observation_ids", []
                ):
                    self.store.db.execute(
                        "DELETE FROM evidence_records WHERE id=?", (record["id"],)
                    )
            self.store.db.execute(
                "DELETE FROM evidence_observations WHERE id=?", (observation_id,)
            )
            self.store.event(
                None, "observation.deleted", {"observation_id": observation_id}
            )
        (self.root / (observation_id + ".png")).unlink(missing_ok=True)

    def delete_record(self, kind, owner, environment, record_id):
        """Delete a reference/baseline/finding and records derived from its identity.

        Observation deletion is separate because a shared source image can support
        another permitted record. Delivered recipient copies are outside this store.
        """
        self.store.permission(owner, "memory.write", environment)
        self.get(kind, owner, environment, record_id)
        removed = {record_id}
        records = self.store.db.execute(
            "SELECT id,body FROM evidence_records WHERE owner=? AND environment=?",
            (owner, environment),
        ).fetchall()
        changed = True
        while changed:
            changed = False
            for record in records:
                body = json.loads(record["body"])
                if record["id"] not in removed and any(
                    body.get(field) in removed
                    for field in ("reference_id", "finding_id", "baseline_id")
                ):
                    removed.add(record["id"])
                    changed = True
        with self.store.transaction():
            self.store.db.executemany(
                "DELETE FROM evidence_records WHERE id=?",
                [(value,) for value in removed],
            )
            self.store.event(None, "evidence.deleted", {"record_ids": sorted(removed)})
        return sorted(removed)

    def purge_expired(self):
        rows = self.store.db.execute(
            "SELECT id,owner,environment FROM evidence_observations WHERE expires<=?",
            (self.store.clock.utc(),),
        ).fetchall()
        for row in rows:
            self.delete_observation(row["owner"], row["environment"], row["id"])
        # A crash between file creation and DB commit leaves an unreferenced image.
        known = {
            row[0]
            for row in self.store.db.execute("SELECT id FROM evidence_observations")
        }
        for path in self.root.glob("*.png"):
            if path.stem not in known:
                path.unlink(missing_ok=True)
        return len(rows)

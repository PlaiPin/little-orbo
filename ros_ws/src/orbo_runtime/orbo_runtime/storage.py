"""Single-process SQLite writer. Intent and event commit before external effects."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .contracts import Rejected, SystemClock, canonical, digest, identity


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL,
 provenance TEXT NOT NULL, state TEXT NOT NULL, started REAL NOT NULL, ended REAL);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 episode_id TEXT REFERENCES episodes(id), kind TEXT NOT NULL,
 utc REAL NOT NULL, mono REAL NOT NULL, boot TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 owner TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(owner,key));
CREATE TABLE IF NOT EXISTS reminders(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, episode_id TEXT NOT NULL REFERENCES episodes(id),
 text TEXT NOT NULL, due REAL NOT NULL, state TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, ack_token TEXT, ack_until REAL,
 command_id TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS preferences(
 owner TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
 value TEXT NOT NULL, authority TEXT NOT NULL REFERENCES events(id), PRIMARY KEY(owner,key));
CREATE TABLE IF NOT EXISTS grants(
 owner TEXT NOT NULL, scope TEXT NOT NULL, destination TEXT NOT NULL,
 revision INTEGER NOT NULL, enabled INTEGER NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(owner,scope,destination));
CREATE TABLE IF NOT EXISTS leases(
 robot TEXT PRIMARY KEY, holder TEXT NOT NULL, epoch INTEGER NOT NULL, expires REAL NOT NULL,
 boot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id),
 robot TEXT NOT NULL, capability TEXT NOT NULL, resources TEXT NOT NULL,
 body TEXT NOT NULL, digest TEXT NOT NULL, epoch INTEGER NOT NULL,
 deadline REAL NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY REFERENCES commands(id), state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exports(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id),
 digest TEXT NOT NULL, content TEXT NOT NULL, purpose TEXT NOT NULL, recipient TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 export_id TEXT PRIMARY KEY REFERENCES exports(id), digest TEXT NOT NULL,
 owner TEXT NOT NULL, revision INTEGER NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS embodied_tasks(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id), owner TEXT NOT NULL,
 environment TEXT NOT NULL, map_version TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL, request TEXT NOT NULL, budget TEXT NOT NULL, used TEXT NOT NULL,
 created REAL NOT NULL, deadline REAL NOT NULL, updated REAL NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_operations(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES embodied_tasks(id),
 revision INTEGER NOT NULL, operation_key TEXT NOT NULL, capability TEXT NOT NULL,
 parameters TEXT NOT NULL, state TEXT NOT NULL, deadline REAL NOT NULL,
 command_id TEXT REFERENCES commands(id), result TEXT NOT NULL,
 UNIQUE(task_id,revision,operation_key));
CREATE TABLE IF NOT EXISTS semantic_places(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, environment TEXT NOT NULL,
 map_version TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
 UNIQUE(owner,environment,map_version,name));
CREATE TABLE IF NOT EXISTS records(
 provider TEXT NOT NULL, account TEXT NOT NULL, external_id TEXT NOT NULL,
 kind TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(provider,account,external_id,kind));
"""


class Store:
    def __init__(self, path, clock=None):
        self.clock = clock or SystemClock()
        self.boot = identity()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema','1')")
        if (
            self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
            != "1"
        ):
            raise RuntimeError("unsupported storage schema")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def event(self, episode_id, kind, payload):
        event_id = identity()
        self.db.execute(
            "INSERT INTO events(id,episode_id,kind,utc,mono,boot,payload) VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                episode_id,
                kind,
                self.clock.utc(),
                self.clock.mono(),
                self.boot,
                canonical(payload),
            ),
        )
        return event_id

    def episode(self, owner, profile="attention.v1", provenance="physical"):
        if profile not in {"attention.v1", "physical_task.v1"}:
            raise Rejected("unsupported episode profile")
        if provenance not in {"physical", "synthetic", "recorded"}:
            raise Rejected("invalid provenance")
        episode_id = identity()
        with self.transaction():
            self.db.execute(
                "INSERT INTO episodes VALUES(?,?,?,?,?,?,NULL)",
                (episode_id, owner, profile, provenance, "active", self.clock.utc()),
            )
            self.event(
                episode_id,
                "episode.started",
                {"profile": profile, "provenance": provenance},
            )
        return episode_id

    def require_episode(self, episode_id, owner):
        row = self.db.execute(
            "SELECT * FROM episodes WHERE id=? AND owner=?", (episode_id, owner)
        ).fetchone()
        if row is None or row["state"] != "active":
            raise Rejected("active owned episode required")
        return row

    def end_episode(self, episode_id, owner, outcome):
        if outcome not in {"completed", "failed", "cancelled", "timeout", "unknown"}:
            raise Rejected("invalid outcome")
        with self.transaction():
            self.require_episode(episode_id, owner)
            pending = self.db.execute(
                "SELECT 1 FROM reminders WHERE episode_id=? AND state NOT IN ('delivered','cancelled')",
                (episode_id,),
            ).fetchone()
            commands = self.db.execute(
                "SELECT 1 FROM commands WHERE episode_id=? AND state IN ('pending','accepted','executing','cancel_requested')",
                (episode_id,),
            ).fetchone()
            if pending or commands:
                raise Rejected("resolve active work before ending episode")
            self.event(episode_id, "episode.ended", {"outcome": outcome})
            self.db.execute(
                "UPDATE episodes SET state=?,ended=? WHERE id=?",
                (outcome, self.clock.utc(), episode_id),
            )

    def replay(self, owner, key, body):
        row = self.db.execute(
            "SELECT * FROM operations WHERE owner=? AND key=?", (owner, key)
        ).fetchone()
        if row and row["digest"] != digest(body):
            raise Rejected("idempotency key reused with different content")
        if row:
            import json

            return json.loads(row["result"])
        return None

    def remember(self, owner, key, body, result):
        self.db.execute(
            "INSERT INTO operations VALUES(?,?,?,?)",
            (owner, key, digest(body), canonical(result)),
        )

    def grant(self, owner, scope, destination, enabled, expires):
        from .contracts import finite

        finite(expires, self.clock.utc(), self.clock.utc() + 366 * 86400)
        if type(enabled) is not bool or expires <= self.clock.utc():
            raise Rejected("invalid grant")
        with self.transaction():
            self.db.execute(
                "INSERT INTO grants VALUES(?,?,?,1,?,?) ON CONFLICT(owner,scope,destination) DO UPDATE SET revision=revision+1,enabled=excluded.enabled,expires=excluded.expires",
                (owner, scope, destination, int(enabled), expires),
            )
            self.event(
                None,
                "permission.changed",
                {
                    "owner": owner,
                    "scope": scope,
                    "destination": destination,
                    "enabled": enabled,
                },
            )

    def permission(self, owner, scope, destination):
        row = self.db.execute(
            "SELECT * FROM grants WHERE owner=? AND scope=? AND destination=?",
            (owner, scope, destination),
        ).fetchone()
        if not row or not row["enabled"] or row["expires"] <= self.clock.utc():
            raise Rejected("permission absent, expired or revoked")
        return row["revision"]

    def recover(self):
        """Never redispatch an uncertain physical operation or replay speech on restart."""
        with self.transaction():
            self.db.execute("UPDATE leases SET expires=0")
            # A process restart cannot turn a persisted task into fresh motor authority.
            self.db.execute(
                "UPDATE grants SET enabled=0,revision=revision+1 WHERE scope='robot.command' AND enabled=1"
            )
            rows = self.db.execute(
                "SELECT id,episode_id FROM commands WHERE state IN ('pending','accepted','executing','cancel_requested')"
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE commands SET state='unknown',evidence='none' WHERE id=?",
                    (row["id"],),
                )
                self.db.execute(
                    "UPDATE outbox SET state='reconcile' WHERE id=?", (row["id"],)
                )
                self.event(
                    row["episode_id"],
                    "action.recovery_unknown",
                    {"command_id": row["id"]},
                )
            self.db.execute(
                "UPDATE reminders SET state='pending',generation=generation+1,ack_token=NULL,ack_until=NULL WHERE state IN ('attention','awaiting_ack')"
            )
            self.db.execute(
                "UPDATE reminders SET state='unknown',generation=generation+1,ack_token=NULL,ack_until=NULL WHERE state='delivering'"
            )
            self.db.execute(
                "UPDATE task_operations SET state='unknown' WHERE state IN ('admitted','working')"
            )
            self.db.execute(
                "UPDATE embodied_tasks SET state='unknown',updated=? "
                "WHERE state IN ('accepted','working')",
                (self.clock.utc(),),
            )
            self.event(None, "runtime.recovered", {"uncertain_commands": len(rows)})

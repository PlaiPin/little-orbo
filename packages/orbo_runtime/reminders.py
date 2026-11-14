from .contracts import Rejected, finite, identity


class Reminders:
    def __init__(self, store):
        self.store = store

    def create(self, owner, episode_id, key, text, due):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
            raise Rejected("reminder text length")
        body = {"action": "create", "episode": episode_id, "text": text, "due": due}
        with self.store.transaction() as db:
            replay = self.store.replay(owner, key, body)
            if replay is not None:
                return replay
            finite(due, self.store.clock.utc(), self.store.clock.utc() + 366 * 86400)
            self.store.require_episode(episode_id, owner)
            reminder_id = identity()
            db.execute(
                "INSERT INTO reminders(id,owner,episode_id,text,due,state,updated) VALUES(?,?,?,?,?,'pending',?)",
                (reminder_id, owner, episode_id, text, due, self.store.clock.utc()),
            )
            self.store.event(
                episode_id,
                "reminder.saved",
                {"reminder_id": reminder_id, "due": due, "text": text},
            )
            result = {"reminder_id": reminder_id, "state": "pending"}
            self.store.remember(owner, key, body, result)
            return result

    def get(self, owner, reminder_id):
        row = self.store.db.execute(
            "SELECT * FROM reminders WHERE id=? AND owner=?", (reminder_id, owner)
        ).fetchone()
        if not row:
            raise Rejected("unknown reminder")
        return dict(row)

    def change(self, owner, reminder_id, key, *, due=None):
        action = "cancel" if due is None else "defer"
        body = {"action": action, "reminder_id": reminder_id, "due": due}
        with self.store.transaction() as db:
            replay = self.store.replay(owner, key, body)
            if replay is not None:
                return replay
            row = self.get(owner, reminder_id)
            if row["state"] in {"cancelled", "delivered"}:
                raise Rejected("terminal reminder")
            if due is not None:
                finite(
                    due,
                    self.store.clock.utc() + 1,
                    self.store.clock.utc() + 366 * 86400,
                )
            state = "cancelled" if due is None else "pending"
            db.execute(
                "UPDATE reminders SET state=?,due=?,generation=generation+1,ack_token=NULL,ack_until=NULL,updated=? WHERE id=?",
                (
                    state,
                    row["due"] if due is None else due,
                    self.store.clock.utc(),
                    reminder_id,
                ),
            )
            self.store.event(row["episode_id"], f"reminder.{action}", body)
            result = {"reminder_id": reminder_id, "state": state}
            self.store.remember(owner, key, body, result)
            return result

    def claim_due(self):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM reminders WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 1",
                (self.store.clock.utc(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE reminders SET state='attention',generation=generation+1 WHERE id=?",
                (row["id"],),
            )
            self.store.event(
                row["episode_id"], "reminder.due", {"reminder_id": row["id"]}
            )
            return self.get(row["owner"], row["id"])

    def await_ack(self, owner, reminder_id, generation, command_id, timeout=30):
        finite(timeout, 1, 120)
        with self.store.transaction() as db:
            row = self.get(owner, reminder_id)
            if row["generation"] != generation or row["state"] != "attention":
                raise Rejected("stale attention")
            command = db.execute(
                "SELECT * FROM commands WHERE id=? AND episode_id=?",
                (command_id, row["episode_id"]),
            ).fetchone()
            if (
                not command
                or command["capability"] != "attention.gesture"
                or command["state"] not in {"accepted", "executing", "completed"}
            ):
                raise Rejected("attention command admission missing")
            token = identity()
            db.execute(
                "UPDATE reminders SET state='awaiting_ack',ack_token=?,ack_until=?,command_id=? WHERE id=?",
                (token, self.store.clock.utc() + timeout, command_id, reminder_id),
            )
            self.store.event(
                row["episode_id"],
                "attention.awaiting_ack",
                {
                    "reminder_id": reminder_id,
                    "generation": generation,
                    "command_id": command_id,
                },
            )
            return token

    def acknowledge(self, owner, reminder_id, token):
        with self.store.transaction() as db:
            row = self.get(owner, reminder_id)
            if (
                row["state"] != "awaiting_ack"
                or not token
                or row["ack_token"] != token
                or row["ack_until"] <= self.store.clock.utc()
            ):
                raise Rejected("stale or invalid acknowledgment")
            db.execute(
                "UPDATE reminders SET state='delivering',ack_token=NULL WHERE id=?",
                (reminder_id,),
            )
            self.store.event(
                row["episode_id"],
                "attention.acknowledged",
                {"reminder_id": reminder_id, "generation": row["generation"]},
            )
            return row["text"], row["generation"]

    def playback_result(self, owner, reminder_id, generation, completed, frames):
        if (
            type(completed) is not bool
            or type(frames) is not int
            or frames < 0
            or (completed and frames == 0)
        ):
            raise Rejected("invalid playback evidence")
        with self.store.transaction() as db:
            row = self.get(owner, reminder_id)
            if row["state"] != "delivering" or row["generation"] != generation:
                raise Rejected("obsolete playback result")
            state = "delivered" if completed else "unknown"
            db.execute("UPDATE reminders SET state=? WHERE id=?", (state, reminder_id))
            self.store.event(
                row["episode_id"],
                "speech.playback",
                {
                    "reminder_id": reminder_id,
                    "state": state,
                    "frames": frames,
                    "evidence": "software_playback",
                },
            )

    def expire_acknowledgments(self, defer_seconds=300):
        finite(defer_seconds, 1, 86400)
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM reminders WHERE state='awaiting_ack' AND ack_until<=?",
                (self.store.clock.utc(),),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE reminders SET state='pending',due=?,generation=generation+1,ack_token=NULL,ack_until=NULL WHERE id=?",
                    (self.store.clock.utc() + defer_seconds, row["id"]),
                )
                self.store.event(
                    row["episode_id"],
                    "attention.no_ack",
                    {"reminder_id": row["id"], "deferred_seconds": defer_seconds},
                )

    def set_preference(
        self, owner, episode_id, key, value, expected_version, operation_key
    ):
        if key != "attention" or value not in {"gesture_then_speech", "silent"}:
            raise Rejected("unsupported preference")
        body = {
            "episode": episode_id,
            "key": key,
            "value": value,
            "version": expected_version,
        }
        with self.store.transaction() as db:
            replay = self.store.replay(owner, operation_key, body)
            if replay is not None:
                return replay
            self.store.require_episode(episode_id, owner)
            self.store.permission(owner, "preferences.write", "local")
            row = db.execute(
                "SELECT version FROM preferences WHERE owner=? AND key=?", (owner, key)
            ).fetchone()
            current = row[0] if row else 0
            if type(expected_version) is not int or expected_version != current:
                raise Rejected("preference version conflict")
            authority = self.store.event(
                episode_id, "preference.authorized", {"owner": owner, **body}
            )
            db.execute(
                "INSERT INTO preferences VALUES(?,?,?,?,?) ON CONFLICT(owner,key) DO UPDATE SET version=excluded.version,value=excluded.value,authority=excluded.authority",
                (owner, key, current + 1, value, authority),
            )
            result = {"version": current + 1, "value": value}
            self.store.remember(owner, operation_key, body, result)
            return result

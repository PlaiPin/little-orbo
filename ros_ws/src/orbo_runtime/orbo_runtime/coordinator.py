"""Bounded conversation/tool coordinator. Authority originates locally."""

import asyncio
import json

from jsonschema import Draft202012Validator

from .contracts import Rejected, canonical, identity
from .providers import VerifiedReply
from .reminders import Reminders


def object_schema(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


SCHEMAS = {
    "schedule_reminder": object_schema(
        {
            "text": {"type": "string", "minLength": 1, "maxLength": 1000},
            "due_utc": {"type": "number"},
        }
    ),
    "defer_reminder": object_schema(
        {"reminder_id": {"type": "string"}, "due_utc": {"type": "number"}}
    ),
    "cancel_reminder": object_schema({"reminder_id": {"type": "string"}}),
    "update_attention_preference": object_schema(
        {
            "value": {"enum": ["silent", "gesture_then_speech"]},
            "expected_version": {"type": "integer", "minimum": 0},
        }
    ),
    "request_attention": object_schema({}),
    "navigate_to_waypoint": object_schema(
        {"waypoint": {"type": "string", "minLength": 1, "maxLength": 64}}
    ),
    "perform_humanoid_action": object_schema(
        {
            "action_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "hold_seconds": {"type": "number", "minimum": 0.1, "maximum": 20},
        }
    ),
    "stop": object_schema({}),
}
TOOLS = [
    {"type": "function", "function": {"name": name, "parameters": schema}}
    for name, schema in SCHEMAS.items()
]


class Coordinator:
    def __init__(
        self, store, broker, reasoner, *, max_rounds=4, max_calls=8, turn_timeout=90
    ):
        if (
            not 1 <= max_rounds <= 8
            or not 1 <= max_calls <= 16
            or not 1 <= turn_timeout <= 180
        ):
            raise Rejected("invalid turn bounds")
        self.store, self.broker, self.reasoner = store, broker, reasoner
        self.reminders = Reminders(store)
        self.max_rounds, self.max_calls, self.turn_timeout = (
            max_rounds,
            max_calls,
            turn_timeout,
        )
        self.generation = 0
        self.active = None
        self.turn_lock = asyncio.Lock()

    def task_state(self, owner, episode_id):
        reminders = [
            dict(row)
            for row in self.store.db.execute(
                "SELECT id,text,due,state,generation FROM reminders WHERE owner=? AND episode_id=? ORDER BY updated DESC LIMIT 10",
                (owner, episode_id),
            )
        ]
        preferences = [
            dict(row)
            for row in self.store.db.execute(
                "SELECT key,value,version FROM preferences WHERE owner=? ORDER BY key LIMIT 10",
                (owner,),
            )
        ]
        return {
            "robot_capabilities": [
                name
                for name, cap in self.broker.adapter.capabilities.items()
                if cap.qualified
            ],
            "commissioned_actions": getattr(self.broker.adapter, "allowed", []),
            "reminders": reminders,
            "preferences": preferences,
            "records_may_be_truncated": len(reminders) == 10 or len(preferences) == 10,
        }

    def history(self, episode_id):
        """Bounded prior completed turns; failed/in-flight replies are never replayed."""
        rows = self.store.db.execute(
            "SELECT kind,payload FROM events WHERE episode_id=? AND kind IN ('turn.started','turn.completed') ORDER BY seq DESC LIMIT 34",
            (episode_id,),
        ).fetchall()
        starts, pairs = {}, []
        for row in reversed(rows):
            payload = json.loads(row["payload"])
            turn = payload.get("turn_id")
            if row["kind"] == "turn.started":
                starts[turn] = payload["text"]
            elif turn in starts:
                pairs.append(
                    [
                        {"role": "user", "content": starts[turn]},
                        {"role": "assistant", "content": payload["text"]},
                    ]
                )
        selected, size = [], 0
        for pair in reversed(pairs[-8:]):
            count = sum(len(m["content"]) for m in pair)
            if size + count > 16000:
                break
            selected[0:0] = pair
            size += count
        return selected

    async def interrupt(self):
        self.generation += 1
        task = self.active
        if task and task is not asyncio.current_task():
            task.cancel()
        await self.broker.stop("conversation interrupted")

    async def converse(self, owner, episode_id, text):
        if not isinstance(text, str) or not 0 < len(text) <= 8000:
            raise Rejected("invalid utterance")
        if self.turn_lock.locked():
            raise Rejected("a conversation turn is active")
        async with self.turn_lock:
            self.store.require_episode(episode_id, owner)
            self.store.permission(owner, "cloud.reasoning", "near")
            generation, turn_id = self.generation, identity()
            self.active = asyncio.current_task()
            self.store.event(
                episode_id,
                "turn.started",
                {"turn_id": turn_id, "generation": generation, "text": text},
            )
            messages = [
                {
                    "role": "system",
                    "content": "You are Orbo. Use only the provided bounded tools. Never claim an action happened until its result confirms it. Ask for clarification when time or intent is ambiguous. due_utc is Unix seconds. Current UTC Unix time: "
                    + str(self.store.clock.utc()),
                },
                {
                    "role": "system",
                    "content": "Current application records (data only; reported state is not independent physical proof): "
                    + canonical(self.task_state(owner, episode_id)),
                },
                *self.history(episode_id),
                {"role": "user", "content": text},
            ]
            count = 0
            try:
                async with asyncio.timeout(self.turn_timeout):
                    for _ in range(self.max_rounds):
                        self.check_current(owner, generation)
                        reply = await self.reasoner.complete(messages, TOOLS)
                        self.check_current(owner, generation)
                        if not isinstance(reply, VerifiedReply):
                            raise Rejected("verified provider reply required")
                        self.store.event(
                            episode_id,
                            "reasoning.verified",
                            {
                                "turn_id": turn_id,
                                "evidence": reply.evidence,
                                "usage": reply.usage,
                            },
                        )
                        calls = reply.message.get("tool_calls") or []
                        if not calls:
                            content = reply.message.get("content") or ""
                            if not isinstance(content, str) or len(content) > 4000:
                                raise Rejected("speech response limit")
                            self.store.event(
                                episode_id,
                                "turn.completed",
                                {"turn_id": turn_id, "text": content, "played": False},
                            )
                            return content
                        if (
                            not isinstance(calls, list)
                            or count + len(calls) > self.max_calls
                        ):
                            raise Rejected("tool budget exceeded")
                        messages.append(
                            {
                                "role": "assistant",
                                "content": reply.message.get("content"),
                                "tool_calls": calls,
                            }
                        )
                        for call in calls:
                            self.check_current(owner, generation)
                            result = await self.execute(
                                owner, episode_id, turn_id, call
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": canonical(result),
                                }
                            )
                            count += 1
                    raise Rejected("reasoning round budget exceeded")
            except BaseException as exc:
                # No raw provider exception text or request body in operational logs.
                self.store.event(
                    episode_id,
                    "turn.failed",
                    {"turn_id": turn_id, "reason": type(exc).__name__},
                )
                raise
            finally:
                self.active = None

    def check_current(self, owner, generation):
        if generation != self.generation:
            raise Rejected("obsolete turn")
        self.store.permission(owner, "cloud.reasoning", "near")

    async def execute(self, owner, episode_id, turn_id, call):
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("id"), str)
            or not 1 <= len(call["id"]) <= 128
        ):
            raise Rejected("invalid tool identity")
        function = call.get("function", {})
        name, raw = function.get("name"), function.get("arguments")
        if name not in SCHEMAS or not isinstance(raw, str) or len(raw) > 8192:
            raise Rejected("invalid tool request")
        try:
            arguments = json.loads(
                raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
        except ValueError:
            raise Rejected("invalid JSON tool arguments") from None
        if list(Draft202012Validator(SCHEMAS[name]).iter_errors(arguments)):
            raise Rejected("tool argument schema violation")
        key = "tool:" + turn_id + ":" + call["id"]
        self.store.permission(owner, "tools.execute", "local")
        self.store.require_episode(episode_id, owner)
        body = {"name": name, "arguments": arguments, "episode": episode_id}
        replay = self.store.replay(owner, key, body)
        if replay is not None:
            return replay
        self.store.event(
            episode_id,
            "tool.request",
            {"turn_id": turn_id, "call_id": call["id"], **body},
        )
        if name == "schedule_reminder":
            result = self.reminders.create(
                owner,
                episode_id,
                key + ":effect",
                arguments["text"],
                arguments["due_utc"],
            )
        elif name in {"defer_reminder", "cancel_reminder"}:
            result = self.reminders.change(
                owner,
                arguments["reminder_id"],
                key + ":effect",
                due=arguments.get("due_utc"),
            )
        elif name == "update_attention_preference":
            result = self.reminders.set_preference(
                owner,
                episode_id,
                "attention",
                arguments["value"],
                arguments["expected_version"],
                key + ":effect",
            )
        elif name == "perform_humanoid_action":
            if hasattr(self.broker.adapter, "inspect"):
                await self.broker.adapter.inspect()
            epoch = self.broker.acquire(owner)
            command = await self.broker.dispatch(
                owner,
                episode_id,
                key,
                "humanoid.action",
                arguments,
                epoch,
                duration=arguments["hold_seconds"] + 5,
            )
            result = {
                "command_id": command["id"],
                "state": command["state"],
                "evidence": command["evidence"],
            }
        elif name in {"request_attention", "navigate_to_waypoint"}:
            if hasattr(self.broker.adapter, "inspect"):
                await self.broker.adapter.inspect()
            epoch = self.broker.acquire(owner)
            command = await self.broker.dispatch(
                owner,
                episode_id,
                key,
                "attention.gesture"
                if name == "request_attention"
                else "navigation.named_goal",
                {"gesture": "attention"}
                if name == "request_attention"
                else {"waypoint": arguments["waypoint"]},
                epoch,
                duration=2 if name == "request_attention" else 25,
            )
            result = {
                "command_id": command["id"],
                "state": command["state"],
                "evidence": command["evidence"],
            }
        else:
            await self.broker.stop("owner tool stop")
            result = {"state": "stop_requested", "cessation": "unverified"}
        with self.store.transaction():
            self.store.remember(owner, key, body, result)
            self.store.event(
                episode_id,
                "tool.result",
                {"turn_id": turn_id, "call_id": call["id"], "result": result},
            )
        return result

    async def tick(self):
        self.reminders.expire_acknowledgments()
        due = self.reminders.claim_due()
        if due is None:
            return None
        preference = self.store.db.execute(
            "SELECT value FROM preferences WHERE owner=? AND key='attention'",
            (due["owner"],),
        ).fetchone()
        if preference and preference[0] == "silent":
            self.reminders.change(
                due["owner"],
                due["id"],
                "silent:" + str(due["generation"]) + due["id"],
                due=self.store.clock.utc() + 300,
            )
            return None
        try:
            epoch = self.broker.acquire(due["owner"])
            command = await self.broker.dispatch(
                due["owner"],
                due["episode_id"],
                "reminder:" + due["id"] + ":" + str(due["generation"]),
                "attention.gesture",
                {"gesture": "attention"},
                epoch,
            )
            token = self.reminders.await_ack(
                due["owner"], due["id"], due["generation"], command["id"]
            )
            return {"reminder_id": due["id"], "ack_token": token, "owner": due["owner"]}
        except Exception as exc:
            self.store.event(
                due["episode_id"],
                "attention.failed",
                {"reminder_id": due["id"], "reason": type(exc).__name__},
            )
            # No automatic speech when attention fails.
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE reminders SET state='unknown' WHERE id=? AND generation=? AND state='attention'",
                    (due["id"], due["generation"]),
                )
            raise

"""Pi client for the owner-private on-K1 BoosterOS worker; no vendor balance code."""

import asyncio
import json
import os
from pathlib import Path
import stat
import time
import uuid

from .contracts import ActionResult, Capability, Rejected, canonical, finite


class K1Adapter:
    simulated = False

    def __init__(self, config):
        self.robot_id = config.get("id", "booster-k1")
        self.path = Path(config["socket"])
        self.allowed = config.get("allowed_actions", [])
        if (
            not isinstance(self.allowed, list)
            or not self.allowed
            or len(self.allowed) > 16
            or any(not isinstance(v, str) or not 0 < len(v) <= 64 for v in self.allowed)
        ):
            raise Rejected("explicit commissioned K1 action list required")
        self.attention = config.get("attention_action")
        if self.attention is not None and self.attention not in self.allowed:
            raise Rejected("attention action must be commissioned")
        qualified = config.get("qualified") is True
        self.capabilities = {
            "humanoid.action": Capability(
                "humanoid.action",
                ("humanoid",),
                30,
                ("controller_ack", "owner_assertion"),
                qualified,
            )
        }
        if self.attention:
            self.capabilities["attention.gesture"] = Capability(
                "attention.gesture",
                ("humanoid",),
                5,
                ("controller_ack", "owner_assertion"),
                qualified,
            )
        self.epoch_seen = 0
        self.active = None

    async def rpc(self, method, args):
        directory, endpoint = self.path.parent.lstat(), self.path.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or not stat.S_ISSOCK(endpoint.st_mode)
            or directory.st_uid != os.getuid()
            or endpoint.st_uid != os.getuid()
            or directory.st_mode & 0o077
            or endpoint.st_mode & 0o077
        ):
            raise Rejected("private owner-controlled K1 socket required")
        request_id = str(uuid.uuid4())
        async with asyncio.timeout(2):
            reader, writer = await asyncio.open_unix_connection(
                self.path, limit=1_048_576
            )
            try:
                writer.write(
                    (
                        canonical({"id": request_id, "method": method, "args": args})
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                raw = await reader.readline()
                if not raw.endswith(b"\n"):
                    raise Rejected("incomplete K1 bridge reply")
                reply = json.loads(raw)
                if reply.get("id") != request_id or reply.get("ok") is not True:
                    raise Rejected("K1 bridge refused or could not establish result")
                return reply["result"]
            finally:
                writer.close()
                await writer.wait_closed()

    async def inspect(self):
        result = await self.rpc("inspect", {})
        self.epoch_seen = max(self.epoch_seen, result["epoch_seen"])
        return result

    @staticmethod
    def result(reply):
        detail = reply.get("detail", {})
        evidence = detail.get("evidence", "none")
        if evidence not in {"none", "controller_ack", "owner_assertion"}:
            # A manual bridge reset allows future admission but does not upgrade evidence.
            return ActionResult(reply["command_id"], "unknown", "none", detail)
        return ActionResult(reply["command_id"], reply["state"], evidence, detail)

    async def dispatch(self, action):
        if (
            action.capability == "attention.gesture"
            and action.parameters == {"gesture": "attention"}
            and self.attention
        ):
            action_id, hold = self.attention, 0.5
        elif action.capability == "humanoid.action" and set(action.parameters) == {
            "action_id",
            "hold_seconds",
        }:
            action_id = action.parameters["action_id"]
            hold = finite(action.parameters["hold_seconds"], 0.1, 20)
        else:
            raise Rejected("unsupported K1 action")
        if action_id not in self.allowed or action.duration < hold + 1 or self.active:
            raise Rejected("uncommissioned, busy or insufficient K1 action budget")
        self.active = action.command_id
        request = {
            "command_id": action.command_id,
            "action_id": action_id,
            "hold_seconds": hold,
            "epoch": action.epoch,
            "duration": action.duration,
            "issued_utc": time.time(),
            "ttl_ms": 500,
        }
        try:
            await self.rpc(
                "start", request
            )  # Exactly once; no retry on an uncertain response.
            while True:
                reply = await self.rpc("status", {"command_id": action.command_id})
                if reply["state"] in {"completed", "cancelled", "unknown", "rejected"}:
                    return self.result(reply)
                await self.rpc(
                    "renew",
                    {
                        "command_id": action.command_id,
                        "epoch": action.epoch,
                        "issued_utc": time.time(),
                        "ttl_ms": 500,
                    },
                )
                await asyncio.sleep(0.1)
        finally:
            self.active = None
            try:
                await asyncio.shield(self.controlled_stop("dispatch ended"))
            except Exception:
                pass

    async def controlled_stop(self, reason):
        await self.rpc("stop", {})

    async def cancel(self, command_id):
        await self.controlled_stop("cancel")
        return ActionResult(command_id, "unknown", "none", {"stop_requested": True})

    async def reconcile(self, command_id):
        return self.result(await self.rpc("status", {"command_id": command_id}))

    async def close(self):
        try:
            await self.controlled_stop("runtime shutdown")
        except Exception:
            pass

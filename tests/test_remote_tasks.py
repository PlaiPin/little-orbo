import hashlib

import pytest

from orbo_runtime.contracts import ManualClock, Rejected
from orbo_runtime.remote_tasks import Principal, RemoteTasks
from orbo_runtime.storage import Store


@pytest.mark.asyncio
async def test_remote_scope_idempotent_delivery_reconnect_ack_and_revocation():
    store = Store(":memory:", ManualClock())
    token = "a" * 40
    principals = [
        Principal(
            "alice",
            "owner",
            ("lab",),
            hashlib.sha256(token.encode()).hexdigest(),
            store.clock.utc() + 60,
        ),
        Principal(
            "bob",
            "owner",
            ("lab",),
            hashlib.sha256(("b" * 40).encode()).hexdigest(),
            store.clock.utc() + 60,
        ),
    ]
    for name in ("alice", "bob"):
        store.grant("owner", "remote.task", name, True, store.clock.utc() + 60)
    calls = []
    task = {"id": "task", "revision": 1, "state": "completed"}

    async def submit(owner, key, request):
        calls.append((owner, key, request))
        return task

    async def cancel(owner, task_id):
        return {"stop_requested": True}

    gateway = RemoteTasks(
        store,
        principals,
        submit=submit,
        get=lambda *_: task,
        cancel=cancel,
        release=lambda *_: {"text": "Present in inspected view; identity unconfirmed."},
    )

    def req(method, args, principal="alice", secret=token):
        return {"principal": principal, "token": secret, "method": method, "args": args}

    with pytest.raises(Rejected):
        await gateway.call(
            req("task.submit", {"key": "one", "request": {"environment": "home"}})
        )
    with pytest.raises(Rejected):
        await gateway.call(req("task.submit", {}, secret="c" * 40))
    await gateway.call(
        req(
            "task.submit",
            {"key": "one", "request": {"environment": "lab", "destination": "public"}},
        )
    )
    assert calls[0][2]["destination"] == "remote:alice"
    first = await gateway.call(req("task.get", {"task_id": "task"}))
    second = await gateway.call(req("task.get", {"task_id": "task"}))
    assert first["delivery_id"] == second["delivery_id"] and not second["acknowledged"]
    with pytest.raises(Rejected):
        await gateway.call(
            req("task.get", {"task_id": "task"}, principal="bob", secret="b" * 40)
        )
    await gateway.call(
        req("delivery.ack", {"task_id": "task", "delivery_id": first["delivery_id"]})
    )
    assert (await gateway.call(req("task.get", {"task_id": "task"})))["acknowledged"]
    store.grant("owner", "remote.task", "alice", False, store.clock.utc() + 60)
    with pytest.raises(Rejected):
        await gateway.call(req("task.get", {"task_id": "task"}))
    store.close()


async def test_remote_recall_reconnect_keeps_exact_initial_delivery():
    store = Store(":memory:", ManualClock())
    token = "d" * 40
    principal = Principal(
        "alice",
        "owner",
        ("lab",),
        hashlib.sha256(token.encode()).hexdigest(),
        store.clock.utc() + 60,
    )
    store.grant("owner", "remote.task", "alice", True, store.clock.utc() + 60)
    task = {"id": "task", "revision": 1, "state": "completed"}

    async def submit(*_):
        return task

    async def cancel(*_):
        return {
            "state": "cancelled",
            "request": {"private": "must never leave"},
            "result": {"private": "secret"},
        }

    gateway = RemoteTasks(
        store,
        [principal],
        submit=submit,
        get=lambda *_: task,
        cancel=cancel,
        release=lambda *_: {
            "text": f"Last seen {store.clock.utc():.0f}",
            "current_truth": False,
        },
    )

    def request(method, args):
        return {"principal": "alice", "token": token, "method": method, "args": args}

    await gateway.call(
        request("task.submit", {"key": "one", "request": {"environment": "lab"}})
    )
    first = await gateway.call(request("task.get", {"task_id": "task"}))
    store.clock.advance(5)
    second = await gateway.call(request("task.get", {"task_id": "task"}))
    assert first == second
    cancelled = await gateway.call(request("task.cancel", {"task_id": "task"}))
    assert "request" not in cancelled and "result" not in cancelled
    store.close()

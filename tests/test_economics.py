import httpx
import pytest

from orbo_runtime.contracts import Rejected
from orbo_runtime.economics import Ledger, NearUsage, fish_period_expense
from orbo_runtime.storage import Store


def test_credit_stages_and_prepaid_costs_stay_separate():
    s = Store(":memory:")
    ledger = Ledger(s)
    for kind, extra in [
        ("award", {}),
        ("credit_received", {"receipt_reference": "receipt-1"}),
        ("credit_applied", {"usage_reference": "usage-1"}),
    ]:
        body = {"amount": "1.25", "instrument": "near_restricted_credit", **extra}
        assert ledger.observe(
            "near", "synthetic", "id-1", kind, body, provenance="fixture"
        )
        assert not ledger.observe(
            "near", "synthetic", "id-1", kind, body, provenance="fixture"
        )
    report = ledger.summary()
    assert len(report["groups"]) == 3
    assert report["cost_coverage"] == "not_established"
    assert fish_period_expense("10", "8", ["5"])["amount"] == "7"
    with pytest.raises(Rejected):
        ledger.observe(
            "fish",
            "synthetic",
            "bad",
            "credit_received",
            {
                "amount": "1",
                "instrument": "near_restricted_credit",
                "receipt_reference": "r",
            },
            provenance="fixture",
        )
    with pytest.raises(Rejected):
        fish_period_expense("1", "2", [])
    s.close()


async def test_usage_fixed_window_overlap_dedup_and_no_host_fallback():
    s = Store(":memory:")
    ledger = Ledger(s)
    requests = []
    row = {"id": "usage-1", "source": "inference", "total_cost_nano_usd": 25}

    async def handler(request):
        requests.append(request)
        assert request.url.host == "cloud-platform.near.ai"
        assert request.url.params["start_time"] == "2026-11-01T00:00:00Z"
        if "cursor" in request.url.params:
            return httpx.Response(200, json={"data": [row], "next_cursor": None})
        return httpx.Response(200, json={"data": [row], "next_cursor": "page-2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        usage = NearUsage(
            "11111111-1111-4111-8111-111111111111",
            ledger,
            client=client,
            token="fixture-token",
        )
        result = await usage.sync("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
        assert result["inserted"] == 1 and result["complete"]
    assert len(requests) == 2
    assert ledger.summary()["groups"][0]["known_amount"] == "25"
    s.close()


async def test_commons_status_credit_reads_are_separate_nonadditive_snapshots():
    from orbo_runtime.economics import CommonsAccounting
    from orbo_runtime.providers import ProviderError

    store = Store(":memory:")
    submission = "11111111-1111-4111-8111-111111111111"
    event = "22222222-2222-4222-8222-222222222222"
    deny = False
    unrelated = False

    async def handler(request):
        assert request.headers["authorization"] == "Bearer fixture-contributor"
        if deny:
            return httpx.Response(403)
        if request.url.path.endswith("submission-status"):
            return httpx.Response(
                200,
                json=[
                    {
                        "submission_id": event if unrelated else submission,
                        "status": "accepted",
                        "credit_points_final": 3.5,
                        "pipeline": {
                            "instruments": [
                                {"atomic_units": "1000000", "payout_state": "sent"}
                            ]
                        },
                    }
                ],
            )
        if request.url.path.endswith("credit-events"):
            return httpx.Response(
                200,
                json=[
                    {
                        "submission_id": submission,
                        "event_id": event,
                        "tenant_id": "synthetic",
                        "credit_points_delta": 2.5,
                    }
                ],
            )
        return httpx.Response(
            200, json={"tenant_id": "synthetic", "credit_points_total": 200.5}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = CommonsAccounting(
            "https://synthetic.invalid",
            "synthetic",
            Ledger(store),
            client=client,
            token="fixture-contributor",
        )
        first = await reader.sync([submission])
        assert first["inserted"] and not (await reader.sync([submission]))["inserted"]
        assert first["received_credit"] == "not_established_by_this_view"
        groups = Ledger(store).summary()["groups"]
        assert len(groups) == 1 and groups[0]["known_amount"] is None
        unrelated = True
        with pytest.raises(Rejected, match="identity"):
            await reader.sync([submission])
        deny = True
        with pytest.raises(ProviderError):
            await reader.sync([submission])
    store.close()

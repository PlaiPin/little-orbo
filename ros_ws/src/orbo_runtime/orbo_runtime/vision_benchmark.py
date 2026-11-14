"""Reproducible vision evaluation that preserves abstentions and failed cases.

The runner is provider agnostic: explicitly supplied candidates receive identical
case inputs, never labels. Scene hashes and the rubric digest bind a run. Fixture
runs qualify scoring/orchestration only, never real-world visual accuracy.
"""

import copy
import time

from .contracts import Rejected, digest


SCENARIOS = {
    "present": {
        "statuses": ["present"],
        "identities": ["exact", "unconfirmed"],
        "positive_supported": True,
    },
    "absent_from_view": {
        "statuses": ["not_visible"],
        "identities": ["not_applicable", "unconfirmed"],
        "positive_supported": False,
    },
    "similar_object": {
        "statuses": ["similar"],
        "identities": ["visual_similarity"],
        "positive_supported": True,
    },
    "occlusion": {
        "statuses": ["unknown", "similar"],
        "identities": ["unconfirmed", "visual_similarity"],
        "positive_supported": False,
    },
    "unsuitable_lighting": {
        "statuses": ["unknown"],
        "identities": ["unconfirmed", "not_applicable"],
        "positive_supported": False,
    },
    "stale_frame": {
        "statuses": ["unknown"],
        "identities": ["unconfirmed", "not_applicable"],
        "positive_supported": False,
    },
    "inaccessible_view": {
        "statuses": ["unknown"],
        "identities": ["unconfirmed", "not_applicable"],
        "positive_supported": False,
    },
}


async def benchmark(cases, candidates, *, provenance, timeout=60):
    """Run explicit async candidate(input)->finding callbacks without retries.

    cases contain id, scenario, input and evidence_hashes. Hashes must bind the
    *same* immutable scene/reference bytes for every model. Callers configure
    permitted provider adapters and task permissions before invoking the runner.
    """
    import asyncio

    if provenance not in {"synthetic", "recorded", "physical"}:
        raise Rejected("explicit benchmark provenance required")
    if not isinstance(cases, list) or not 7 <= len(cases) <= 1000:
        raise Rejected("bounded benchmark with all seven scenarios required")
    if not isinstance(candidates, dict) or not 1 <= len(candidates) <= 8:
        raise Rejected("one to eight explicit model candidates required")
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise Rejected("bounded benchmark timeout required")
    ids = set()
    covered = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "scenario",
            "input",
            "evidence_hashes",
        }:
            raise Rejected("invalid benchmark case")
        if (
            not isinstance(case["id"], str)
            or not case["id"]
            or case["id"] in ids
            or not isinstance(case["scenario"], str)
            or case["scenario"] not in SCENARIOS
            or not isinstance(case["input"], dict)
            or not isinstance(case["evidence_hashes"], list)
            or any(
                not isinstance(h, str)
                or len(h) != 64
                or any(c not in "0123456789abcdef" for c in h)
                for h in case["evidence_hashes"]
            )
        ):
            raise Rejected("invalid benchmark identity or scene digest")
        if (
            case["scenario"] not in {"inaccessible_view", "stale_frame"}
            and not case["evidence_hashes"]
        ):
            raise Rejected("scene evidence hash required")
        ids.add(case["id"])
        covered.add(case["scenario"])
    if covered != set(SCENARIOS):
        raise Rejected("benchmark must retain every frozen scenario")
    frozen = digest({"cases": cases, "rubric": SCENARIOS})
    rows = []
    for model, candidate in candidates.items():
        if not isinstance(model, str) or not model or not callable(candidate):
            raise Rejected("explicit model identifier and callable required")
        for case in cases:
            started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    result = await candidate(copy.deepcopy(case["input"]))
                if (
                    not isinstance(result, dict)
                    or result.get("status")
                    not in {"present", "similar", "not_visible", "unknown"}
                    or result.get("identity")
                    not in {
                        "exact",
                        "visual_similarity",
                        "unconfirmed",
                        "not_applicable",
                    }
                ):
                    raise Rejected("invalid benchmark finding")
                rule = SCENARIOS[case["scenario"]]
                correct = (
                    result["status"] in rule["statuses"]
                    and result["identity"] in rule["identities"]
                )
                unsupported = (
                    result["status"] == "present" and not rule["positive_supported"]
                ) or (result["identity"] == "exact" and case["scenario"] != "present")
                rows.append(
                    {
                        "model": model,
                        "case_id": case["id"],
                        "scenario": case["scenario"],
                        "status": result["status"],
                        "identity": result["identity"],
                        "correct": correct,
                        "unsupported_claim": unsupported,
                        "abstained": result["status"] == "unknown",
                        "failed": False,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
            except (Exception,) as error:
                # No exception text: provider bodies may contain private inputs.
                rows.append(
                    {
                        "model": model,
                        "case_id": case["id"],
                        "scenario": case["scenario"],
                        "correct": False,
                        "unsupported_claim": False,
                        "abstained": False,
                        "failed": True,
                        "failure_type": type(error).__name__,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
    summaries = {}
    for model in candidates:
        subset = [row for row in rows if row["model"] == model]
        summaries[model] = {
            "cases": len(subset),
            "correct": sum(row["correct"] for row in subset),
            "unsupported_claims": sum(row["unsupported_claim"] for row in subset),
            "abstentions": sum(row["abstained"] for row in subset),
            "failures": sum(row["failed"] for row in subset),
        }
    return {
        "manifest_digest": frozen,
        "provenance": provenance,
        "rubric": SCENARIOS,
        "results": rows,
        "summary": summaries,
        "acceptance": {
            "unsupported_claims_max": 0,
            "all_scenarios_required": True,
            "correct_fraction_min": 0.85,
            "qualification": "physical accuracy requires owner-labeled scene inputs; fixtures qualify scoring only",
        },
    }

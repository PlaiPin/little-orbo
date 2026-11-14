"""Frozen process evaluation reconstructed solely from approved delivered events.

A supplied reference label is an annotation, not independent visual proof. This
analysis can count supported abstentions and unsupported claims in a declared
paired fixture/study; it never infers causality from one successful robot episode.
"""

from .contracts import Rejected

QUESTION = "Did clarification or an additional viewpoint prevent an unsupported object-location answer?"
LABELS = {"supported", "unsupported", "unknown"}
CONDITIONS = {"initial_view", "clarification_or_additional_view"}


def validate_label(label):
    if (
        not isinstance(label, dict)
        or set(label)
        != {
            "case_id",
            "condition",
            "claim_label",
            "label_source",
            "abstained",
            "intervention",
            "observation_ids",
        }
        or not isinstance(label["case_id"], str)
        or not 1 <= len(label["case_id"]) <= 128
        or label["condition"] not in CONDITIONS
        or label["claim_label"] not in LABELS
        or label["label_source"]
        not in {"synthetic_fixture", "owner_annotation", "independent_annotation"}
        or type(label["abstained"]) is not bool
        or label["intervention"] not in {"none", "clarification", "additional_view"}
        or not isinstance(label["observation_ids"], list)
        or len(label["observation_ids"]) > 64
        or any(not isinstance(v, str) or not v for v in label["observation_ids"])
    ):
        raise Rejected("invalid frozen process-evaluation label")
    if label["condition"] == "initial_view" and label["intervention"] != "none":
        raise Rejected("initial-view condition cannot include an intervention")
    if label["condition"] != "initial_view" and label["intervention"] == "none":
        raise Rejected("intervention condition requires recorded intervention")
    return label


def analyze_process(package):
    affected = set(package.get("derivation", {}).get("redacted_event_ids", []))
    gaps = [
        event["id"] for event in package["events"] if event["kind"] == "evidence.gap"
    ]
    labels, excluded = [], []
    for event in package["events"]:
        if event["kind"] != "evaluation.object_location":
            continue
        if event["id"] in affected:
            excluded.append(event["id"])
            continue
        try:
            labels.append({"event_id": event["id"], **validate_label(event["payload"])})
        except (Rejected, KeyError, TypeError):
            excluded.append(event["id"])
    return {
        "question": QUESTION,
        "labels": labels,
        "unsupported_claims": sum(
            label["claim_label"] == "unsupported" for label in labels
        ),
        "abstentions": sum(label["abstained"] for label in labels),
        "unknown_labels": sum(label["claim_label"] == "unknown" for label in labels),
        "gap_event_ids": gaps,
        "excluded_label_event_ids": excluded,
        "complete": bool(labels) and not gaps and not affected and not excluded,
        "visual_accuracy": "not_established_without_authorized_images_and_reference_labels",
        "causal_improvement": "not_established",
    }


def compare_delivered(packages):
    """Compare exact delivered packages, rejecting duplicate/confounded case pairs."""
    from .traces import analyze

    if not isinstance(packages, list) or not 1 <= len(packages) <= 128:
        raise Rejected("bounded delivered package set required")
    cases, incomplete = {}, []
    for package in packages:
        analyze(package)  # Validate the ordinary carrier first.
        report = analyze_process(package)
        if not report["complete"]:
            incomplete.append(package["episode"]["id"])
            continue
        for label in report["labels"]:
            case = cases.setdefault(label["case_id"], {})
            if label["condition"] in case:
                raise Rejected(
                    "duplicate case/condition; select one frozen evaluation cohort"
                )
            case[label["condition"]] = {
                **label,
                "provenance": package["episode"]["provenance"],
            }
    pairs, missing = [], []
    for case_id, conditions in sorted(cases.items()):
        if set(conditions) != CONDITIONS:
            missing.append(case_id)
            continue
        before, after = (
            conditions["initial_view"],
            conditions["clarification_or_additional_view"],
        )
        if (before["label_source"], before["provenance"]) != (
            after["label_source"],
            after["provenance"],
        ):
            raise Rejected(
                "paired comparison provenance and annotation source must match"
            )
        pairs.append(
            {
                "case_id": case_id,
                "initial_unsupported": before["claim_label"] == "unsupported",
                "intervention_unsupported": after["claim_label"] == "unsupported",
                "intervention_abstained": after["abstained"],
                "unknown": "unknown" in {before["claim_label"], after["claim_label"]},
                "provenance": before["provenance"],
            }
        )
    return {
        "question": QUESTION,
        "pairs": pairs,
        "unsupported_claim_difference": sum(
            int(p["intervention_unsupported"]) - int(p["initial_unsupported"])
            for p in pairs
            if not p["unknown"]
        ),
        "missing_pairs": missing,
        "incomplete_episode_ids": incomplete,
        "causal_improvement": "not_established",
        "limitations": [
            "Descriptive paired counts, not proof of training or general improvement.",
            "Only delivered annotations are analyzed; no private image sidecars are consulted.",
        ],
    }

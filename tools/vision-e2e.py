#!/usr/bin/env python3
"""Run bounded synthetic-image checks through the production NEAR adapter.

Image labels are evaluation artifacts and are never included in model messages.
Each scene gets one independent request, with no retries or model fallback.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import time

from orbo_runtime.credentials import credential
from orbo_runtime.live_e2e import read_image
from orbo_runtime.providers import NearReasoner

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "docs/engineering/evidence/near-vision"

PROMPT = """Inspect this image for a robot. Describe only visible movable objects on the table. Do not count the table, wall, shadows, or attached packing tape as separate objects. Do not invent hidden objects. Return a JSON object with:
objects: a list of {name, color, count, location, visibility};
relationships: a list of short spatial statements;
people_visible: a boolean;
hidden_contents: what can actually be determined about the contents of any closed container, or null if none;
uncertainties: a list of important uncertainties.
Use simple object names and be concise. No markdown fences."""


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def check_evidence(root):
    """Check archived bytes, object labels and signatures without credentials/network."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    for item in manifest["files"]:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("evidence path escapes bundle")
        data = path.read_bytes()
        if (
            len(data) != item["bytes"]
            or hashlib.sha256(data).hexdigest() != item["sha256"]
        ):
            raise ValueError("evidence bytes changed: " + item["path"])
    labels_bytes = (root / "labels.json").read_bytes()
    if hashlib.sha256(labels_bytes).hexdigest() != manifest["original_labels_sha256"]:
        raise ValueError("labels differ from the pre-inference snapshot")
    if json.loads((root / "inference-prompt.json").read_text())["text"] != PROMPT:
        raise ValueError("runner prompt differs from archived inference prompt")
    labels = json.loads(labels_bytes)
    objects = 0
    for scene in labels["scenes"]:
        record = json.loads(
            (root / "responses" / (scene["id"] + "-response.json")).read_text()
        )
        image = read_image(root / scene["image"])
        if hashlib.sha256(image).hexdigest() != record["image_sha256"]:
            raise ValueError("response is associated with different image bytes")
        answer = json.loads(record["response_text"])
        if answer != record["parsed_response"]:
            raise ValueError("parsed response differs from the exact answer")
        expected = {
            (o["name"], o["color"]): o["count"] for o in scene["expected_objects"]
        }
        actual = {(o["name"], o["color"]): o["count"] for o in answer["objects"]}
        if (
            expected != actual
            or len(answer["objects"]) != len(expected)
            or answer["people_visible"] is not False
        ):
            raise ValueError("recorded recognition does not match scene labels")
        verified = record["verification"]
        receipt = verified["verification"]
        signature = receipt["signature"]
        if (
            verified["model"] != manifest["model"]
            or verified["e2ee"] is not True
            or verified["signature_kind"] != "provider_tee"
            or receipt["id"] != verified["response_id"]
            or signature["signer"]["signing_algo"] != "ed25519"
        ):
            raise ValueError("unexpected verification metadata")
        matching = [
            a
            for a in receipt["attestation_summaries"]
            if a["signer"] == signature["signer"]
        ]
        if not matching:
            raise ValueError("signature has no matching recorded signer")
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(matching[0]["signing_public_key"])
        ).verify(
            bytes.fromhex(signature["signature"]), signature["signed_text"].encode()
        )
        objects += sum(expected.values())
    return {
        "status": "passed",
        "files_verified": len(manifest["files"]),
        "scenes": len(labels["scenes"]),
        "objects_matching_labels": objects,
        "ed25519_signatures_verified": len(labels["scenes"]),
        "network_requests": 0,
        "scope": "Archived bytes, labels and signed hash strings checked; not a fresh TEE attestation or independent plaintext-to-signed-hash replay",
    }


async def run(root, output_root):
    # Freeze labels before calling the model; do not consume them as prompt input.
    label_bytes = (root / "labels.json").read_bytes()
    images = sorted((root / "images").glob("scene-*.png"))
    if not 1 <= len(images) <= 6:
        raise ValueError("one to six synthetic scenes required")
    prepared = [(path, read_image(path)) for path in images]
    output = output_root / ("run-" + str(time.time_ns()))
    output.mkdir(parents=True, mode=0o700)
    save(output / "prompt.json", {"text": PROMPT, "labels_sent_to_model": False})
    key = credential("NEARAI_API_KEY")
    reasoner = NearReasoner(timeout=120)
    report = {
        "schema": "orbo.synthetic-vision-e2e.v1",
        "model": reasoner.model,
        "provenance": {"images": "synthetic", "provider": "live_service"},
        "labels_sha256": hashlib.sha256(label_bytes).hexdigest(),
        "labels_sent_to_model": False,
        "results": [],
    }
    try:
        for path, image_bytes in prepared:
            started = time.monotonic()
            item = {
                "scene": path.stem,
                "image": str(path.resolve()),
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "image_bytes": len(image_bytes),
            }
            try:
                reply = await reasoner.complete(
                    [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": PROMPT},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,"
                                        + base64.b64encode(image_bytes).decode()
                                    },
                                },
                            ],
                        }
                    ],
                    [],
                )
                content = reply.message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("vision response is empty")
                item.update(
                    {
                        "status": "verified_response_received",
                        "response_text": content,
                        "usage": reply.usage,
                        "verification": reply.evidence,
                    }
                )
                try:
                    item["parsed_response"] = json.loads(content)
                except ValueError:
                    item["format_error"] = "response is not a JSON object"
            except Exception as error:
                # Store no provider exception body, which may contain request data.
                item.update({"status": "failed", "error_type": type(error).__name__})
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            serialized = json.dumps(item)
            if key in serialized:
                raise RuntimeError("refusing to save a credential in the report")
            save(output / (path.stem + "-response.json"), item)
            report["results"].append(item)
            save(output / "report.json", report)
            print(
                json.dumps(
                    {
                        k: item[k]
                        for k in (
                            "scene",
                            "status",
                            "elapsed_seconds",
                            "response_text",
                            "error_type",
                            "usage",
                        )
                        if k in item
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if item["status"] == "failed":
                break
        report["transport_passed"] = len(report["results"]) == len(images) and all(
            result["status"] == "verified_response_received"
            for result in report["results"]
        )
        report["visual_accuracy"] = "requires comparison with labels.json"
        assert (root / "labels.json").read_bytes() == label_bytes, (
            "labels changed during inference"
        )
    finally:
        await reasoner.close()
        save(output / "report.json", report)
        print(
            json.dumps({"report": str((output / "report.json").resolve())}), flush=True
        )
    return report["transport_passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--check-evidence", action="store_true")
    parser.add_argument(
        "--directory",
        type=Path,
        default=BUNDLE,
    )
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/vision-e2e")
    args = parser.parse_args()
    if args.check_evidence:
        print(json.dumps(check_evidence(args.directory.resolve()), indent=2))
        return
    if not args.allow_cloud:
        parser.error("--allow-cloud is required for billed image requests")
    os.umask(0o077)
    raise SystemExit(
        0 if asyncio.run(run(args.directory.resolve(), args.output.resolve())) else 1
    )


if __name__ == "__main__":
    main()

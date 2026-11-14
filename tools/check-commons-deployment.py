#!/usr/bin/env python3
"""Check Commons build identity and optionally exact accepted evidence, without writes."""

import argparse
import asyncio
import json
from pathlib import Path

from orbo_runtime.commons_deployment import (
    check_deployment,
    checked_origin,
    release_identity,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--submission-id")
    parser.add_argument("--digest")
    parser.add_argument("--purpose", default="embodied_process")
    parser.add_argument("--privacy-risk", choices=("low", "medium"), default="low")
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        identity = release_identity(manifest)
        checked_origin(args.origin)
        if bool(args.submission_id) != bool(args.digest):
            parser.error("--submission-id and --digest are required together")
        if not args.allow_network:
            print(
                json.dumps(
                    {
                        "preflight": "passed",
                        "build_commit": identity,
                        "network": "not_run",
                        "deployment": "not_checked",
                    }
                )
            )
            return
        report = asyncio.run(
            check_deployment(
                args.origin,
                manifest,
                socket=args.socket,
                submission_id=args.submission_id,
                digest=args.digest,
                purpose=args.purpose,
                privacy_risk=args.privacy_risk,
            )
        )
    except Exception as error:
        # Provider/daemon exceptions can contain URLs, evidence or credentials.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()

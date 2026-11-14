#!/usr/bin/env python3
"""Apply the declared generic or complete Orbo patch series to pinned Commons."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations/trace_commons"
UPSTREAM = "https://github.com/TraceCommons/trace-commons.git"
MARKER = ".orbo-patches.json"


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def output(*args, cwd):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def load_series(directory, profile):
    manifest = json.loads((directory / "series.json").read_text())
    if manifest.get("schema") != "orbo.commons-patch-series.v1":
        raise ValueError("unsupported patch-series schema")
    groups = {}
    for group in ("generic", "orbo"):
        names = manifest[group]
        if not isinstance(names, list) or not names:
            raise ValueError("patch group must be a nonempty ordered list")
        groups[group] = []
        for name in names:
            if not isinstance(name, str):
                raise ValueError("patch path must be a string")
            path = Path(name)
            if (
                len(path.parts) != 2
                or path.parts[0] != group
                or path.suffix != ".patch"
                or path.is_absolute()
                or (directory / group).is_symlink()
                or (directory / path).is_symlink()
                or not (directory / path).is_file()
            ):
                raise ValueError("invalid patch path: " + name)
            groups[group].append(directory / path)
    declared = groups["generic"] + groups["orbo"]
    if len(declared) != len(set(declared)):
        raise ValueError("duplicate patch in series")
    if set(directory.rglob("*.patch")) != set(declared):
        raise ValueError("patch files and series manifest differ")
    if profile not in groups:
        raise ValueError("unknown profile")
    return groups["generic"] if profile == "generic" else declared


def changed_paths(source):
    tracked = output("git", "diff", "--name-only", "HEAD", cwd=source).splitlines()
    untracked = output(
        "git", "ls-files", "--others", "--exclude-standard", cwd=source
    ).splitlines()
    return set(tracked + untracked) - {MARKER}


def bootstrap(source, revision, patches, profile, source_url=UPSTREAM):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full lowercase commit hash")
    if not source.exists():
        source.mkdir(parents=True)
        run("git", "init", "-q", str(source))
        run("git", "remote", "add", "origin", source_url, cwd=source)
        run("git", "fetch", "--depth=1", "origin", revision, cwd=source)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=source)
    if (
        Path(output("git", "rev-parse", "--show-toplevel", cwd=source)).resolve()
        != source.resolve()
    ):
        raise ValueError("source directory must be a checkout root")
    if output("git", "rev-parse", "HEAD", cwd=source) != revision:
        raise ValueError("source revision mismatch; existing checkout preserved")
    names = [f"{p.parent.name}/{p.name}" for p in patches]
    patch_hash = hashlib.sha256(b"".join(p.read_bytes() for p in patches)).hexdigest()
    paths = set()
    for patch in patches:
        paths.update(re.findall(r"^\+\+\+ b/(.+)$", patch.read_text(), re.M))
    if any(Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
        raise ValueError("unsafe patched source path")
    marker = source / MARKER
    identity = {
        "schema": "orbo.commons-patched-source.v2",
        "revision": revision,
        "profile": profile,
        "patches": names,
        "patch_sha256": patch_hash,
    }
    if marker.exists():
        previous = json.loads(marker.read_text())
        if any(previous.get(key) != value for key, value in identity.items()):
            raise ValueError(
                "patch series changed; use a fresh directory, existing source preserved"
            )
        if set(previous.get("files", {})) != paths:
            raise ValueError("patched-source manifest is incomplete")
        for path, expected in previous["files"].items():
            file = source / path
            if (
                file.is_symlink()
                or not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("patched source changed: " + path)
        if changed_paths(source) - paths:
            raise ValueError(
                "checkout has additional modifications; existing source preserved"
            )
    else:
        if changed_paths(source):
            raise ValueError("checkout has modifications; use a fresh directory")
        applied = []
        try:
            for patch in patches:
                run(
                    "git",
                    "apply",
                    "--check",
                    "--whitespace=error",
                    str(patch),
                    cwd=source,
                )
                run("git", "apply", "--whitespace=error", str(patch), cwd=source)
                applied.append(patch)
        except subprocess.CalledProcessError:
            # Only reverse patches applied in this invocation to a clean tree.
            for patch in reversed(applied):
                run("git", "apply", "--reverse", str(patch), cwd=source)
            raise
        identity["files"] = {
            p: hashlib.sha256((source / p).read_bytes()).hexdigest()
            for p in sorted(paths)
        }
        marker.write_text(json.dumps(identity, indent=2) + "\n")
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("orbo", "generic"), default="orbo")
    parser.add_argument("--directory", type=Path)
    parser.add_argument(
        "--revision", help="Generic-only upstream review revision; full commit hash"
    )
    args = parser.parse_args()
    if args.revision and args.profile != "generic":
        parser.error("--revision is only supported for the generic review profile")
    source = (args.directory or ROOT / f".sdk/commons-{args.profile}").resolve()
    revision = args.revision or (INTEGRATION / "UPSTREAM_REVISION").read_text().strip()
    try:
        patches = load_series(INTEGRATION / "patches", args.profile)
        bootstrap(source, revision, patches, args.profile)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Commons bootstrap refused: {error}\n")
    print(source)
    print(
        f"Verified {args.profile} series ({len(patches)} patches). No daemon/account actions performed."
    )


if __name__ == "__main__":
    main()

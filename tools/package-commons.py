#!/usr/bin/env python3
"""Build and package the pinned Orbo Commons client and open ingest server."""

import argparse
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bootstrap", ROOT / "tools/bootstrap-commons.py"
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
BINARIES = ("trace-commons-contributor", "trace-commons-ingest")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_identity(marker):
    if marker.get("profile") != "orbo":
        raise ValueError("full Orbo patch profile required")
    return marker["revision"][:12] + "-orbo-" + marker["patch_sha256"][:16]


def source_archive(source, destination, epoch):
    """Only tracked source plus declared patched files; never credentials or build output."""
    paths = set(
        subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"])
        .decode()
        .split("\0")
    ) - {""}
    marker = json.loads((source / bootstrap.MARKER).read_text())
    paths.update(marker["files"])
    paths.add(bootstrap.MARKER)
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=epoch) as compressed,
        tarfile.open(fileobj=compressed, mode="w|") as archive,
    ):
        for name in sorted(paths):
            path = source / name
            if path.is_symlink():
                # Preserve safe repository symlinks, never follow their targets.
                target = os.readlink(path)
                if Path(target).is_absolute() or not path.resolve().is_relative_to(
                    source.resolve()
                ):
                    raise ValueError("source archive refuses escaping symlink")
            item = archive.gettarinfo(str(path), arcname="source/" + name)
            item.uid = item.gid = 0
            item.uname = item.gname = ""
            item.mtime = epoch
            if item.isfile():
                with path.open("rb") as stream:
                    archive.addfile(item, stream)
            else:
                archive.addfile(item)


def package(source, output, profile):
    if output.exists():
        raise ValueError("output exists; choose a fresh release directory")
    revision = (bootstrap.INTEGRATION / "UPSTREAM_REVISION").read_text().strip()
    marker = bootstrap.bootstrap(
        source,
        revision,
        bootstrap.load_series(bootstrap.INTEGRATION / "patches", "orbo"),
        "orbo",
    )
    identity = build_identity(marker)
    epoch = int(
        subprocess.check_output(
            ["git", "-C", str(source), "show", "-s", "--format=%ct", "HEAD"], text=True
        )
    )
    # Keep output isolated to this verified checkout. A caller's shared target can
    # accidentally contain a same-version workspace built from a different tree.
    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(source / "target"),
        "TRACE_COMMONS_BUILD_COMMIT": identity,
        "SOURCE_DATE_EPOCH": str(epoch),
        "CARGO_INCREMENTAL": "0",
        "RUSTFLAGS": "-D warnings",
    }
    command = [
        "cargo",
        "+1.96.0",
        "build",
        "--manifest-path",
        str(source / "Cargo.toml"),
        "--locked",
    ]
    for binary in BINARIES:
        command.extend(["-p", binary, "--bin", binary])
    if profile == "release":
        command.append("--release")
    subprocess.run(command, cwd=source, env=env, check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".commons-package-", dir=output.parent
    ) as tmp:
        staging = Path(tmp)
        (staging / "bin").mkdir()
        binaries = {}
        for binary in BINARIES:
            path = source / "target" / profile / binary
            version = subprocess.check_output(
                [str(path), "--version"], text=True, timeout=30
            ).strip()
            if f"(commit {identity}," not in version:
                raise ValueError("binary identity differs from verified patched source")
            shutil.copy2(path, staging / "bin" / binary)
            binaries[binary] = {"sha256": sha256(path), "version": version}
        subprocess.run(
            [str(staging / "bin/trace-commons-ingest"), "--tls-selfcheck"],
            check=True,
            timeout=30,
        )
        source_archive(source, staging / "source.tar.gz", epoch)
        shutil.copytree(ROOT / "deploy/trace-commons", staging / "deployment")
        manifest = {
            "schema": "orbo.commons-release.v1",
            "build_commit": identity,
            "upstream_revision": revision,
            "patch_sha256": marker["patch_sha256"],
            "patches": marker["patches"],
            "profile": profile,
            "rustc": subprocess.check_output(
                ["rustc", "+1.96.0", "-vV"], text=True
            ).strip(),
            "binaries": binaries,
            "source_sha256": sha256(staging / "source.tar.gz"),
            "features": [],
            "pipeline": "open-build; no proprietary Score/settlement/payout adapters",
        }
        manifest["files"] = {
            str(p.relative_to(staging)): sha256(p)
            for p in sorted(staging.rglob("*"))
            if p.is_file()
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Atomic publication only after both executable identities and TLS pass.
        staging.rename(output)
    return manifest


def verify_release(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != "orbo.commons-release.v1":
        raise ValueError("invalid release manifest")
    expected = manifest.get("files", {})
    if not expected or not set("bin/" + b for b in BINARIES) <= set(expected):
        raise ValueError("incomplete release manifest")
    actual = {
        str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()
    }
    if actual != set(expected) | {"manifest.json"}:
        raise ValueError("release file inventory differs")
    for name, digest in expected.items():
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("unsafe release path")
        if sha256(path) != digest:
            raise ValueError("release hash mismatch")
    if manifest["source_sha256"] != expected.get("source.tar.gz"):
        raise ValueError("source identity differs")
    for binary in BINARIES:
        if manifest["binaries"][binary]["sha256"] != expected["bin/" + binary]:
            raise ValueError("binary identity differs")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".sdk/commons-orbo")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--verify",
        type=Path,
        help="Verify every packaged file without running binaries",
    )
    parser.add_argument("--profile", choices=("debug", "release"), default="release")
    args = parser.parse_args()
    try:
        if args.verify:
            if args.output:
                parser.error("--verify and --output are exclusive")
            result = verify_release(args.verify.resolve())
        else:
            if not args.output:
                parser.error("--output is required when building")
            result = package(args.source.resolve(), args.output.resolve(), args.profile)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Commons packaging failed: {type(error).__name__}\n")
    print(
        json.dumps(
            {
                "output": str((args.verify or args.output).resolve()),
                "build_commit": result["build_commit"],
            }
        )
    )


if __name__ == "__main__":
    main()

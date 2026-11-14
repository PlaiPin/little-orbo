"""Deployment checks must distinguish identity, enrollment, and actual evidence."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile

import httpx
import pytest

from orbo_runtime.commons_deployment import (
    REQUIRED_METHODS,
    check_deployment,
    release_identity,
)
from orbo_runtime.contracts import Rejected

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "commons_package", ROOT / "tools/package-commons.py"
)
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)
MANIFEST = {
    "schema": "orbo.commons-release.v1",
    "upstream_revision": "a" * 40,
    "patch_sha256": "b" * 64,
    "build_commit": "a" * 12 + "-orbo-" + "b" * 16,
}
ORIGIN = "https://commons.example"


def transport(*, wrong_build=False, missing_source=False, huge=False):
    def handle(request):
        assert "authorization" not in request.headers
        if request.url.path == "/health":
            if huge:
                return httpx.Response(200, content=b"x" * 65_537)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "build_commit": "unknown"
                    if wrong_build
                    else MANIFEST["build_commit"],
                    "near_ai_staging": False,
                },
            )
        assert request.url.path == "/v1/source"
        return httpx.Response(
            200,
            json={
                "license": "AGPL-3.0-or-later",
                "source_url": "https://github.com/upstream"
                if missing_source
                else ORIGIN + "/orbo-commons-source.tar.gz",
                "build_commit": MANIFEST["build_commit"],
            },
        )

    return httpx.MockTransport(handle)


async def test_health_does_not_claim_delivery_or_production_pipeline():
    async with httpx.AsyncClient(transport=transport()) as client:
        report = await check_deployment(ORIGIN, MANIFEST, client=client)
    assert report["health"] == "passed"
    assert report["delivery"] == "not_checked"
    assert report["production_pipeline"] == "not_qualified"
    assert report["rewards"] == "not_checked"


@pytest.mark.parametrize("fault", ["wrong_build", "missing_source", "huge"])
async def test_mismatch_missing_corresponding_source_and_unbounded_response_fail(fault):
    async with httpx.AsyncClient(transport=transport(**{fault: True})) as client:
        with pytest.raises(Rejected):
            await check_deployment(ORIGIN, MANIFEST, client=client)


class IPC:
    def __init__(self, *, enrolled=True, methods=REQUIRED_METHODS):
        self.enrolled, self.methods = enrolled, methods
        self.calls = []

    async def call(self, method, params):
        self.calls.append(method)
        if method == "hello":
            return {"methods": list(self.methods)}
        return {
            "logged_in": self.enrolled,
            "setup_complete": False,
            "paused": True,
            "terms_hold": True,
        }


async def test_daemon_inspection_is_read_only_and_reports_holds():
    ipc = IPC()
    async with httpx.AsyncClient(transport=transport()) as client:
        report = await check_deployment(ORIGIN, MANIFEST, ipc=ipc, client=client)
    assert ipc.calls == ["hello", "status"]
    assert report["daemon"] == "enrolled_compatible"
    assert report["paused"] and report["terms_hold"] and not report["setup_complete"]


@pytest.mark.parametrize("ipc", [IPC(enrolled=False), IPC(methods={"status"})])
async def test_unenrolled_and_unpatched_daemon_fail(ipc):
    async with httpx.AsyncClient(transport=transport()) as client:
        with pytest.raises(Rejected):
            await check_deployment(ORIGIN, MANIFEST, ipc=ipc, client=client)


async def test_missing_accepted_submission_is_not_success():
    def handle(request):
        if request.url.path != "/v1/datasets/task-evidence":
            return transport().handle_request(request)
        assert request.headers["authorization"] == "Bearer private-test-token"
        assert (
            request.url.params["submission_ids"]
            == "00000000-0000-0000-0000-000000000001"
        )
        return httpx.Response(
            200,
            headers={"X-Orbo-Task-Selection": request.url.params["submission_ids"]},
            json={
                "export_id": "00000000-0000-0000-0000-000000000002",
                "manifest": {},
                "items": [],
                "item_count": 0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(Rejected, match="not available"):
            await check_deployment(
                ORIGIN,
                MANIFEST,
                client=client,
                token="private-test-token",
                submission_id="00000000-0000-0000-0000-000000000001",
                digest="c" * 64,
            )


@pytest.mark.parametrize(
    "origin",
    [
        "http://commons.example",
        "https://u:p@commons.example",
        "https://commons.example/path",
    ],
)
async def test_invalid_origins_never_make_request(origin):
    def fail(request):
        pytest.fail("no request allowed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(Rejected):
            await check_deployment(origin, MANIFEST, client=client)


def test_source_archive_is_reproducible_and_excludes_untracked_secrets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "Cargo.lock").write_text("locked\n")
    (source / "README.md").write_text("source\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    (source / "patched.rs").write_text("// declared patch\n")
    (source / ".orbo-patches.json").write_text(
        json.dumps({"files": {"patched.rs": "hash"}})
    )
    (source / "credentials.env").write_text("secret")
    outputs = [tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"]
    for output in outputs:
        packager.source_archive(source, output, 123)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with tarfile.open(outputs[0]) as archive:
        assert set(archive.getnames()) == {
            "source/Cargo.lock",
            "source/README.md",
            "source/patched.rs",
            "source/.orbo-patches.json",
        }


def release(tmp_path):
    (tmp_path / "bin").mkdir()
    paths = [tmp_path / "source.tar.gz"] + [
        tmp_path / "bin" / b for b in packager.BINARIES
    ]
    for path in paths:
        path.write_bytes(b"fixture artifact")
    files = {str(p.relative_to(tmp_path)): packager.sha256(p) for p in paths}
    manifest = {
        **MANIFEST,
        "files": files,
        "source_sha256": files["source.tar.gz"],
        "binaries": {b: {"sha256": files["bin/" + b]} for b in packager.BINARIES},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("fault", ["changed", "extra", "missing", "symlink"])
def test_release_verification_refuses_modified_or_incomplete_packages(tmp_path, fault):
    release(tmp_path)
    packager.verify_release(tmp_path)
    path = tmp_path / "bin" / packager.BINARIES[0]
    if fault == "changed":
        path.write_bytes(b"changed")
    elif fault == "extra":
        (tmp_path / "credentials.env").write_text("private")
    elif fault == "missing":
        path.unlink()
    else:
        path.unlink()
        path.symlink_to(tmp_path / "source.tar.gz")
    with pytest.raises(ValueError):
        packager.verify_release(tmp_path)


def test_wrong_manifest_identity_fails():
    with pytest.raises(Rejected):
        release_identity({**MANIFEST, "build_commit": "different"})

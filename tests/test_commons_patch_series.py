"""Patch packaging must preserve separation and reproducible, non-destructive builds."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "commons_bootstrap", ROOT / "tools/bootstrap-commons.py"
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True
    ).strip()


def repository(path):
    path.mkdir()
    git(path, "init", "-q")
    (path / "example.txt").write_text("baseline\n")
    git(path, "add", ".")
    git(
        path,
        "-c",
        "user.name=Patch Test",
        "-c",
        "user.email=patch-test@example.invalid",
        "commit",
        "-qm",
        "Fixture",
    )
    return git(path, "rev-parse", "HEAD")


def patches(path):
    (path / "generic").mkdir(parents=True)
    (path / "orbo").mkdir()
    first = path / "generic/first.patch"
    second = path / "orbo/second.patch"
    first.write_text(
        "--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-baseline\n+generic\n"
    )
    second.write_text(
        "--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-generic\n+orbo\n"
    )
    (path / "series.json").write_text(
        json.dumps(
            {
                "schema": "orbo.commons-patch-series.v1",
                "generic": ["generic/first.patch"],
                "orbo": ["orbo/second.patch"],
            }
        )
    )
    return first, second


def test_real_series_separates_independent_generic_changes():
    directory = ROOT / "integrations/trace_commons/patches"
    generic = bootstrap.load_series(directory, "generic")
    full = bootstrap.load_series(directory, "orbo")
    assert len(generic) == 2 and len(full) == 5
    assert full[:2] == generic
    for patch in generic:
        content = patch.read_text().lower()
        assert "orbo" not in content
        assert "trace-commons-ingest" not in content
        assert "+    async fn" in content  # Regression travels with the fix.
        assert "+++ b/docs/" in content


def test_profile_order_and_idempotent_reentry(tmp_path):
    origin = tmp_path / "origin"
    revision = repository(origin)
    directory = tmp_path / "patches"
    patches(directory)
    for profile, expected in [("generic", "generic\n"), ("orbo", "orbo\n")]:
        checkout = tmp_path / profile
        selected = bootstrap.load_series(directory, profile)
        bootstrap.bootstrap(checkout, revision, selected, profile, str(origin))
        marker = (checkout / bootstrap.MARKER).read_bytes()
        bootstrap.bootstrap(checkout, revision, selected, profile, str(origin))
        assert (checkout / "example.txt").read_text() == expected
        assert (checkout / bootstrap.MARKER).read_bytes() == marker


@pytest.mark.parametrize(
    "fault", ["extra", "duplicate", "escape", "missing", "symlink"]
)
def test_manifest_refuses_ambiguous_or_unsafe_series(tmp_path, fault):
    patches(tmp_path)
    manifest = tmp_path / "series.json"
    value = json.loads(manifest.read_text())
    if fault == "extra":
        (tmp_path / "unlisted.patch").write_text("not declared")
    elif fault == "duplicate":
        value["generic"] *= 2
    elif fault == "escape":
        value["generic"] = ["../first.patch"]
    elif fault == "missing":
        (tmp_path / "generic/first.patch").unlink()
    else:
        original = tmp_path / "generic/first.patch"
        external = tmp_path / "external"
        original.rename(external)
        original.symlink_to(external)
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        bootstrap.load_series(tmp_path, "orbo")


@pytest.mark.parametrize(
    "fault", ["source", "patch", "profile", "manifest", "revision", "extra"]
)
def test_reentry_refuses_changed_checkout_without_overwriting(tmp_path, fault):
    checkout = tmp_path / "checkout"
    revision = repository(checkout)
    first, second = patches(tmp_path / "patches")
    bootstrap.bootstrap(checkout, revision, [first, second], "orbo")
    marker = checkout / bootstrap.MARKER
    if fault == "source":
        (checkout / "example.txt").write_text("owner edit\n")
    elif fault == "patch":
        second.write_text(second.read_text().replace("+orbo", "+changed"))
    elif fault == "manifest":
        value = json.loads(marker.read_text())
        value["files"] = {}
        marker.write_text(json.dumps(value))
    elif fault == "extra":
        (checkout / "extra.txt").write_text("owner file")
    before = (checkout / "example.txt").read_bytes()
    with pytest.raises(ValueError):
        bootstrap.bootstrap(
            checkout,
            "a" * 40 if fault == "revision" else revision,
            [first, second],
            "generic" if fault == "profile" else "orbo",
        )
    assert (checkout / "example.txt").read_bytes() == before


def test_dirty_checkout_and_failed_series_preserve_source(tmp_path):
    checkout = tmp_path / "checkout"
    revision = repository(checkout)
    first, second = patches(tmp_path / "patches")
    (checkout / "owner.txt").write_text("preserve me")
    with pytest.raises(ValueError, match="modifications"):
        bootstrap.bootstrap(checkout, revision, [first, second], "orbo")
    assert (checkout / "owner.txt").read_text() == "preserve me"
    (checkout / "owner.txt").unlink()
    second.write_text(second.read_text().replace("-generic", "-does-not-match"))
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.bootstrap(checkout, revision, [first, second], "orbo")
    assert (checkout / "example.txt").read_text() == "baseline\n"
    assert not (checkout / bootstrap.MARKER).exists()
    assert not bootstrap.changed_paths(checkout)


def test_build_defaults_match_bootstrap_and_old_series_is_not_selected():
    script = (ROOT / "tools/build-commons.sh").read_text()
    assert ".sdk/commons-orbo" in script
    assert ".sdk/commons-locked" not in script
    assert not list((ROOT / "integrations/trace_commons/patches").glob("*.patch"))


def test_existing_directory_must_be_the_checkout_root(tmp_path):
    checkout = tmp_path / "checkout"
    revision = repository(checkout)
    child = checkout / "nested"
    child.mkdir()
    first, _ = patches(tmp_path / "patches")
    with pytest.raises(ValueError, match="checkout root"):
        bootstrap.bootstrap(child, revision, [first], "generic")
    assert (checkout / "example.txt").read_text() == "baseline\n"

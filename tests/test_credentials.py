import json
import os

import pytest

from orbo_runtime import credentials
from orbo_runtime.contracts import Rejected


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = repo / "credentials.toml"
    secret = tmp_path / "private" / "credentials.env"
    secret.parent.mkdir(mode=0o700)
    config.write_text("[credentials]\nfile = " + json.dumps(str(secret)) + "\n")
    monkeypatch.setenv("ORBO_CREDENTIALS_CONFIG", str(config))
    return repo, config, secret


def put(path, content):
    path.write_text(content)
    path.chmod(0o600)


def test_one_file_used_by_provider_consumers_without_environment_fallback(
    setup, monkeypatch
):
    from orbo_runtime.providers import credential

    _, _, secret = setup
    put(secret, 'NEARAI_API_KEY="external-value"\nOPENAI_API_KEY=live-value\n')
    monkeypatch.setenv("NEARAI_API_KEY", "ambient-value")
    monkeypatch.setenv("COMMONS_EXPORT_TOKEN", "must-not-be-used")
    assert credential("NEARAI_API_KEY") == "external-value"
    assert credential("OPENAI_API_KEY") == "live-value"
    with pytest.raises(Rejected, match="missing credential"):
        credential("COMMONS_EXPORT_TOKEN")
    assert credential("COMMONS_EXPORT_TOKEN", required=False) is None


def test_initialization_never_overwrites_existing_secrets(setup):
    _, config, secret = setup
    result = credentials.initialize(config)
    assert result == {"file": str(secret), "created": True}
    assert secret.stat().st_mode & 0o777 == 0o600
    assert set(credentials.read_values()) == set(credentials.NAMES)
    assert all(not value for value in credentials.read_values().values())
    put(secret, "NEARAI_API_KEY=preserve-this-value\n")
    assert credentials.initialize(config)["created"] is False
    assert secret.read_text() == "NEARAI_API_KEY=preserve-this-value\n"


def test_values_are_literal_not_shell_code(setup, tmp_path):
    _, _, secret = setup
    marker = tmp_path / "never-created"
    value = f"$(touch {marker}) $HOME `echo x` # literal"
    put(secret, "NEARAI_API_KEY='" + value + "'\n")
    assert credentials.credential("NEARAI_API_KEY") == value
    assert not marker.exists()


@pytest.mark.parametrize(
    "content",
    [
        "X=one\nX=two\n",
        "export X=one\n",
        'X="unclosed\n',
        "x=lowercase\n",
        "X=a\x00b\n",
    ],
)
def test_bad_assignments_fail_without_exposing_values(setup, content):
    _, _, secret = setup
    put(secret, content)
    with pytest.raises(Rejected) as result:
        credentials.read_values()
    assert content not in str(result.value)


def test_repo_secret_and_symlink_into_repo_are_rejected(setup, tmp_path):
    repo, config, _ = setup
    in_repo = repo / "private.env"
    put(in_repo, "KEY=inside\n")
    for target in (in_repo, tmp_path / "redirect.env"):
        if target != in_repo:
            target.symlink_to(in_repo)
        config.write_text("[credentials]\nfile = " + json.dumps(str(target)))
        with pytest.raises(Rejected, match="outside"):
            credentials.read_values()


def test_public_permissions_and_symlinks_fail_closed(setup, tmp_path):
    _, _, secret = setup
    put(secret, "KEY=secret\n")
    secret.chmod(0o640)
    with pytest.raises(Rejected, match="owner-only"):
        credentials.read_values()
    target = tmp_path / "other.env"
    put(target, "KEY=secret\n")
    secret.unlink()
    secret.symlink_to(target)
    with pytest.raises(Rejected):
        credentials.read_values()


def test_large_file_is_refused(setup):
    _, _, secret = setup
    put(secret, "X=" + "x" * credentials.LIMIT)
    with pytest.raises(Rejected, match="bounded"):
        credentials.read_values()


def test_external_secret_cannot_be_hardlinked_into_repository(setup):
    repo, _, secret = setup
    put(secret, "KEY=private\n")
    os.link(secret, repo / "linked.env")
    with pytest.raises(Rejected, match="owner-only"):
        credentials.read_values()


def test_systemd_copy_uses_same_parser(setup, monkeypatch):
    _, config, secret = setup
    private = secret.parent / "orbo.env"
    put(private, "NEARAI_API_KEY=systemd-value\n")
    config.write_text('[credentials]\nfile = "${CREDENTIALS_DIRECTORY}/orbo.env"\n')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(private.parent))
    assert credentials.credential("NEARAI_API_KEY") == "systemd-value"
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    with pytest.raises(Rejected, match="systemd"):
        credentials.read_values()


def test_status_discloses_only_presence(setup, capsys):
    _, config, secret = setup
    put(secret, "NEARAI_API_KEY=not-for-output\nNEAR_SIGNER_PRIVATE_KEY=also-private\n")
    assert credentials.main(["status", "--config", str(config)]) == 0
    output = capsys.readouterr().out
    assert "not-for-output" not in output and "also-private" not in output
    assert json.loads(output)["configured"]["NEARAI_API_KEY"] is True


def test_default_pointer_discovery_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("ORBO_CREDENTIALS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    found = credentials.config_file()
    assert found.name == "credentials.toml" and found.parent.name == "deploy"
    assert found.is_file()


@pytest.mark.parametrize("value", ["secrets.env", "$HOME/secret.env", ""])
def test_unsupported_pointer_paths_are_rejected(setup, value):
    _, config, _ = setup
    config.write_text("[credentials]\nfile = " + json.dumps(value))
    with pytest.raises(Rejected):
        credentials.read_values()

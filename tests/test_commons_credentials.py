"""Native services consume one secret source without copying native account state."""

import json
from pathlib import Path

import pytest

from orbo_runtime.commons_credentials import (
    check_contributor_settings,
    command_for,
    launch_environment,
)
from orbo_runtime.contracts import Rejected

VALUES = {
    "TRACE_NEAR_AI_PRIVACY_API_KEY": "shared-classifier-key",
    "TRACE_COMMONS_SIGNED_TOKEN_SECRET": "server-secret",
    "DATABASE_URL": "postgres://user:private@localhost/db",
    "TRACE_COMMONS_LOGIN_RESOLVER_DATABASE_URL": "postgres://resolver:private@localhost/db",
    "OPENAI_API_KEY": "unrelated-voice-key",
    "TRACE_COMMONS_INVITE": "private-bootstrap-invite",
    "TRACE_COMMONS_DEVICE_CODE": "private-bootstrap-code",
    "NEAR_INTENTS_PRIVATE_KEY": "unrelated-wallet-key",
}


def test_contributor_gets_only_classifier_values_from_shared_file():
    env = launch_environment(
        "contributor",
        VALUES,
        {
            "PATH": "/bin",
            "AWS_ACCESS_KEY_ID": "unrelated-aws-id",
            "OPENAI_API_KEY": "inherited-voice-key",
            "TRACE_NEAR_AI_PRIVACY_API_KEY": "stale-classifier-key",
            "TRACE_COMMONS_SIGNED_TOKEN_SECRET": "stale-server-secret",
            "DATABASE_URL": "stale-database",
            "RUST_LOG": "warn",
        },
    )
    assert env["TRACE_NEAR_AI_PRIVACY_API_KEY"] == "shared-classifier-key"
    assert env["TRACE_PRIVACY_FILTER_BACKEND"] == "near-ai"
    assert env["PATH"] == "/bin" and env["RUST_LOG"] == "warn"
    assert "OPENAI_API_KEY" not in env and "NEAR_INTENTS_PRIVATE_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "TRACE_COMMONS_SIGNED_TOKEN_SECRET" not in env


def test_ingest_selects_shared_database_issuer_and_classifier_without_other_api_keys():
    env = launch_environment(
        "ingest",
        VALUES,
        {
            "TRACE_COMMONS_BIND": "127.0.0.1:3907",
            "TRACE_COMMONS_TENANT_TOKENS": "stale-admin-token",
            "OPENAI_API_KEY": "stale-key",
        },
    )
    assert env["DATABASE_URL"] == VALUES["DATABASE_URL"]
    assert (
        env["TRACE_COMMONS_SIGNED_TOKEN_SECRET"]
        == VALUES["TRACE_COMMONS_SIGNED_TOKEN_SECRET"]
    )
    assert env["TRACE_COMMONS_BIND"] == "127.0.0.1:3907"
    assert "TRACE_COMMONS_TENANT_TOKENS" not in env
    assert "TRACE_COMMONS_INVITE" not in env and "TRACE_COMMONS_DEVICE_CODE" not in env
    assert "OPENAI_API_KEY" not in env and "NEAR_INTENTS_PRIVATE_KEY" not in env


def test_inherited_key_never_substitutes_for_missing_shared_key():
    with pytest.raises(Rejected, match="shared file"):
        launch_environment(
            "contributor", {}, {"TRACE_NEAR_AI_PRIVACY_API_KEY": "stale-key"}
        )


def state(tmp_path):
    tmp_path.chmod(0o700)
    files = {}
    for name, value in (
        ("config.json", {"tenant_id": "retained", "pii_filter": None}),
        ("daemon-settings.json", {"near_ai": None, "poll_interval_secs": 60}),
        ("daemon-queue.jsonl", {"entry": "retained"}),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        files[path] = path.read_bytes()
    return files


def test_launch_keeps_existing_identity_and_queue_bytes(tmp_path):
    before = state(tmp_path)
    command = command_for("contributor", Path("/opt/bin/contributor"), tmp_path)
    assert command == [
        "/opt/bin/contributor",
        "--config-dir",
        str(tmp_path),
        "daemon",
        "run",
    ]
    assert {path: path.read_bytes() for path in before} == before
    assert set(tmp_path.iterdir()) == set(before)


@pytest.mark.parametrize(
    "filename,field,value",
    [
        ("config.json", "pii_filter", "near-ai"),
        ("daemon-settings.json", "near_ai", {"api_key": "old-persisted-key"}),
    ],
)
def test_old_second_key_source_refused_without_mutation(
    tmp_path, filename, field, value
):
    state(tmp_path)
    path = tmp_path / filename
    original = json.loads(path.read_text())
    original[field] = value
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    with pytest.raises(Rejected, match="persisted"):
        check_contributor_settings(tmp_path)
    assert path.read_bytes() == before


def test_public_native_config_refused(tmp_path):
    state(tmp_path)
    (tmp_path / "config.json").chmod(0o644)
    with pytest.raises(Rejected, match="owner-private"):
        check_contributor_settings(tmp_path)


def test_native_launcher_reads_shared_pointer_without_copying_secrets(tmp_path):
    import subprocess
    import sys

    native_state = tmp_path / "native-state"
    native_state.mkdir()
    before = state(native_state)
    secrets = tmp_path / "outside-repository.env"
    secrets.write_text(
        'TRACE_NEAR_AI_PRIVACY_API_KEY="fixture-shared"\nOPENAI_API_KEY="fixture-other"\n'
    )
    secrets.chmod(0o600)
    pointer = tmp_path / "public.toml"
    pointer.write_text("[credentials]\nfile = " + json.dumps(str(secrets)) + "\n")
    executable = tmp_path / "native-fixture"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json, os\n"
        'print(json.dumps({"key_selected": os.environ.get("TRACE_NEAR_AI_PRIVACY_API_KEY") == "fixture-shared", '
        '"backend": os.environ.get("TRACE_PRIVACY_FILTER_BACKEND"), '
        '"unrelated_key_present": "OPENAI_API_KEY" in os.environ}))\n'
    )
    executable.chmod(0o700)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "orbo_runtime.commons_credentials",
            "--config",
            str(pointer),
            "contributor",
            "--binary",
            str(executable),
            "--config-dir",
            str(native_state),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "key_selected": True,
        "backend": "near-ai",
        "unrelated_key_present": False,
    }
    assert {path: path.read_bytes() for path in before} == before
    assert set(native_state.iterdir()) == set(before)


def test_explicit_invite_login_selects_shared_invite_only(tmp_path):
    values = {
        **VALUES,
        "TRACE_COMMONS_INVITE": "https://issuer.example/onboard#fixture",
        "TRACE_COMMONS_DEVICE_CODE": "unused-code",
    }
    env = launch_environment(
        "login",
        values,
        {
            "PATH": "/bin",
            "TRACE_COMMONS_DEVICE_CODE": "stale-code",
            "TRACE_COMMONS_INVITE": "stale-invite",
            "TRACE_COMMONS_ALLOWED_HOSTS": "wrong.example",
        },
    )
    assert env == {
        "PATH": "/bin",
        "TRACE_COMMONS_INVITE": values["TRACE_COMMONS_INVITE"],
    }
    config_dir = tmp_path / "new-native-state"
    command = command_for("login", "/native", config_dir, issuer_host="issuer.example")
    assert command == [
        "/native",
        "--config-dir",
        str(config_dir),
        "login",
        "--allowed-hosts",
        "issuer.example",
        "--scopes",
        "debugging_evaluation",
    ]
    assert not config_dir.exists()  # Native CLI creates its own private state.


def test_explicit_device_code_login_keeps_code_out_of_argv(tmp_path):
    env = launch_environment(
        "login",
        {
            "TRACE_COMMONS_DEVICE_CODE": "fixture-code",
            "TRACE_COMMONS_INVITE": "unused-invite",
        },
        {},
        device_code=True,
    )
    assert env == {"TRACE_COMMONS_DEVICE_CODE": "fixture-code"}
    command = command_for("login", "/native", tmp_path / "new", device_code=True)
    assert command == [
        "/native",
        "--config-dir",
        str(tmp_path / "new"),
        "login",
        "--scopes",
        "debugging_evaluation",
    ]
    assert "fixture-code" not in command


@pytest.mark.parametrize("device_code", [False, True])
def test_login_cannot_fall_back_to_default_browser_route(device_code):
    with pytest.raises(Rejected, match="selected enrollment"):
        launch_environment(
            "login", {}, {"TRACE_COMMONS_INVITE": "inherited"}, device_code=device_code
        )


def test_login_preserves_existing_enrollment_and_refuses_invalid_routes(tmp_path):
    original = state(tmp_path)
    with pytest.raises(Rejected, match="already enrolled"):
        command_for("login", "/native", tmp_path, issuer_host="issuer.example")
    assert {path: path.read_bytes() for path in original} == original
    with pytest.raises(Rejected):
        command_for(
            "login", "/native", tmp_path, device_code=True, issuer_host="issuer.example"
        )
    with pytest.raises(Rejected):
        command_for("login", "/native", tmp_path)
    with pytest.raises(Rejected):
        command_for("contributor", "/native", tmp_path, issuer_host="issuer.example")

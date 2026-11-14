"""Launch native Commons services from Orbo's single shared credential source.

The contributor's protocol redactor supports environment-selected privacy
filtering independently of its persisted `pii_filter` selector. Keeping that
selector unset avoids copying the shared API credential into native settings.
Enrollment, device identity, consent and queue state remain in their original
owner-private directory; this launcher never rewrites them.
"""

import argparse
import json
import os
from pathlib import Path
import re

from .contracts import Rejected

PRIVACY_KEY = "TRACE_NEAR_AI_PRIVACY_API_KEY"
PRIVACY_PREFIX = "TRACE_NEAR_AI_PRIVACY_"
COMMONS_PREFIX = "TRACE_COMMONS_"
DATABASE_KEYS = {"DATABASE_URL", "TRACE_COMMONS_LOGIN_RESOLVER_DATABASE_URL"}
ENROLLMENT_KEYS = {"TRACE_COMMONS_INVITE", "TRACE_COMMONS_DEVICE_CODE"}
# Native operational configuration may stay in EnvironmentFile; secrets must
# come from read_values, never an unrelated inherited shell environment.
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY", "MASTER_KEY")


def inherited_secret(name):
    return (
        name in DATABASE_KEYS | ENROLLMENT_KEYS
        or name.endswith("_DATABASE_URL")
        or any(marker in name for marker in SECRET_MARKERS)
    )


def launch_environment(role, values, inherited, *, device_code=False):
    if role not in {"contributor", "ingest", "login"}:
        raise Rejected("unknown Commons service")
    # Drop all inherited credentials, including unrelated wallet/OpenAI keys.
    operational = {
        "PATH",
        "TMPDIR",
        "RUST_LOG",
        "RUST_BACKTRACE",
        "LANG",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    env = {
        name: value
        for name, value in inherited.items()
        if (
            name in operational
            or name.startswith(
                ("LC_", COMMONS_PREFIX, PRIVACY_PREFIX, "TRACE_PRIVACY_FILTER_")
            )
        )
        and not inherited_secret(name)
    }
    if role == "login":
        # Native code selection is environment-based to keep the one-time code
        # out of argv. Passing --allowed-hosts would override that route.
        selected_name = (
            "TRACE_COMMONS_DEVICE_CODE" if device_code else "TRACE_COMMONS_INVITE"
        )
        if not values.get(selected_name):
            raise Rejected("selected enrollment credential missing from shared file")
        env = {
            name: value
            for name, value in env.items()
            if name in operational or name.startswith("LC_")
        }
        env[selected_name] = values[selected_name]
        return env
    if device_code:
        raise Rejected("device-code selection is only valid for explicit login")
    if role == "contributor":
        # Do not pass ingest administrator/issuer credentials into the robot daemon.
        env = {
            name: value
            for name, value in env.items()
            if not name.startswith(COMMONS_PREFIX) and name not in DATABASE_KEYS
        }
        selected = {
            name: value
            for name, value in values.items()
            if name.startswith(PRIVACY_PREFIX)
        }
    else:
        selected = {
            name: value
            for name, value in values.items()
            if (
                name.startswith((COMMONS_PREFIX, PRIVACY_PREFIX))
                or name in DATABASE_KEYS
            )
            and name not in ENROLLMENT_KEYS
        }
    if not selected.get(PRIVACY_KEY):
        raise Rejected("Commons privacy classifier credential missing from shared file")
    env.update({name: value for name, value in selected.items() if value})
    env["TRACE_PRIVACY_FILTER_BACKEND"] = "near-ai"
    env.setdefault("TRACE_NEAR_AI_PRIVACY_BASE_URL", "https://cloud-api.near.ai/v1")
    env.setdefault("TRACE_NEAR_AI_PRIVACY_MODEL", "openai/privacy-filter")
    return env


def check_contributor_settings(directory, *, allow_missing=False):
    """Refuse a stale second privacy-key source without mutating native identity."""
    directory = Path(directory)
    if allow_missing and not directory.exists() and not directory.is_symlink():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise Rejected("existing owner-private contributor directory required")
    mode = directory.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise Rejected("owner-private contributor directory required")
    for filename, field in (
        ("config.json", "pii_filter"),
        ("daemon-settings.json", "near_ai"),
    ):
        path = directory / filename
        if not path.exists():
            continue
        if (
            path.is_symlink()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077
        ):
            raise Rejected("owner-private contributor configuration required")
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get(field) is not None:
            raise Rejected(
                "remove persisted privacy selector/key before using shared credentials"
            )


def command_for(role, binary, config_dir, *, issuer_host=None, device_code=False):
    if role == "login":
        if config_dir is None:
            raise Rejected("contributor config directory required")
        if device_code and issuer_host is not None:
            raise Rejected("device-code and issuer-host enrollment are exclusive")
        if not device_code and (
            not isinstance(issuer_host, str)
            or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", issuer_host
            )
        ):
            raise Rejected("explicit issuer hostname required for invite enrollment")
        check_contributor_settings(config_dir, allow_missing=True)
        if (Path(config_dir) / "config.json").exists():
            raise Rejected("contributor already enrolled; existing identity preserved")
        command = [str(binary), "--config-dir", str(config_dir), "login"]
        if issuer_host:
            command += ["--allowed-hosts", issuer_host]
        return command + ["--scopes", "debugging_evaluation"]
    if issuer_host is not None or device_code:
        raise Rejected("enrollment options require explicit login")
    if role == "contributor":
        if config_dir is None:
            raise Rejected("contributor config directory required")
        check_contributor_settings(config_dir)
        return [str(binary), "--config-dir", str(config_dir), "daemon", "run"]
    if config_dir is not None:
        raise Rejected("ingest does not use a contributor directory")
    return [str(binary)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Shared credential pointer TOML")
    parser.add_argument("role", choices=("contributor", "ingest", "login"))
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path)
    enrollment = parser.add_mutually_exclusive_group()
    enrollment.add_argument(
        "--issuer-host", help="Allowed issuer hostname for invite enrollment"
    )
    enrollment.add_argument(
        "--device-code",
        action="store_true",
        help="Select the shared one-time device code",
    )
    args = parser.parse_args()
    try:
        from .credentials import read_values

        environment = launch_environment(
            args.role,
            read_values(args.config),
            os.environ,
            device_code=args.device_code,
        )
        command = command_for(
            args.role,
            args.binary,
            args.config_dir,
            issuer_host=args.issuer_host,
            device_code=args.device_code,
        )
        os.execve(str(args.binary), command, environment)
    except (OSError, ValueError, Rejected):
        # Do not render native paths, parser input or credential values in logs.
        parser.exit(
            1,
            "Commons launch refused: check shared credentials and native configuration\n",
        )


if __name__ == "__main__":
    main()

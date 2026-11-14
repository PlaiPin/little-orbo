"""One Git-ignored project .env, selected by a public repository TOML pointer.

The file is never sourced as shell code. Services retrieve only their requested
keys; native-service wrappers select the subset exported to a child process.
There is no ambient provider-key fallback that could hide an incomplete setup.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tomllib

from .contracts import Rejected

NAMES = (
    "NEARAI_API_KEY",
    "OPENAI_API_KEY",
    "COMMONS_EXPORT_TOKEN",
    "COMMONS_CONTRIBUTOR_TOKEN",
    "NEARAI_REPORTING_TOKEN",
    "NEAR_INTENTS_API_KEY",
    "NEAR_SIGNER_ACCOUNT_ID",
    "NEAR_SIGNER_PUBLIC_KEY",
    "NEAR_SIGNER_PRIVATE_KEY",
    "TRACE_COMMONS_INVITE",
    "TRACE_COMMONS_DEVICE_CODE",
    "TRACE_NEAR_AI_PRIVACY_API_KEY",
    "DATABASE_URL",
    "TRACE_COMMONS_LOGIN_RESOLVER_DATABASE_URL",
    "ORBO_REMOTE_OWNER_PHONE_TOKEN",
    "FISH_API_KEY",
    "FISH_VOICE_ID",
)
NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
LIMIT = 65_536


def _roots(config_path=None):
    """Find source/deployment roots, including a checkout with a .git file."""
    paths = [Path(__file__).resolve()]
    if config_path is not None:
        paths.append(Path(config_path).resolve())
    roots = set()
    for path in paths:
        for parent in path.parents:
            if (
                (parent / "pyproject.toml").is_file()
                and (parent / "deploy").is_dir()
                or (parent / ".git").exists()
            ):
                roots.add(parent)
                break
    return roots


def config_file(path=None):
    selected = path or os.environ.get("ORBO_CREDENTIALS_CONFIG")
    if selected:
        return Path(selected).expanduser().absolute()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "deploy" / "credentials.toml"
        if candidate.is_file():
            return candidate
    raise Rejected("set ORBO_CREDENTIALS_CONFIG to the public credentials TOML")


def credential_path(path, config_path=None):
    """Resolve a pointer independently of cwd, retaining no-follow file semantics."""
    value = str(path)
    marker = "${CREDENTIALS_DIRECTORY}"
    if marker in value:
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if not directory or not Path(directory).is_absolute():
            raise Rejected("systemd credential directory is unavailable")
        value = value.replace(marker, directory)
    if "$" in value:
        raise Rejected("unsupported credential path substitution")
    result = Path(value).expanduser()
    if not result.is_absolute():
        if config_path is None:
            raise Rejected("relative credential paths require a pointer configuration")
        result = Path(config_path).absolute().parent / result
    result = Path(os.path.abspath(result))
    return result


def external_path(path, config_path=None):
    """Optional standalone wallet JSON files remain outside tracked source trees."""
    result = credential_path(path, config_path)
    resolved = result.resolve()
    if any(resolved.is_relative_to(root) for root in _roots(config_path)):
        raise Rejected("credential file must be outside the repository/deployment tree")
    return result


def credential_file(path=None):
    config = config_file(path)
    try:
        if config.stat().st_size > 8192:
            raise ValueError()
        document = tomllib.loads(config.read_text())
        if (
            set(document) != {"credentials"}
            or set(document["credentials"]) != {"file"}
            or not isinstance(document["credentials"]["file"], str)
            or not document["credentials"]["file"]
        ):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise Rejected(
            "invalid or unreadable credentials pointer configuration"
        ) from None
    return credential_path(document["credentials"]["file"], config)


def parse_values(text):
    """Parse literal KEY=value lines, with optional matching outer quotes only."""
    result = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not NAME.fullmatch(name) or name in result:
            raise Rejected(
                f"invalid or duplicate credential assignment on line {number}"
            )
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise Rejected(f"invalid credential quoting on line {number}")
            value = value[1:-1]
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise Rejected(f"invalid credential value on line {number}")
        result[name] = value
    return result


def read_values(config_path=None):
    path = credential_file(config_path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise Rejected(
            "credential file is not readable as a regular private file"
        ) from None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_size > LIMIT
        ):
            raise Rejected(
                "credential file must be owner-only, owned by this user, and bounded"
            )
        data = source.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise Rejected("credential file exceeds limit")
    try:
        return parse_values(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise Rejected("credential file must be UTF-8 text") from None


def credential(name, *, required=True):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Rejected("invalid credential name")
    value = read_values().get(name)
    if not value and required:
        raise Rejected(f"missing credential: {name}")
    return value or None


def initialize(config_path=None):
    """Copy .env.example to the private .env; never replace existing values."""
    path = credential_file(config_path)
    sample = path.with_name(".env.example")
    if sample.is_file():
        if sample.stat().st_size > LIMIT:
            raise Rejected("credential example exceeds limit")
        template = sample.read_text()
        values = parse_values(template)
        if set(values) != set(NAMES) or any(values.values()):
            raise Rejected(
                "credential example must contain only the empty template fields"
            )
    else:
        template = (
            "# Private Orbo credentials. Literal values only; never source this file.\n"
        )
        template += "".join(name + '=""\n' for name in NAMES)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return {"file": str(path), "created": False}
    with os.fdopen(fd, "w") as target:
        target.write(template)
    return {"file": str(path), "created": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "status"])
    parser.add_argument(
        "--config", help="public TOML pointer; defaults to deploy/credentials.toml"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            report = initialize(args.config)
        else:
            values = read_values(args.config)
            report = {
                "file": str(credential_file(args.config)),
                "configured": {name: bool(values.get(name)) for name in NAMES},
            }
        print(json.dumps(report, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"error_class": type(error).__name__, "configured": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

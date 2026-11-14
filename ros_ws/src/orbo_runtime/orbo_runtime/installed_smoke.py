#!/usr/bin/env python3
"""Installed owner IPC smoke. Run as the service user in an isolated ROS domain."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from orbo_runtime.cli import request


async def check():
    with tempfile.TemporaryDirectory(prefix="orbo-installed-") as directory:
        root = Path(directory)
        config = root / "runtime.toml"
        endpoint = root / "runtime.sock"
        config.write_text(
            '[runtime]\nowner="smoke"\nprofile="orbo"\ndatabase='
            + json.dumps(str(root / "journal.db"))
            + "\nsocket="
            + json.dumps(str(endpoint))
            + '\n[robot]\nid="orbo"\nflippers_qualified=false\nattention_pulse_us=[]\n'
        )
        config.chmod(0o600)
        env = {
            **os.environ,
            "ROS_DOMAIN_ID": "94",
            "ROS_LOG_DIR": str(root / "ros-log"),
        }
        with (root / "runtime.log").open("w") as log:
            process = subprocess.Popen(
                ["ros2", "run", "orbo_runtime", "runtime", "serve", "--config", str(config)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                async with asyncio.timeout(10):
                    while not endpoint.exists():
                        if process.poll() is not None:
                            raise RuntimeError((root / "runtime.log").read_text())
                        await asyncio.sleep(0.05)
                assert endpoint.stat().st_mode & 0o777 == 0o600
                start = await request(endpoint, "episode.start", {})
                assert start["ok"], start
                episode = start["result"]["episode_id"]
                stopped = await request(endpoint, "stop", {})
                assert (
                    stopped["ok"]
                    and stopped["result"]["physical_cessation"] == "unverified"
                )
                ended = await request(
                    endpoint,
                    "episode.end",
                    {"episode_id": episode, "outcome": "unknown"},
                )
                assert ended["ok"], ended
                journal = await request(endpoint, "journal", {})
                assert journal["ok"] and any(
                    e["kind"] == "episode.ended" for e in journal["result"]
                )
                print(
                    "Installed real ROS adapter/runtime started without hardware, owner IPC mode verified, episode journal and stop request worked. No arm or provider call performed."
                )
            finally:
                # Signal the ROS CLI and its executable, as terminal Ctrl-C does.
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                finally:
                    # Also reap descendants if the CLI exited before its child.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.to_thread(process.wait)
            assert process.returncode == 0, (root / "runtime.log").read_text()
            assert not endpoint.exists()
            print("Graceful shutdown removed owner socket; exit status 0.")


def main():
    asyncio.run(check())


if __name__ == "__main__":
    main()

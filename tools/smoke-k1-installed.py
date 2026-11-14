#!/usr/bin/env python3
"""Installed K1 disconnected-profile smoke; no robot connection or motion."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from orbo_runtime.cli import request


async def main():
    with tempfile.TemporaryDirectory(prefix="orbo-installed-") as directory:
        root = Path(directory)
        config = root / "runtime.toml"
        endpoint = root / "runtime.sock"
        config.write_text(
            '[runtime]\nowner="smoke"\nprofile="booster_k1"\ndatabase='
            + json.dumps(str(root / "journal.db"))
            + "\nsocket="
            + json.dumps(str(endpoint))
            + '\n[robot]\nid="orbo"\nsocket="/tmp/orbo-no-worker.sock"\nqualified=false\nallowed_actions=["hand_wave"]\n'
        )
        config.write_text(
            config.read_text().replace(
                "/tmp/orbo-no-worker.sock", str(root / "absent-worker.sock")
            )
        )
        config.chmod(0o600)
        env = {
            **os.environ,
            "ROS_DOMAIN_ID": "94",
            "ROS_LOG_DIR": str(root / "ros-log"),
        }
        with (root / "runtime.log").open("w") as log:
            process = subprocess.Popen(
                ["/opt/orbo/.venv/bin/orbo", "serve", "--config", str(config)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
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
                assert not stopped["ok"], (
                    "A disconnected K1 stop must remain unconfirmed"
                )
                denied = await request(
                    endpoint,
                    "robot.action",
                    {
                        "episode_id": episode,
                        "key": "uncommissioned",
                        "action_id": "hand_wave",
                        "hold_seconds": 0.1,
                    },
                )
                assert not denied["ok"]
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
                    "Installed K1 runtime started with an absent uncommissioned bridge. Owner IPC and episode journal work; motion and unreachable stop are refused. No SDK motion or provider call performed."
                )
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
                    raise
            assert process.returncode == 0, (root / "runtime.log").read_text()
            assert not endpoint.exists()
            print("Graceful shutdown removed owner socket; exit status 0.")


if __name__ == "__main__":
    asyncio.run(main())

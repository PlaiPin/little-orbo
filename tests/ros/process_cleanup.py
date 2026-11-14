"""Terminate a test-owned process group, including orphaned ROS children."""

import os
import signal
import subprocess


def stop_process_group(process, *, timeout=8):
    """The caller must have launched process with start_new_session=True."""
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # ros2 run/launch can exit while a controller child is still alive.
        # Waiting for only the wrapper left CPU-consuming controllers behind.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

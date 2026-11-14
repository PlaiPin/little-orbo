"""No ROS needed: a wrapper exiting must not leak its unresponsive child."""

import select
import subprocess
import sys

from process_cleanup import stop_process_group


def test_child_is_stopped_even_when_wrapper_has_already_exited():
    child = (
        "import signal,time; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(60)"
    )
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert select.select([wrapper.stdout], [], [], 5)[0]
        assert wrapper.stdout.readline() == b"ready\n"
        assert wrapper.wait(timeout=5) == 0
        stop_process_group(wrapper)
        assert select.select([wrapper.stdout], [], [], 5)[0], "orphan retained stdout"
        assert wrapper.stdout.read() == b""
    finally:
        stop_process_group(wrapper)
        wrapper.stdout.close()

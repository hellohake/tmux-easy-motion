#!/usr/bin/env python3
"""Integration regression test for hint-overlay viewport stability."""

import contextlib
import fcntl
import io
import os
import pty
import select
import shlex
import struct
import subprocess
import tempfile
import termios
import time
import runpy


PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASY_MOTION = runpy.run_path(os.path.join(PLUGIN_DIR, "scripts", "easy_motion.py"))


def render_full_screen():
    lines = ["中" * 76] + ["line-{:02d}".format(index) for index in range(1, 38)]
    capture_buffer = "\n".join(lines)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        EASY_MOTION["print_text_with_targets"](
            capture_buffer,
            [76],
            "",
            "",
            "",
            "",
            "a",
            152,
        )
    return lines, output.getvalue()


def main():
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    env["LC_ALL"] = "C.UTF-8"
    expected_lines, rendered_screen = render_full_screen()

    with tempfile.TemporaryDirectory(prefix="easy-motion-hint-viewport-") as tmp_dir:
        socket = os.path.join(tmp_dir, "socket")
        payload_path = os.path.join(tmp_dir, "screen.out")
        with open(payload_path, "w", encoding="utf-8") as payload_file:
            payload_file.write(rendered_screen)

        master = None
        client = None

        def tmux(*args):
            return subprocess.check_output(
                ["tmux", "-S", socket, *args],
                env=env,
                text=True,
                stderr=subprocess.STDOUT,
            ).rstrip("\n")

        try:
            fixture = (
                "import sys,time; "
                "time.sleep(0.3); "
                f"sys.stdout.write(open({payload_path!r}, encoding='utf-8').read()); "
                "sys.stdout.flush(); "
                "time.sleep(30)"
            )
            pane = tmux(
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-x",
                "152",
                "-y",
                "39",
                "-s",
                "test",
                "-P",
                "-F",
                "#{pane_id}",
                "python3 -u -c " + shlex.quote(fixture),
            )

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 39, 152, 0, 0))
            client = subprocess.Popen(
                ["tmux", "-S", socket, "attach-session", "-t", "test"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            os.close(slave)

            deadline = time.monotonic() + 5
            captured = ""
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.03)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        pass
                captured = tmux("capture-pane", "-p", "-t", pane, "-S", "0", "-E", "37")
                if "line-37" in captured:
                    break
            else:
                raise AssertionError("hint overlay did not finish rendering")

            history_size = int(tmux("display-message", "-p", "-t", pane, "#{history_size}"))
            actual_lines = captured.splitlines()
            assert history_size == 0, "hint overlay scrolled {} rows".format(history_size)
            assert actual_lines[0] == expected_lines[0], "top row moved or changed"
            assert actual_lines[-1] == expected_lines[-1], "bottom row moved or changed"
            print("PASS: full-screen hint overlay preserves the tmux viewport")
        finally:
            subprocess.run(
                ["tmux", "-S", socket, "kill-server"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if client is not None:
                try:
                    client.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    client.terminate()
                    client.wait(timeout=3)
            if master is not None:
                os.close(master)


if __name__ == "__main__":
    main()

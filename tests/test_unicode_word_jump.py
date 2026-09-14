#!/usr/bin/env python3
"""Regression test for word jumps after wide Unicode characters."""

import fcntl
import importlib.util
import os
import pty
import select
import shlex
import struct
import subprocess
import tempfile
import termios
import time
import unicodedata


PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASY_MOTION_PATH = os.path.join(PLUGIN_DIR, "scripts", "easy_motion.py")
TARGET_KEYS = "asdfghjklqwertyuiopzxcvbnm"

SPEC = importlib.util.spec_from_file_location("easy_motion", EASY_MOTION_PATH)
EASY_MOTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EASY_MOTION)


def find_target_label(group, target_position, prefix=""):
    if isinstance(group, int):
        return prefix if group == target_position else None
    for key, child in zip(TARGET_KEYS, group):
        label = find_target_label(child, target_position, prefix + key)
        if label is not None:
            return label
    return None


def display_width(text):
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def run_case(sample, target_text, pane_width=80):
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    env["LC_ALL"] = "C.UTF-8"

    with tempfile.TemporaryDirectory(prefix="easy-motion-unicode-word-") as tmp_dir:
        socket = os.path.join(tmp_dir, "socket")
        master = None
        client = None

        def tmux(*args):
            return subprocess.check_output(
                ["tmux", "-S", socket, *args],
                env=env,
                text=True,
                stderr=subprocess.STDOUT,
            ).rstrip("\n")

        def drain_client():
            if select.select([master], [], [], 0.03)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    pass

        try:
            fixture = (
                "import time; "
                f"print({sample!r}, flush=True); "
                "print('FOOTER', flush=True); "
                "time.sleep(30)"
            )
            pane = tmux(
                "-f", "/dev/null", "new-session", "-d", "-x", str(pane_width), "-y", "12",
                "-s", "test", "-P", "-F", "#{pane_id}",
                "python3 -u -c " + shlex.quote(fixture),
            )
            tmux("set-option", "-g", "prefix", "C-f")
            tmux("set-window-option", "-g", "mode-keys", "vi")
            tmux("set-option", "-g", "@easy-motion-target-keys", TARGET_KEYS)
            tmux("run-shell", os.path.join(PLUGIN_DIR, "easy_motion.tmux"))

            command = (
                os.path.join(PLUGIN_DIR, "scripts", "easy_motion.sh")
                + " '#{pid}' '#{session_id}' '#{window_id}' '#{pane_id}' bd-w"
            )
            binding = (
                "bind-key w { copy-mode; "
                f'run-shell -b "{command}" }}\n'
            )
            subprocess.run(
                ["tmux", "-S", socket, "source-file", "-"],
                input=binding, env=env, text=True, check=True,
            )

            deadline = time.monotonic() + 5
            while "FOOTER" not in tmux("capture-pane", "-p", "-t", pane):
                if time.monotonic() > deadline:
                    raise AssertionError("fixture did not finish")

            master, slave = pty.openpty()
            fcntl.ioctl(
                slave,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 12, pane_width, 0, 0),
            )
            client = subprocess.Popen(
                ["tmux", "-S", socket, "attach-session", "-t", "test"],
                stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
            )
            os.close(slave)

            deadline = time.monotonic() + 5
            while not tmux("list-clients"):
                if time.monotonic() > deadline:
                    raise AssertionError("tmux client did not attach")
                time.sleep(0.03)

            capture = tmux("capture-pane", "-p", "-t", pane) + "\n"
            lines = capture.splitlines()
            target_row = next(
                row for row, line in enumerate(lines) if target_text in line
            )
            target_line = lines[target_row]
            target_character_col = target_line.index(target_text)
            target_position = (
                sum(len(line) + 1 for line in lines[:target_row])
                + target_character_col
            )
            cursor_row, cursor_col = map(
                int,
                tmux(
                    "display-message", "-p", "-t", pane,
                    "#{cursor_y}:#{cursor_x}",
                ).split(":"),
            )
            cursor_position = EASY_MOTION.convert_row_col_to_text_pos(
                cursor_row, cursor_col, capture
            )
            grouped_indices = EASY_MOTION.group_indices(
                EASY_MOTION.motion_to_indices(cursor_position, capture, "bd-w", None),
                len(TARGET_KEYS),
            )
            label = find_target_label(grouped_indices, target_position)
            assert label, f"target {target_text!r} did not receive a hint"

            os.write(master, bytes([6]) + b"w")
            deadline = time.monotonic() + 5
            while tmux("display-message", "-p", "#{pane_id}") == pane:
                if time.monotonic() > deadline:
                    raise AssertionError("hint overlay did not open")
                drain_client()

            for key in label:
                os.write(master, key.encode())
                time.sleep(0.05)

            deadline = time.monotonic() + 5
            while (
                tmux("display-message", "-p", "#{pane_id}") != pane
                or len(tmux("list-windows").splitlines()) != 1
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("original pane was not restored")
                drain_client()

            actual_position = tmux(
                "display-message", "-p", "-t", pane,
                "#{copy_cursor_y}:#{copy_cursor_x}",
            )
            expected_position = "{}:{}".format(
                target_row, display_width(target_line[:target_character_col])
            )
            assert actual_position == expected_position, (
                f"wrong cursor for {target_text!r} in {sample!r}: "
                f"expected={expected_position}, actual={actual_position}"
            )
        finally:
            subprocess.run(
                ["tmux", "-S", socket, "kill-server"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if client is not None:
                try:
                    client.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    client.terminate()
                    client.wait(timeout=3)
            if master is not None:
                os.close(master)


def main():
    run_case("alpha target next", "target")
    run_case("中文，单词。测试", "单")
    run_case("中文 target next", "target")
    run_case("prefix-1234567890 中文 target next tail", "target", pane_width=20)
    print("PASS: word jumps land correctly after wide Unicode characters")


if __name__ == "__main__":
    main()

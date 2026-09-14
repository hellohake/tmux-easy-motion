#!/usr/bin/env python3
"""Regression test for preserving a scrolled copy-mode viewport."""

import fcntl
import os
import pty
import runpy
import select
import shlex
import struct
import subprocess
import tempfile
import termios
import time


PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASY_MOTION = runpy.run_path(os.path.join(PLUGIN_DIR, "scripts", "easy_motion.py"))
TARGET_KEYS = "asdfghjklqwertyuiopzxcvbnm"


def find_target_label(group, target_position, prefix=""):
    if isinstance(group, int):
        return prefix if group == target_position else None
    for key, child in zip(TARGET_KEYS, group):
        label = find_target_label(child, target_position, prefix + key)
        if label is not None:
            return label
    return None


def main():
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    env["LC_ALL"] = "C.UTF-8"

    with tempfile.TemporaryDirectory(prefix="easy-motion-scrollback-") as tmp_dir:
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

        try:
            fixture = (
                "import sys; "
                "[(print(f'BLOCK-{i:03d} +> 状态：这是一段包含中文的长文本，用于复现 "
                "agent 输出中的软换行；commit={i:03d} abcdefghijklmnopqrstuvwxyz "
                "继续补充中文内容直到它跨越终端宽度，验证视口与目标路径是否一致。', flush=True), "
                "print(f'NEXT-{i:03d} short line', flush=True)) for i in range(180)]; "
                "sys.stdin.read()"
            )
            pane = tmux(
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-x",
                "152",
                "-y",
                "38",
                "-s",
                "test",
                "-P",
                "-F",
                "#{pane_id}",
                "python3 -u -c " + shlex.quote(fixture),
            )
            tmux("set-option", "-g", "prefix", "C-f")
            tmux("set-window-option", "-g", "mode-keys", "vi")
            tmux("set-option", "-g", "pane-border-status", "top")
            for option, value in {
                "@easy-motion-copy-mode-prefix": "s",
                "@easy-motion-default-motion": "bd-j",
                "@easy-motion-target-keys": TARGET_KEYS,
            }.items():
                tmux("set-option", "-g", option, value)
            tmux("run-shell", os.path.join(PLUGIN_DIR, "easy_motion.tmux"))

            command = (
                os.path.join(PLUGIN_DIR, "scripts", "easy_motion.sh")
                + " '#{pid}' '#{session_id}' '#{window_id}' '#{pane_id}' bd-j"
            )
            binding = (
                "bind-key -T copy-mode-vi s { send-keys -X start-of-line; "
                f'run-shell -b "{command}" }}\n'
            )
            subprocess.run(
                ["tmux", "-S", socket, "source-file", "-"],
                input=binding,
                env=env,
                text=True,
                check=True,
            )

            deadline = time.monotonic() + 5
            while "BLOCK-179" not in tmux("capture-pane", "-p", "-t", pane):
                if time.monotonic() > deadline:
                    raise AssertionError("fixture did not finish")

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 38, 152, 0, 0))
            client = subprocess.Popen(
                ["tmux", "-S", socket, "attach-session", "-t", "test"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            os.close(slave)

            def drain(delay=0.03):
                if select.select([master], [], [], delay)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        pass

            def wait(predicate, label, timeout=8):
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    drain()
                    if predicate():
                        return
                raise AssertionError(f"timed out waiting for {label}")

            def active_pane():
                return tmux("display-message", "-p", "#{pane_id}")

            def viewport():
                scroll, row, col, height = map(
                    int,
                    tmux(
                        "display-message",
                        "-p",
                        "-t",
                        pane,
                        "#{scroll_position}:#{copy_cursor_y}:#{copy_cursor_x}:#{pane_height}",
                    ).split(":"),
                )
                captured = tmux(
                    "capture-pane",
                    "-p",
                    "-t",
                    pane,
                    "-S",
                    str(-scroll),
                    "-E",
                    str(height - scroll - 1),
                ) + "\n"
                return scroll, row, col, height, captured

            wait(lambda: bool(tmux("list-clients")), "attached client")
            os.write(master, b"\x06[")
            wait(
                lambda: tmux("display-message", "-p", "-t", pane, "#{pane_mode}")
                == "copy-mode",
                "copy mode",
            )
            os.write(master, b"\x15")
            drain(0.2)
            os.write(master, b"\x15")
            drain(0.2)

            scroll_before, row_before, _, height, capture = viewport()
            assert scroll_before > 0, "test requires a scrolled viewport"
            lines = capture.splitlines()
            target_row = height - 8
            target_col = len(lines[target_row]) - len(lines[target_row].lstrip())
            target_text = lines[target_row]
            target_position = sum(len(line) + 1 for line in lines[:target_row]) + target_col
            cursor_position = EASY_MOTION["convert_row_col_to_text_pos"](
                row_before, 0, capture
            )
            groups = EASY_MOTION["group_indices"](
                EASY_MOTION["motion_to_indices"](
                    cursor_position, capture, "bd-j", None
                ),
                len(TARGET_KEYS),
            )
            label = find_target_label(groups, target_position)
            assert label, "target line did not receive a hint"

            os.write(master, b"s")
            wait(lambda: active_pane() != pane, "hint overlay")
            for key in label:
                os.write(master, key.encode())
                drain(0.1)
            wait(
                lambda: active_pane() == pane
                and len(tmux("list-windows").splitlines()) == 1,
                "original pane restoration",
            )

            scroll_after, row_after, col_after, _, capture_after = viewport()
            lines_after = capture_after.splitlines()
            actual_text = lines_after[row_after]
            assert scroll_after == scroll_before, (
                f"scrollback moved: before={scroll_before}, after={scroll_after}"
            )
            assert actual_text == target_text, (
                f"wrong target: expected={target_text!r}, actual={actual_text!r}"
            )
            assert (row_after, col_after) == (target_row, target_col), (
                "wrong viewport coordinate: "
                f"expected={(target_row, target_col)}, actual={(row_after, col_after)}"
            )
            print("PASS: scrolled viewport and selected line are preserved")
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

#!/usr/bin/env python3
"""Regression tests for preserving screen geometry in the hint overlay."""

import contextlib
import io
import os
import re
import runpy


PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASY_MOTION = runpy.run_path(os.path.join(PLUGIN_DIR, "scripts", "easy_motion.py"))
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")


def render(capture_buffer, grouped_indices, target_keys="ab", terminal_width=20):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        EASY_MOTION["print_text_with_targets"](
            capture_buffer,
            grouped_indices,
            "",
            "",
            "",
            "",
            target_keys,
            terminal_width,
        )
    return ANSI_ESCAPE.sub("", output.getvalue())


def test_wide_target_keeps_following_text_in_the_same_columns():
    actual = render("前中文后", [1])
    assert actual == "前a 文后", repr(actual)


def test_full_width_line_does_not_accept_a_newline_preview():
    source = "中" * 76 + "\nnext"
    actual = render(source, [76], target_keys="a", terminal_width=152)
    assert actual == source, "a line-end hint overflowed the 152-cell row"


def test_line_end_preview_uses_a_real_free_display_cell():
    source = "中" * 75 + " \nnext"
    actual = render(source, [76], target_keys="a", terminal_width=152)
    assert actual == "中" * 75 + " a\nnext", repr(actual)


def test_duplicate_targets_at_a_newline_emit_only_one_newline():
    actual = render("x\nnext", [[0, 1]], terminal_width=10)
    assert actual == "aa\nbext", repr(actual)


def main():
    test_wide_target_keeps_following_text_in_the_same_columns()
    test_full_width_line_does_not_accept_a_newline_preview()
    test_line_end_preview_uses_a_real_free_display_cell()
    test_duplicate_targets_at_a_newline_emit_only_one_newline()
    print("PASS: hint rendering preserves terminal-cell geometry")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The type-checking gate (D-209, docs/dashboard_design.md §12): `mypy --strict` over
the strict module list (mypy.ini, `files`) reports no error, and every `# type: ignore`
in those modules names its error code and gives a reason on the same line.

The mypy run needs mypy in the interpreter running the test (the repo's .venv, pinned in
requirements-dev.txt); without it only that test is skipped (the pre-commit hook always
runs it with the .venv's mypy):
    .venv/bin/python -m unittest test_typing
"""
import configparser
import importlib.util
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "mypy.ini")
# The list only grows (§4): these are strict since phase 2a.
FLOOR = {"actions.py", "store.py", "mcp_server.py"}
IGNORE = re.compile(r"#\s*type:\s*ignore\b")
IGNORE_OK = re.compile(r"#\s*type:\s*ignore\[[a-z0-9_-]+(\s*,\s*[a-z0-9_-]+)*\]\s*#\s*\S")


def config():
    cp = configparser.ConfigParser()
    if not cp.read(CONFIG):
        raise FileNotFoundError(CONFIG)
    return cp["mypy"]


def strict_files():
    return [f.strip() for f in config()["files"].split(",") if f.strip()]


class StrictList(unittest.TestCase):
    def test_config_is_strict(self):
        self.assertTrue(config().getboolean("strict"))

    def test_list_keeps_the_phase_2a_modules(self):
        self.assertLessEqual(FLOOR, set(strict_files()))

    def test_listed_files_exist(self):
        for f in strict_files():
            self.assertTrue(os.path.isfile(os.path.join(HERE, f)), f)

    def test_every_ignore_has_a_code_and_a_reason(self):
        bad = []
        for f in strict_files():
            with open(os.path.join(HERE, f)) as fh:
                for n, line in enumerate(fh, 1):
                    if IGNORE.search(line) and not IGNORE_OK.search(line):
                        bad.append(f"{f}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "write `# type: ignore[code]  # why` (the code and a reason, same line)")


@unittest.skipIf(importlib.util.find_spec("mypy") is None,
                 f"mypy is not installed for {sys.executable}: run this test with .venv/bin/python "
                 "(.venv/bin/pip install -r requirements-dev.txt)")
class MypyStrict(unittest.TestCase):
    def test_strict_modules_pass(self):
        r = subprocess.run([sys.executable, "-m", "mypy", "--config-file", CONFIG], cwd=HERE,
                           capture_output=True, text=True, timeout=900)
        self.assertEqual(r.returncode, 0, f"mypy --strict failed:\n{r.stdout}{r.stderr}")


if __name__ == "__main__":
    unittest.main()

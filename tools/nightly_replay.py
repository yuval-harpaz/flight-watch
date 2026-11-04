#!/usr/bin/env python3
"""The nightly review's squawk replay: this checkout's code, as it stands, against the stored squawks/cases.jsonl.

  <root>/.venv/bin/python tools/nightly_replay.py --root <root>     # exits 1 when a case changed, 2 when it could not run

One command, as the unattended review is refused `cp` with flags whatever its allowed list says (4 Nov 2026). The copy goes to
tmp_squawks here; squawks/ is not changed. tools/nightly_publish.py compares the code before and after on its own afterwards.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nightly_publish import replay  # noqa: E402

OUT = "tmp_squawks"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True, help="the main checkout, with squawks/, standing-data/ and .venv/")
    a = p.parse_args(argv)
    python = os.path.join(a.root, ".venv", "bin", "python")
    shutil.rmtree(OUT, ignore_errors=True)
    try:
        cases = replay(os.getcwd(), a.root, python, OUT)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"the replay did not run: {e}")
        print((getattr(e, "stderr", None) or b"")[-500:].decode(errors="replace"))
        return 2
    return 1 if subprocess.run([python, os.path.join(a.root, "tools", "compare_cases.py"),
                                os.path.join(a.root, "squawks", "cases.jsonl"), cases]).returncode else 0


if __name__ == "__main__":
    sys.exit(main())

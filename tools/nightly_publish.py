#!/usr/bin/env python3
"""The end of the nightly cycle (tools/nightly.sh): publish the night's commits, or tell the owner why not.

Pushed to origin/main by default (the owner, 3 Nov 2026). Not pushed when:
- the review wrote a HOLD line in reviews/<date>.owner: its fixes would lose an alert, or it suspects a new category of issue;
- the unit tests fail;
- the tests as they were before the night's commits fail on the new code: behaviour we had recorded has changed;
- the squawk replay's alert kinds differ between the code before and after: a real emergency may lose an alert;
- a commit touches a path the review may not change (this cycle's own scripts, workflows, anything not listed);
- main has commits of the owner's not on origin, or the merge or push fails.
These checks are this script's own, not the review's word. The owner's private chat (TELEGRAM_CHAT_ID, never the channel) gets a
message when something was held and for each line of the .owner file. No message when all went out cleanly: see reviews/<date>.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ALLOWED = ("flight_watch.py", "AGENTS.md", "turn_zones.json", "tests/", "docs/")
ALLOWED_TOOLS = ("tools/",)
FORBIDDEN = ("tools/nightly.sh", "tools/nightly_prompt.md", "tools/nightly_publish.py", "tools/nightly_replay.py",
             "tools/read_trace.py", "tools/cors_worker.js")
LOGIC = ("flight_watch.py", "turn_zones.json")
TEST_TIMEOUT, REPLAY_TIMEOUT = 900, 1800


def git(cwd: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def owner_lines(path: str) -> tuple[list[str], list[str]]:
    """The review's reviews/<date>.owner: (HOLD lines, NOTE lines). An unknown line counts as a hold."""
    holds, notes = [], []
    try:
        with open(path, encoding="utf-8") as f:
            for line in map(str.strip, f):
                if line.startswith("NOTE:"):
                    notes.append(line[5:].strip())
                elif line:
                    holds.append(line[5:].strip() if line.startswith("HOLD:") else line)
    except OSError:
        pass
    return holds, notes


def bad_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if p in FORBIDDEN
            or not (p in ALLOWED or p.startswith(("tests/", "docs/", *ALLOWED_TOOLS)))]


def run_tests(cwd: str, python: str) -> list[str]:
    """The failing tests' names ([] = all passed)."""
    try:
        r = subprocess.run([python, "-m", "unittest", "discover", "-s", "tests"], cwd=cwd, capture_output=True, text=True,
                           timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ["(the tests timed out)"]
    if r.returncode == 0:
        return []
    return [line.split(" ", 1)[1].split(" (")[0] for line in r.stderr.splitlines()
            if line.startswith(("FAIL: ", "ERROR: "))] or ["(the tests failed)"]


def old_tests_on_new_code(root: str, base: str, head: str, python: str) -> list[str]:
    """The tests of `base` run on the code of `head`."""
    with tempfile.TemporaryDirectory(prefix="nightly-old-tests-") as d:
        wt = os.path.join(d, "wt")
        git(root, "worktree", "add", "--detach", "-q", wt, head)
        try:
            git(wt, "rm", "-rqf", "--ignore-unmatch", "tests")
            git(wt, "checkout", base, "--", "tests")
            return run_tests(wt, python)
        finally:
            git(root, "worktree", "remove", "--force", wt, check=False)


def replay(code: str, root: str, python: str, out: str) -> str:
    """The squawk replay of the checkout `code`, into a copy of squawks/ at `out`; its cases.jsonl."""
    shutil.copytree(os.path.join(root, "squawks"), out)
    subprocess.run([python, "tools/squawk_cases.py", "--out", out, "--standing-data", os.path.join(root, "standing-data"),
                    "--redo", "--no-fetch"], cwd=code, capture_output=True, timeout=REPLAY_TIMEOUT, check=True)
    return os.path.join(out, "cases.jsonl")


def squawk_changes(root: str, base: str, head: str, python: str) -> list[str]:
    """Cases of the squawk replay whose alert kinds differ between `base` and `head`."""
    if not os.path.isdir(os.path.join(root, "squawks")):
        return ["(no squawks/ to replay)"]
    with tempfile.TemporaryDirectory(prefix="nightly-squawks-") as d:
        cases = {}
        for name, commit in (("base", base), ("head", head)):
            wt = os.path.join(d, name + "_wt")
            git(root, "worktree", "add", "--detach", "-q", wt, commit)
            try:
                cases[name] = replay(wt, root, python, os.path.join(d, name))
            finally:
                git(root, "worktree", "remove", "--force", wt, check=False)
        r = subprocess.run([python, os.path.join(root, "tools", "compare_cases.py"), cases["base"], cases["head"]],
                           capture_output=True, text=True)
        return [] if r.returncode == 0 else r.stdout.strip().splitlines()[:-1] or ["(the comparison failed)"]


def tell_owner(text: str) -> bool:
    """The owner's private chat with the bot: TELEGRAM_CHAT_ID only, never the channel. The token is never printed."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: the owner was not told")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30) as r:
            return json.load(r).get("ok", False)
    except Exception as e:  # (its text can name the URL, with the token)
        print("telegram failed:", type(e).__name__)
        return False


def checks(root: str, wt: str, base: str, head: str, python: str) -> list[str]:
    """Why these commits must not go out ([] = they may)."""
    paths = git(wt, "diff", "--name-only", base, head).splitlines()
    why = []
    bad = bad_paths(paths)
    if bad:
        why.append("commits touch paths the review may not change: " + ", ".join(bad))
    failed = run_tests(wt, python)
    if failed:
        why.append("tests fail: " + ", ".join(failed[:10]))
    failed = old_tests_on_new_code(root, base, head, python)
    if failed:
        why.append("the tests from before tonight's commits fail on the new code (recorded behaviour changed): "
                   + ", ".join(failed[:10]))
    if any(p in LOGIC for p in paths):
        changed = squawk_changes(root, base, head, python)
        if changed:
            why.append(f"the squawk replay changed ({len(changed)} cases): " + "; ".join(changed[:5]))
    return why


def publish(root: str, branch: str, remote: str = "origin") -> None:
    """Merge the night's branch into main and push. Commits of the owner's on main, not yet on origin, stop it: they are
    not this cycle's to publish."""
    if git(root, "rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise RuntimeError("the main checkout is not on main")
    git(root, "fetch", "-q", remote)
    unpushed = git(root, "rev-list", "--count", f"{remote}/main..main")
    if unpushed != "0":
        raise RuntimeError(f"main has {unpushed} commit(s) not on {remote}: yours to push first")
    git(root, "merge", "-q", "--no-edit", f"{remote}/main")
    before = git(root, "rev-parse", "HEAD")
    try:
        git(root, "merge", "-q", "--no-edit", branch)
    except RuntimeError:
        git(root, "merge", "--abort", check=False)
        raise
    try:
        git(root, "push", "-q", remote, "main")
    except RuntimeError:
        git(root, "reset", "-q", "--merge", before)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--base", required=True, help="the commit the night's branch was made from")
    p.add_argument("--claude-exit", type=int, default=0)
    a = p.parse_args()
    python, branch = os.path.join(a.root, ".venv", "bin", "python"), f"nightly/{a.date}"
    holds, notes = owner_lines(os.path.join(a.root, "reviews", f"{a.date}.owner"))
    head = git(a.worktree, "rev-parse", "HEAD")
    commits = git(a.worktree, "rev-list", "--count", f"{a.base}..{head}")
    print(f"commits: {commits}; review's holds: {len(holds)}, notes: {len(notes)}")
    why = list(holds)
    if commits != "0":
        why += checks(a.root, a.worktree, a.base, head, python)
    logic = commits != "0" and any(x in LOGIC for x in git(a.worktree, "diff", "--name-only", a.base, head).splitlines())
    pushed = False
    if commits != "0" and not why:
        try:
            publish(a.root, branch)
            pushed = True
        except RuntimeError as e:
            why.append(str(e))
    print("pushed" if pushed else f"not pushed: {why}")
    if a.claude_exit not in (0,) and commits == "0":
        why.append(f"the review ended with exit {a.claude_exit} and no commits")
    lines = []
    if why:
        lines.append(f"Nightly {a.date}: " + (f"{commits} commit(s) NOT pushed, on {branch}." if commits != "0" else "nothing to push."))
        lines += [f"- {w}" for w in why]
    if notes:
        lines += ([] if lines else [f"Nightly {a.date}: pushed {commits} commit(s)." if pushed else f"Nightly {a.date}:"])
        lines += [f"- {n}" for n in notes]
    if lines:
        lines.append(f"Details: reviews/{a.date}.md")
        print("told the owner:", tell_owner("\n".join(lines)))
    return 10 if pushed and logic else 0  # (10: restart the monitor on the new code)


if __name__ == "__main__":
    sys.exit(main())

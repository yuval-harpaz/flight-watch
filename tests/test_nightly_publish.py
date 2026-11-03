"""tools/nightly_publish.py on throwaway git repositories: no network, no Telegram."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import nightly_publish as np  # noqa: E402


def run(cwd, *a):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


class Repo:
    """origin (bare), a main checkout, and a night's branch with one commit."""

    def __init__(self, d):
        self.origin, self.root = os.path.join(d, "origin.git"), os.path.join(d, "root")
        run(d, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        run(d, "git", "clone", "-q", self.origin, self.root)
        run(self.root, "git", "checkout", "-q", "-B", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            run(self.root, "git", "config", k, v)
        self.commit("flight_watch.py", "one\n")
        run(self.root, "git", "push", "-q", "origin", "main")

    def commit(self, path, text):
        with open(os.path.join(self.root, path), "w") as f:
            f.write(text)
        run(self.root, "git", "add", path)
        run(self.root, "git", "commit", "-qm", path)

    def night(self):
        run(self.root, "git", "checkout", "-qb", "nightly/x")
        self.commit("flight_watch.py", "two\n")
        run(self.root, "git", "checkout", "-q", "main")

    def on_origin(self):
        return subprocess.run(["git", "show", "main:flight_watch.py"], cwd=self.origin, capture_output=True, text=True).stdout


class PublishTests(unittest.TestCase):
    def test_owner_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".owner", delete=False) as f:
            f.write("HOLD: a new category\nNOTE: reverted a fix\n\nno prefix\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(np.owner_lines(f.name), (["a new category", "no prefix"], ["reverted a fix"]))
        self.assertEqual(np.owner_lines(f.name + ".absent"), ([], []))

    def test_paths_the_review_may_not_change(self):
        self.assertEqual(np.bad_paths(["flight_watch.py", "tests/x.py", "tools/plot_alert.py", "docs/a.html", "AGENTS.md"]), [])
        for p in ("tools/nightly.sh", "tools/nightly_prompt.md", "tools/nightly_publish.py", ".github/workflows/x.yml",
                  "monitor.sh", "alerts.jsonl", "tools/cors_worker.js"):
            self.assertEqual(np.bad_paths([p]), [p])

    def test_merges_and_pushes(self):
        with tempfile.TemporaryDirectory() as d:
            r = Repo(d)
            r.night()
            np.publish(r.root, "nightly/x")
            self.assertEqual(r.on_origin(), "two\n")

    def test_owners_unpushed_commits_are_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            r = Repo(d)
            r.night()
            r.commit("mine.txt", "work in progress\n")
            with self.assertRaisesRegex(RuntimeError, "not on origin"):
                np.publish(r.root, "nightly/x")
            self.assertEqual(r.on_origin(), "one\n")

    def test_a_merge_conflict_leaves_main_as_it_was(self):
        with tempfile.TemporaryDirectory() as d:
            r = Repo(d)
            r.night()
            r.commit("flight_watch.py", "another\n")
            run(r.root, "git", "push", "-q", "origin", "main")
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.root, capture_output=True, text=True).stdout
            with self.assertRaises(RuntimeError):
                np.publish(r.root, "nightly/x")
            self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.root, capture_output=True, text=True).stdout, head)
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=r.root, capture_output=True, text=True).stdout, "")

    def test_a_failed_push_takes_the_merge_back(self):
        with tempfile.TemporaryDirectory() as d:
            r = Repo(d)
            r.night()
            before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.root, capture_output=True, text=True).stdout
            real = np.git

            def fail_push(cwd, *a, **k):
                if a[0] == "push":
                    raise RuntimeError("git push: refused")
                return real(cwd, *a, **k)
            with mock.patch.object(np, "git", fail_push), self.assertRaisesRegex(RuntimeError, "refused"):
                np.publish(r.root, "nightly/x")
            self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.root, capture_output=True, text=True).stdout, before)

    def test_the_owner_is_told_in_the_private_chat_only(self):
        sent = {}

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"ok": True}).encode()

        def urlopen(url, data, timeout):
            sent["data"] = data.decode()
            return Answer()
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "111", "TELEGRAM_CHANNEL_ID": "@channel"}
        with mock.patch.dict(os.environ, env), mock.patch.object(np.urllib.request, "urlopen", urlopen):
            self.assertTrue(np.tell_owner("held"))
        self.assertIn("chat_id=111", sent["data"])
        self.assertNotIn("channel", sent["data"])
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "", "TELEGRAM_CHANNEL_ID": "@channel"}):
            self.assertFalse(np.tell_owner("held"))  # never falls back on the channel


if __name__ == "__main__":
    unittest.main()

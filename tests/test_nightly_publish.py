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
import nightly_replay  # noqa: E402
import read_trace  # noqa: E402


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
        for p in ("tools/nightly.sh", "tools/nightly_prompt.md", "tools/nightly_publish.py", "tools/nightly_replay.py", "tools/read_trace.py",
                  ".github/workflows/x.yml",
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


class ReplayCommandTests(unittest.TestCase):
    """tools/nightly_replay.py, with the replay itself replaced: what it reports from each result."""

    def run_it(self, after):
        kinds = lambda k: json.dumps({"uri": "u", "post_t": "2026-11-04T00:00", "iata": "LY", "number": "1", "from": "TLV",
                                      "to": "VIE", "alerts": [{"kind": k}]}) + "\n"
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "root")
            os.makedirs(os.path.join(root, "squawks"))
            os.makedirs(os.path.join(root, "tools"))
            os.makedirs(os.path.join(root, ".venv", "bin"))
            os.symlink(sys.executable, os.path.join(root, ".venv", "bin", "python"))
            here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "compare_cases.py")
            os.symlink(os.path.abspath(here), os.path.join(root, "tools", "compare_cases.py"))
            with open(os.path.join(root, "squawks", "cases.jsonl"), "w") as f:
                f.write(kinds("EMERGENCY"))
            work = os.path.join(d, "work")
            os.makedirs(os.path.join(work, "tmp_squawks"))  # a replay left by the previous fix
            with open(os.path.join(work, "tmp_squawks", "old"), "w") as f:
                f.write("x")

            def replay(code, root_, python, out):
                self.assertFalse(os.path.exists(out))
                os.makedirs(out)
                if after is None:
                    raise subprocess.CalledProcessError(1, "squawk_cases", stderr=b"boom")
                path = os.path.join(out, "cases.jsonl")
                with open(path, "w") as f:
                    f.write(kinds(after))
                return path

            cwd = os.getcwd()
            os.chdir(work)
            try:
                with mock.patch.object(nightly_replay, "replay", replay):
                    return nightly_replay.main(["--root", root])
            finally:
                os.chdir(cwd)

    def test_same_kinds_pass(self):
        self.assertEqual(self.run_it("EMERGENCY"), 0)

    def test_a_changed_case_fails(self):
        self.assertEqual(self.run_it("DIVERSION"), 1)

    def test_a_replay_that_did_not_run_is_not_a_pass(self):
        self.assertEqual(self.run_it(None), 2)


class ReadTraceTests(unittest.TestCase):
    def test_only_a_trace_file_of_a_real_hex(self):
        self.assertEqual(read_trace.trace_url("896678"), "https://adsb.lol/data/traces/78/trace_full_896678.json")
        for bad in ("../../etc", "89667", "896678/../x", "zzzzzz"):
            with self.assertRaises(ValueError):
                read_trace.trace_url(bad)

    def test_rows_in_the_window(self):
        tr = {"timestamp": 1000.0, "trace": [[0, 31.0, 35.0, 20000, 400.0, 90.0, 0, 500, {"flight": "ETD608  ", "squawk": "0763"}],
                                             [600, 31.1, 35.5, "ground", 0.0, 90.0, 0, None, None]]}
        self.assertEqual(len(read_trace.rows(tr)), 2)
        got = read_trace.rows(tr, around=1000.0, minutes=5)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].endswith("ETD608 0763"))


if __name__ == "__main__":
    unittest.main()

You are the nightly review of flight-watch, running unattended (no one will answer questions). Be brief and
cheap: read summaries first, grep the raw files only for what you need, never read a whole alerts or log file.

The monitor's day that ended at 03:00 UTC on {DATE}: `{ROOT}/reviews/{DATE}_digest.md` (made by
tools/daily_digest.py). Raw files in `{ROOT}`: alerts_*.jsonl, posts_telegram_*.jsonl, flight_watch_*.log (dated by
the day their run started). Open questions from earlier nights: `{ROOT}/reviews/ISSUES.md`.

You are in a git worktree of the repository on branch `nightly/{DATE}` (made from main). Change code only here, never
in `{ROOT}` (except writing to `{ROOT}/reviews/`). Python: `{ROOT}/.venv/bin/python`. AGENTS.md (read it) says why the
checks are as they are; do not undo a choice it records.

1. Judge each Telegram message of the day (what the public saw): **valid** (a real irregularity, or a
   correct sanity alert), **false** (a routine flight we misread - say what misled us), or **unclear**.
   Use the log lines of that aircraft (grep its hex) and AGENTS.md. When the log does not settle it, or before changing a check for it, read
   the track, as a command of its own: `{ROOT}/.venv/bin/python tools/read_trace.py <hex> --around <UTC YYYY-MM-DDTHH:MM> --minutes 20`.
   Each start in the digest names the code it ran
   (`code <commit>[+changes] (flight_watch.py <sha256 8>)`): judge an alert by the code that raised it, and do not
   report as new a false alarm that a later commit (`git log`) already fixed. Note also anything alerted that the
   digest shows as noisy (many alerts on one aircraft, a kind suddenly frequent) and monitor health (restarts,
   gaps, refused polls, errors).
2. End by implementing your recommendations (the owner's decision, 3 Nov 2026): for every open issue in ISSUES.md not marked
   `[needs owner]`, and everything new you found, choose the option you recommend and do it, the most Telegram messages first,
   with the time you have (stop by ~35 min; what is left stays open with your recommendation). Keep each change as narrow as
   the case allows and record its cost. Each fix gets a unit test in tests/ (the scenario that misled us), a line in AGENTS.md (date,
   case, why), and a commit on this branch (`git add <files>`, never `-A`). Before committing run
   `{ROOT}/.venv/bin/python -m unittest discover -s tests` (all must pass) and, when alert logic changed, the squawk
   replay, as a command of its own: `{ROOT}/.venv/bin/python tools/nightly_replay.py --root {ROOT}` (~3 min; exits 1 when a
   case changed). Do not push: after you end, a script runs its own checks and pushes your commits to main.
3. Do not commit a change that would make us miss what we caught before: a changed squawk-replay case, an existing test whose
   expected alert had to change, an alert you judged valid (this day's or in earlier `{ROOT}/reviews/*.md`) that it would
   lose, or undoing a choice AGENTS.md records as the owner's. Revert it, keep the issue open marked `[needs owner]` with the
   case and the options, and add a line to `{ROOT}/reviews/{DATE}.owner`: `NOTE: <one sentence>`. Your other fixes still go out.
   If you suspect a new category of issue we have not discussed - not one more case of what AGENTS.md or ISSUES.md covers, but
   a kind of failure or of data or behaviour new to us (say, a feed starting to send something else, a monitor failure with
   no known cause, an unseen class of false alarm) - add `HOLD: <one sentence>` to that file and an issue marked `[needs
   owner]`: nothing of this night is pushed, and the script tells the owner. An issue already marked is not new: no second HOLD.
4. Write `{ROOT}/reviews/{DATE}.md` (at most ~60 lines): the verdict per Telegram message (one line each: time,
   flight, kind, verdict, why), monitor health in one or two lines, fixes made (commit, what, test, its cost), and issues raised.
   Append each new issue to `{ROOT}/reviews/ISSUES.md` as `- [ ] {DATE} <title>: <the question for the owner, with
   the case, the options and your recommendation>`; tick (`[x]`) those this day's data or fixes resolved. Do not repeat an
   issue that is already open; add the new case to it instead.

Never: print or write secrets (tokens, env values), send anything to Telegram or other services, change Telegram /
Bluesky settings, commit alerts / posts / logs, or start / stop the monitor. End with one line: the review file's path.

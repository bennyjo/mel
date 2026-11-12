"""`.github/scripts/checked-base.sh` against a fake `gh`. Offline."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "checked-base.sh"
STEP = "No agent commit touches operator-owned files"

FAKE_GH = """#!/usr/bin/env bash
case "$2" in
  *"/jobs") cat "$FAKE_DIR/jobs.json" ;;
  *"page=1") cat "$FAKE_DIR/runs.json" ;;
  *) echo '{"workflow_runs": []}' ;;
esac
"""


def run_script(runs: list[dict], jobs: dict, head: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        gh = tmp / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        (tmp / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
        (tmp / "jobs.json").write_text(json.dumps(jobs))
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "FAKE_DIR": str(tmp),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "100",
        }
        return subprocess.run(["bash", str(SCRIPT), STEP], cwd=REPO, env=env,
                              capture_output=True, text=True)


class TestCheckedBase(unittest.TestCase):

    def test_first_run_checks_head_only(self):
        r = run_script([], {"jobs": []}, "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0" * 40)

    def test_only_this_run_counts_as_first(self):
        runs = [{"id": 100, "head_sha": "a" * 40, "event": "push"}]
        r = run_script(runs, {"jobs": []}, "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0" * 40)

    def test_earlier_run_that_never_checked_still_fails(self):
        runs = [{"id": 7, "head_sha": "a" * 40, "event": "schedule"}]
        r = run_script(runs, {"jobs": [{"steps": []}]}, "")
        self.assertEqual(r.returncode, 1)

    def test_earlier_checked_run_on_main_is_the_base(self):
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
        runs = [{"id": 7, "head_sha": head, "event": "schedule"}]
        jobs = {"jobs": [{"steps": [{"name": STEP, "conclusion": "success"}]}]}
        r = run_script(runs, jobs, head)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), head)


if __name__ == "__main__":
    unittest.main()

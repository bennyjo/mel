"""`core/lease.py`: what a lease means to a runner, and two simulated runners
against a local bare remote. Offline."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core import lease

T0 = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc)
LEASE_FILE = "journal/lease.json"


class TestDescribe(unittest.TestCase):
    def test_no_lease_is_free(self):
        d = lease.describe({}, "cloud", T0)
        self.assertFalse(d["held"])

    def test_a_released_lease_is_free(self):
        d = lease.describe({"runner": None, "released_by": "cloud",
                            "released": "2026-09-21T11:59:00Z"}, "operator", T0)
        self.assertFalse(d["held"])

    def test_a_fresh_lease_by_the_other_runner_is_held(self):
        d = lease.describe({"runner": "operator", "started": "2026-09-21T11:50:00Z",
                            "ttl_s": 3000}, "cloud", T0)
        self.assertTrue(lease.held_by_other(d))
        self.assertEqual(d["age_s"], 600)

    def test_an_expired_lease_is_not_fresh(self):
        d = lease.describe({"runner": "operator", "started": "2026-09-21T10:00:00Z",
                            "ttl_s": 3000}, "cloud", T0)
        self.assertTrue(d["held"])
        self.assertFalse(d["fresh"])

    def test_a_lease_from_a_clock_slightly_ahead_is_fresh(self):
        d = lease.describe({"runner": "operator", "started": "2026-09-21T12:00:02Z",
                            "ttl_s": 3000}, "cloud", T0)
        self.assertTrue(d["fresh"])

    def test_my_own_lease_is_mine(self):
        d = lease.describe({"runner": "cloud", "started": "2026-09-21T11:59:00Z"}, "cloud", T0)
        self.assertTrue(d["mine"])

    def test_a_garbled_payload_is_held_but_not_fresh(self):
        d = lease.describe({"runner": "operator", "ttl_s": "x"}, "cloud", T0)
        self.assertTrue(d["held"])
        self.assertFalse(d["fresh"])


class TestRunnerId(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(lease, "checkout_id", return_value="abcd1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_operator_when_the_loop_pushes(self):
        with mock.patch.dict(os.environ, {"MEL_PUSH_BY_LOOP": "1"}):
            os.environ.pop("MEL_RUNNER", None)
            self.assertEqual(lease.runner_id(), "operator-abcd1234")

    def test_cloud_otherwise(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("MEL_RUNNER", None)
            os.environ.pop("MEL_PUSH_BY_LOOP", None)
            self.assertEqual(lease.runner_id(), "cloud-abcd1234")

    def test_mel_runner_overrides(self):
        with mock.patch.dict(os.environ, {"MEL_RUNNER": "laptop 2"}):
            self.assertEqual(lease.runner_id(), "laptop_2")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=check).stdout.strip()


class TwoRunners(unittest.TestCase):
    """Two clones of one bare "origin" that share origin/main, as two runners do."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mel-lease-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("MEL_RUNNER", "MEL_PUSH_BY_LOOP", "MEL_LEASE")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="runner", GIT_AUTHOR_EMAIL="runner@example.com",
                   GIT_COMMITTER_NAME="runner", GIT_COMMITTER_EMAIL="runner@example.com")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        git(self.tmp, "init", "--quiet", "-b", "main", str(seed))
        (seed / "core").mkdir()
        shutil.copy(REPO / "core" / "lease.py", seed / "core" / "lease.py")
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "seed")
        git(seed, "push", "--quiet", str(self.origin), "main")
        self.cloud = self.clone("cloud")
        self.operator = self.clone("operator")

    def clone(self, name):
        path = self.tmp / name
        git(self.tmp, "clone", "--quiet", str(self.origin), str(path))
        return path

    def lease(self, runner, *args, name=None):
        """Run the runner's lease.py; `name` "" leaves MEL_RUNNER unset."""
        env = dict(os.environ)
        name = runner.name if name is None else name
        if name:
            env["MEL_RUNNER"] = name
        proc = subprocess.run([sys.executable, str(runner / "core" / "lease.py"), *args],
                              capture_output=True, text=True, env=env)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"lease.py {' '.join(args)} printed no JSON: {proc.stderr}")

    def origin_lease(self):
        shown = subprocess.run(["git", "show", f"lease:{LEASE_FILE}"], cwd=self.origin,
                               capture_output=True, text=True)
        return json.loads(shown.stdout) if shown.returncode == 0 else None

    def origin_main(self):
        return git(self.origin, "rev-parse", "main")

    def origin_lease_tip(self):
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/lease"],
                              cwd=self.origin, capture_output=True, text=True)
        return proc.stdout.strip() or None

    def lease_subjects(self):
        return git(self.origin, "log", "--format=%s", "refs/heads/lease").splitlines()

    def sync(self, runner):
        git(runner, "fetch", "--quiet", "origin")
        git(runner, "merge", "--quiet", "--ff-only", "origin/main")

    def cycle(self, runner, name):
        """What a tick leaves behind: a cycle commit pushed to main."""
        log = runner / "journal" / "cycles.md"
        log.parent.mkdir(exist_ok=True)
        with log.open("a") as f:
            f.write(f"{name} done\n")
        git(runner, "add", "-A")
        git(runner, "commit", "--quiet", "-m", name)
        git(runner, "push", "--quiet", "origin", "HEAD:main")

    def test_second_runner_skips_while_the_first_holds(self):
        rc, got = self.lease(self.cloud, "acquire")
        self.assertEqual((rc, got["acquired"], got["written"]), (0, True, True))
        self.assertEqual(self.origin_lease()["runner"], "cloud")

        before = self.origin_lease_tip()
        rc, got = self.lease(self.operator, "acquire")
        self.assertEqual((rc, got["acquired"], got["runner"]), (3, False, "cloud"))
        self.assertEqual(self.origin_lease_tip(), before)

        rc, got = self.lease(self.operator, "check")
        self.assertEqual((rc, got["held"], got["fresh"], got["mine"]), (0, True, True, False))

    def test_two_cloud_runs_exclude_each_other(self):
        # Cycles 518 and 519 (2026-10-07): two cloud runs, same default
        # identity, one still settling when the next started.
        rc, first = self.lease(self.cloud, "acquire", name="")
        self.assertEqual(rc, 0)
        self.assertTrue(first["me"].startswith("cloud-"))
        self.assertEqual(self.lease(self.cloud, "env", name="")[1]["me"], first["me"],
                         "one checkout must keep one id, or release cannot find its lease")

        self.sync(self.operator)
        rc, second = self.lease(self.operator, "acquire", name="")
        self.assertTrue(second["me"].startswith("cloud-"))
        self.assertNotEqual(second["me"], first["me"])
        self.assertEqual((rc, second["acquired"], second["runner"]), (3, False, first["me"]))

        self.assertEqual(self.lease(self.cloud, "release", name="")[1]["released"], True)
        self.sync(self.operator)
        self.assertEqual(self.lease(self.operator, "acquire", name="")[0], 0)

    def test_release_after_the_push_hands_the_lease_over(self):
        self.lease(self.cloud, "acquire")
        self.cycle(self.cloud, "cycle 518: light tick")
        rc, got = self.lease(self.cloud, "release")
        self.assertEqual((rc, got["released"]), (0, True))
        self.assertIsNone(self.origin_lease()["runner"])

        self.sync(self.operator)
        rc, got = self.lease(self.operator, "acquire")
        self.assertEqual((rc, got["acquired"]), (0, True))
        self.assertEqual(self.origin_lease()["runner"], "operator")
        self.assertEqual(self.lease_subjects(), ["lease: operator acquired",
                                                 "lease: cloud released",
                                                 "lease: cloud acquired"])
        self.assertEqual(git(self.origin, "log", "--format=%s", "main").splitlines()[0],
                         "cycle 518: light tick")

    def test_acquire_on_a_missing_branch_creates_it(self):
        self.assertIsNone(self.origin_lease_tip())
        rc, got = self.lease(self.cloud, "check")
        self.assertEqual((rc, got["held"]), (0, False))
        rc, got = self.lease(self.cloud, "acquire")
        self.assertEqual((rc, got["acquired"], got["written"], got["replaced"]),
                         (0, True, True, None))
        self.assertEqual(self.origin_lease()["runner"], "cloud")
        # A root commit: the branch shares no history with main.
        self.assertEqual(git(self.origin, "rev-list", "--parents", "-n1", "refs/heads/lease").split(),
                         [self.origin_lease_tip()])

    def test_the_lease_never_adds_a_commit_to_main(self):
        main_before = self.origin_main()
        self.lease(self.cloud, "acquire")
        self.lease(self.cloud, "release")
        self.assertEqual(self.origin_main(), main_before)
        self.assertEqual(git(self.cloud, "rev-parse", "HEAD"), main_before,
                         "acquire must leave the local checkout alone")
        self.assertEqual(git(self.origin, "ls-tree", "-r", "--name-only", "main")
                         .count(LEASE_FILE), 0)

    def test_lease_commits_change_only_the_lease_file(self):
        self.lease(self.cloud, "acquire")
        self.lease(self.cloud, "release")
        shas = git(self.origin, "rev-list", "refs/heads/lease").split()
        self.assertEqual(len(shas), 2)
        for sha in shas:
            files = git(self.origin, "diff-tree", "--root", "--no-commit-id",
                        "--name-only", "-r", sha)
            self.assertEqual(files, LEASE_FILE)

    def test_a_non_fast_forward_push_to_the_lease_branch_is_rejected(self):
        # The contention primitive: a runner that built on a stale lease tip
        # (or raced to create the branch) cannot overwrite the winner.
        self.lease(self.cloud, "acquire")
        winner = self.origin_lease_tip()
        # Run in the operator clone: a root commit pushed over an existing branch.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from core import lease; "
             "c = lease.lease_commit(None, {'runner': 'operator', 'started': 'x', 'ttl_s': 1}, 'm'); "
             "ok, err = lease.push(c); print(int(ok))", str(self.operator)],
            cwd=self.operator, capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), "0", proc.stderr)
        self.assertEqual(self.origin_lease_tip(), winner)

    def test_racing_runners_exactly_one_wins(self):
        # Both runners read a free lease; the cloud's push lands between the
        # operator's read and its push. Deterministic: the operator's
        # lease.py runs in-process with push() wrapped to let the cloud in.
        spec = importlib.util.spec_from_file_location(
            "lease_operator", self.operator / "core" / "lease.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        real_push, cloud_result = mod.push, {}

        def push_after_the_cloud(commit):
            cloud_result["rc"], cloud_result["out"] = self.lease(self.cloud, "acquire")
            return real_push(commit)

        before = git(self.operator, "rev-parse", "HEAD")
        stdout = io.StringIO()
        with mock.patch.object(mod, "push", push_after_the_cloud), \
                mock.patch.dict(os.environ, {"MEL_RUNNER": "operator"}), \
                contextlib.redirect_stdout(stdout):
            rc = mod.cmd_acquire(argparse.Namespace(ttl=mod.DEFAULT_TTL_S))
        got = json.loads(stdout.getvalue())

        self.assertEqual(cloud_result["rc"], 0)
        self.assertEqual((rc, got["acquired"], got["runner"]), (3, False, "cloud"))
        self.assertIn("lost the race", got["reason"])
        self.assertEqual(self.origin_lease()["runner"], "cloud")
        self.assertEqual(git(self.operator, "rev-parse", "HEAD"), before,
                         "a lost race must leave the loser's branch alone")

    def test_concurrent_acquires_never_both_win(self):
        for _ in range(5):
            self.sync(self.cloud)
            self.sync(self.operator)
            results = {}
            start = threading.Barrier(2)

            def run(runner):
                start.wait()
                results[runner.name] = self.lease(runner, "acquire")

            threads = [threading.Thread(target=run, args=(r,)) for r in (self.cloud, self.operator)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            winners = [name for name, (rc, _) in results.items() if rc == 0]
            self.assertEqual(len(winners), 1, results)
            loser = "operator" if winners == ["cloud"] else "cloud"
            self.assertIn(results[loser][0], (3, 4), results)
            self.assertEqual(self.origin_lease()["runner"], winners[0])
            winner = self.cloud if winners == ["cloud"] else self.operator
            self.assertEqual(self.lease(winner, "release")[0], 0)

    def test_a_refused_write_fails_closed(self):
        # The cloud's "remote end hung up" on refs/mel/lease, as a remote
        # that refuses every push. The old lease proceeded "unprotected".
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'remote end hung up' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = git(self.cloud, "rev-parse", "HEAD")
        rc, got = self.lease(self.cloud, "acquire")
        self.assertEqual((rc, got["acquired"], got["written"]), (4, False, False))
        self.assertIn("push refused", got["reason"])
        self.assertIsNone(self.origin_lease())
        self.assertEqual(git(self.cloud, "rev-parse", "HEAD"), before)

    def test_a_runner_on_a_stale_main_cannot_take_the_lease(self):
        # a00018 (2026-09-27): a checkout behind origin settles a row that
        # origin already closed.
        self.cycle(self.cloud, "cycle 300: light tick")
        rc, got = self.lease(self.operator, "acquire")
        self.assertEqual(rc, 4)
        self.assertIn("behind", got["reason"])

        (self.operator / "local.txt").write_text("unpushed\n")
        git(self.operator, "add", "-A")
        git(self.operator, "commit", "--quiet", "-m", "cycle 301: light tick")
        git(self.operator, "fetch", "--quiet", "origin")
        rc, got = self.lease(self.operator, "acquire")
        self.assertEqual(rc, 4)
        self.assertIn("diverged", got["reason"])
        self.assertIsNone(self.origin_lease())

    def test_an_expired_lease_is_taken_over(self):
        self.lease(self.cloud, "acquire", "--ttl", "0")
        self.sync(self.operator)
        rc, got = self.lease(self.operator, "acquire")
        self.assertEqual((rc, got["acquired"], got["replaced"]), (0, True, "cloud"))
        self.assertEqual(self.origin_lease()["runner"], "operator")

    def test_a_runner_renews_its_own_lease(self):
        # loop.sh renews its lease at the top of every tick; a crashed run
        # retakes its own the same way.
        self.lease(self.cloud, "acquire")
        rc, got = self.lease(self.cloud, "acquire")
        self.assertEqual((rc, got["replaced"]), (0, "cloud"))

    def test_release_leaves_the_other_runners_lease_alone(self):
        self.lease(self.cloud, "acquire")
        rc, got = self.lease(self.operator, "release")
        self.assertEqual((rc, got["released"]), (0, False))
        self.assertEqual(self.origin_lease()["runner"], "cloud")

    def test_release_lands_even_when_main_moved(self):
        # An operator commit lands while the lease is held; release must not
        # need the holder's local main to be current.
        self.lease(self.cloud, "acquire")
        self.sync(self.operator)
        self.cycle(self.operator, "operator: a fix")
        rc, got = self.lease(self.cloud, "release")
        self.assertEqual((rc, got["released"]), (0, True))
        self.assertIsNone(self.origin_lease()["runner"])
        self.assertIn("operator: a fix", git(self.origin, "log", "--format=%s", "main"))
        self.assertNotIn("lease: cloud released", git(self.origin, "log", "--format=%s", "main"))

    def strand_a_cycle(self, runner, name):
        """CYCLE.md step 8's failure: a commit on a detached HEAD, then a
        `git push origin main` that succeeds without publishing it."""
        git(runner, "checkout", "--quiet", "--detach")
        (runner / "cycle.txt").write_text(f"{name}\n")
        git(runner, "add", "-A")
        git(runner, "commit", "--quiet", "-m", name)
        git(runner, "push", "--quiet", "origin", "main")
        return git(runner, "rev-parse", "HEAD")

    def assert_reports_unpublished(self, rc, got, commits):
        self.assertEqual(rc, 1, got)
        self.assertIn("unpublished commits", got["error"])
        self.assertEqual(got["unpublished_commits"], commits)

    def test_release_with_stranded_commits_fails_but_frees_the_lease(self):
        # The release still lands: a stranded runner must not keep the other
        # runner out (CYCLE.md step 8, loop.sh's divergence path).
        self.lease(self.cloud, "acquire")
        stranded = self.strand_a_cycle(self.cloud, "cycle 813: full")
        rc, got = self.lease(self.cloud, "release")
        self.assert_reports_unpublished(rc, got, 1)
        self.assertTrue(got["released"])
        self.assertIsNone(self.origin_lease()["runner"])
        self.assertNotEqual(
            subprocess.run(["git", "cat-file", "-e", stranded], cwd=self.origin,
                           capture_output=True).returncode, 0,
            "the commit must still be unpublished, or this proves nothing")

    def test_release_counts_a_chain_of_stranded_commits(self):
        self.lease(self.cloud, "acquire")
        self.strand_a_cycle(self.cloud, "cycle 813: full")
        self.strand_a_cycle(self.cloud, "cycle 814: full")
        rc, got = self.lease(self.cloud, "release")
        self.assert_reports_unpublished(rc, got, 2)

    def test_stranded_commits_fail_a_release_with_no_lease_to_give_back(self):
        self.strand_a_cycle(self.cloud, "cycle 813: full")
        rc, got = self.lease(self.cloud, "release")
        self.assert_reports_unpublished(rc, got, 1)
        self.assertEqual((got["released"], got["reason"]), (False, "no lease held"))

    def test_stranded_commits_fail_a_release_of_a_lease_lost_to_the_other_runner(self):
        self.lease(self.cloud, "acquire", "--ttl", "0")
        self.strand_a_cycle(self.cloud, "cycle 813: full")
        self.sync(self.operator)
        self.lease(self.operator, "acquire")
        rc, got = self.lease(self.cloud, "release")
        self.assert_reports_unpublished(rc, got, 1)
        self.assertFalse(got["released"])
        self.assertEqual(self.origin_lease()["runner"], "operator")

    def test_stranded_commits_are_reported_when_the_release_push_fails(self):
        self.lease(self.cloud, "acquire")
        self.strand_a_cycle(self.cloud, "cycle 813: full")
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        rc, got = self.lease(self.cloud, "release")
        self.assert_reports_unpublished(rc, got, 1)
        self.assertFalse(got["released"])
        self.assertEqual(self.origin_lease()["runner"], "cloud")

    def test_release_fails_when_head_cannot_be_compared(self):
        self.lease(self.cloud, "acquire")
        git(self.cloud, "symbolic-ref", "HEAD", "refs/heads/unborn")
        rc, got = self.lease(self.cloud, "release")
        self.assertEqual((rc, got["released"]), (1, True))
        self.assertIn("could not compare HEAD", got["error"])
        self.assertNotIn("unpublished_commits", got)

    def test_unreachable_origin_fails_closed(self):
        git(self.cloud, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        rc, got = self.lease(self.cloud, "acquire")
        self.assertEqual((rc, got["acquired"]), (4, False))
        self.assertIn("unreachable", got["reason"])

    def test_no_remote_has_nothing_to_protect(self):
        git(self.cloud, "remote", "remove", "origin")
        rc, got = self.lease(self.cloud, "acquire")
        self.assertEqual((rc, got["acquired"], got["written"]), (0, True, False))


if __name__ == "__main__":
    unittest.main()

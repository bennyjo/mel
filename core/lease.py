#!/usr/bin/env python3
"""Runner lease: one cycle at a time across Mel's runners.

PROTECTED ENGINE FILE - the allocating agent must not edit files under core/.

Two runners can cycle on the same hour: two cloud routine runs, or a cloud
run and the operator machine's loop.sh. CYCLE.md step 0's collision guard
reads origin/main, so it only sees a cycle that has already committed; two
cycles that start from the same tip cannot see each other. Ported from
Phil, where exactly that happened on 2026-09-04.

Where it lives (operator): journal/lease.json on origin's `lease` branch,
refs/heads/lease. Not main: with the file on main every cycle added two
`lease:` commits to the product history (65% of main's commits by
2026-10-24). A custom ref, refs/mel/lease, was the first home and every cloud
push to it failed ("remote end hung up", cycles 505 to 552 at least) while
the same sessions pushed main, so every cloud cycle ran unprotected; a
branch under refs/heads/ takes the same credential as main. On 2026-10-07 a
cycle 518 that was still settling when the next hourly run started collided
with that run (cycle 519): both settled f000527, f000528, f000530 and
f000531, and the second push conflicted on journal/forecasts.jsonl.

The file holds one JSON object: {"runner", "started", "ttl_s"} while held,
{"runner": null, "released_by", "released"} once released; a missing file
or a missing branch is free too. acquire and release each push one commit
that changes nothing but this file, as a plain fast-forward push to the
lease branch. Origin applies a push only while the branch still points where
the pusher saw it (or, for the first acquire, does not exist), so of two
runners racing for a free lease exactly one push lands; the loser re-reads
and yields. Nothing here ever commits to main.

acquire builds its commit on origin's lease tip (a root commit when the
branch does not exist yet) and leaves the local checkout alone. It needs
HEAD to contain origin/main: a runner whose main is behind or diverged
would settle rows origin already closed (a00018, 2026-09-27). release
builds its commit on the lease tip too, so it works whatever the cycle's
own push did.

So a release lands whether or not the cycle reached origin. A cycle that
commits on a detached HEAD and runs `git push origin main` pushes the stale
branch and sees success (CYCLE.md step 8), and its release would land too.
release therefore also checks that HEAD is on origin/main and exits 1 with
"unpublished commits" when it is not. The lease still goes back, because a
stranded runner must not keep the other runner out. The exit and the JSON are
the signal: a cloud agent reads them; loop.sh discards them and does its own
push and warnings.

A runner that does not get the lease skips the tick; it does not run a
light one. A light tick still settles and commits ledger and forecast rows,
which are not union-merged (.gitattributes), so a light tick next to the
holder's cycle makes one of the two pushes conflict, as cycle 519's did.
The holder's own cycle settles the same rows, so the skipped tick loses
nothing. That covers both refusals: held by the other runner (exit 3) and
not written (exit 4: origin unreachable, the push refused, or local main
behind or diverged). A runner that cannot write the lease could not publish
its cycle either: a credential that can push main can push the lease
branch. A run that dies mid-cycle
leaves a lease that expires on its own after `ttl_s`, so nothing wedges the
other runner for longer than that.

Runner identity: $MEL_RUNNER, else "operator" when loop.sh's
MEL_PUSH_BY_LOOP is set and "cloud" otherwise, followed by a random id kept
in this checkout's git directory. The id is what tells two cloud runs apart:
cycles 518 and 519 were both "cloud", and with a bare "cloud" the second
would have taken the first's fresh lease as its own. Each cloud run starts
from a fresh clone and gets a fresh id; every tick of one loop.sh shares
one, so the loop renews its own lease. On the operator machine loop.sh
acquires and releases in the interactive shell (the keyring is unlocked
there; a push from inside `claude -p` hangs) and tells the cycle agent
through MEL_LEASE. In the cloud the cycle agent runs acquire and release
itself.

Usage:
  python3 core/lease.py check                # never writes; prints JSON
  python3 core/lease.py acquire [--ttl S]    # exit 0 acquired, 3 held by other, 4 not written
  python3 core/lease.py release              # exit 0 released or not ours, 1 push failed or unpublished commits
  python3 core/lease.py env                  # prints MEL_LEASE and MEL_PUSH_BY_LOOP

Every subcommand prints one JSON object. Without an origin remote there is
no shared lease to protect, so acquire reports "acquired": true with
"written": false. Nothing here writes any file but journal/lease.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEASE_FILE = "journal/lease.json"
MAIN = "refs/heads/main"
REMOTE_MAIN = "refs/remotes/origin/main"
LEASE_REF = "refs/heads/lease"
REMOTE_LEASE = "refs/remotes/origin/lease"
# Longer than any cycle on record (cycle 518 ran 80 minutes, cycle 514 52),
# and under two hourly ticks, so a run that died costs the next tick only.
# Phil's 50 minutes would have let cycle 519 in at 12:03.
DEFAULT_TTL_S = 110 * 60
RELEASE_ATTEMPTS = 3


def runner_id() -> str:
    name = (os.environ.get("MEL_RUNNER") or "").strip()
    if not name:
        kind = "operator" if os.environ.get("MEL_PUSH_BY_LOOP") else "cloud"
        name = f"{kind}-{checkout_id()}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40]


def checkout_id() -> str:
    """A random id kept in this checkout's git directory, made on first use."""
    try:
        gitdir = git("rev-parse", "--absolute-git-dir").stdout.strip()
        path = pathlib.Path(gitdir) / "mel-runner-id"
        if not path.exists():
            path.write_text(secrets.token_hex(4) + "\n")
        return path.read_text().strip()
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def iso(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def git(*args, check=True, input=None, env=None):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=check, timeout=120, input=input,
                          env=env)


def last_line(proc, fallback: str) -> str:
    """The line of git's stderr that says what failed, without the hints."""
    lines = [ln.strip() for ln in proc.stderr.splitlines()
             if ln.strip() and not ln.startswith("hint:")]
    rejected = [ln for ln in lines if "rejected]" in ln]
    return (rejected or lines or [fallback])[-1]


def has_origin() -> bool:
    return git("remote", "get-url", "origin", check=False).returncode == 0


def fetch_main() -> str:
    """origin/main's sha after a fresh fetch.

    Raises RuntimeError when origin cannot be reached; acquire turns that
    into a refusal, since a runner that cannot reach origin cannot publish.
    """
    fetched = git("fetch", "--no-tags", "--quiet", "origin",
                  f"+{MAIN}:{REMOTE_MAIN}", check=False)
    if fetched.returncode != 0:
        raise RuntimeError(last_line(fetched, "fetch failed"))
    return git("rev-parse", REMOTE_MAIN).stdout.strip()


def fetch_lease() -> str | None:
    """origin/lease's sha after a fresh fetch; None when the branch does not
    exist yet (the lease is free). Raises RuntimeError when origin cannot be
    reached."""
    fetched = git("fetch", "--no-tags", "--quiet", "origin",
                  f"+{LEASE_REF}:{REMOTE_LEASE}", check=False)
    if fetched.returncode == 0:
        return git("rev-parse", REMOTE_LEASE).stdout.strip()
    # Missing branch and unreachable origin both fail the fetch; ls-remote
    # tells them apart (exit 2: reachable, no such ref).
    probe = git("ls-remote", "--exit-code", "--heads", "origin", LEASE_REF, check=False)
    if probe.returncode == 2:
        return None
    raise RuntimeError(last_line(fetched, "fetch failed"))


def read_lease(rev: str | None) -> dict:
    """The lease payload at `rev`; {} (free) when absent or unreadable."""
    if rev is None:
        return {}
    shown = git("show", f"{rev}:{LEASE_FILE}", check=False)
    try:
        payload = json.loads(shown.stdout) if shown.returncode == 0 else {}
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def describe(payload: dict, me: str, at: dt.datetime | None = None) -> dict:
    """Pure: what a lease payload means to runner `me` at time `at`."""
    runner = payload.get("runner")
    if not runner:
        return {"held": False, "mine": False, "fresh": False, "runner": None,
                "age_s": None}
    at = at or now()
    try:
        started = dt.datetime.fromisoformat(str(payload.get("started")).replace("Z", "+00:00"))
        age = int((at - started).total_seconds())
    except (TypeError, ValueError):
        age = None
    try:
        ttl = int(payload.get("ttl_s", DEFAULT_TTL_S))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    # abs(): the two runners' clocks differ, so a lease read a moment after
    # it was taken can look a second or two old in the future.
    fresh = age is not None and abs(age) < ttl
    return {"held": True, "mine": runner == me, "fresh": fresh,
            "runner": runner, "age_s": age, "ttl_s": ttl}


def held_by_other(d: dict) -> bool:
    return d["held"] and d["fresh"] and not d["mine"]


def is_ancestor(a: str, b: str) -> bool:
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def lease_commit(parent: str | None, payload: dict, message: str) -> str:
    """A commit on `parent` whose only change is the lease file holding
    `payload`; a root commit when `parent` is None (no lease branch yet).

    Built through a throwaway index, so the working tree and the real index
    stay untouched.
    """
    blob = git("hash-object", "-w", "--stdin",
               input=json.dumps(payload) + "\n").stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        if parent:
            git("read-tree", parent, env=env)
        git("update-index", "--add", "--cacheinfo",
            f"100644,{blob},{LEASE_FILE}", env=env)
        tree = git("write-tree", env=env).stdout.strip()
    parents = ["-p", parent] if parent else []
    return git("commit-tree", tree, *parents, "-m", message).stdout.strip()


def push(commit: str):
    """(ok, error). Origin rejects the push unless the lease branch is still
    an ancestor of `commit` (or, for a root commit, still absent)."""
    pushed = git("push", "--quiet", "origin", f"{commit}:{LEASE_REF}", check=False)
    return pushed.returncode == 0, last_line(pushed, "push rejected")


def out(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj))
    return code


def refuse(me: str, reason: str) -> int:
    return out({"acquired": False, "written": False, "me": me, "reason": reason}, 4)


def cmd_env(args) -> int:
    """The two variables CYCLE.md branches on, for a session whose tool
    allowlist blocks printenv (cycle 113, 2026-09-21). Reads only."""
    return out({"MEL_LEASE": os.environ.get("MEL_LEASE"),
                "MEL_PUSH_BY_LOOP": os.environ.get("MEL_PUSH_BY_LOOP"),
                "me": runner_id()})


def cmd_check(args) -> int:
    me = runner_id()
    if not has_origin():
        return out(dict(describe({}, me), reason="no remote", me=me))
    try:
        base = fetch_main()
        tip = fetch_lease()
    except RuntimeError as e:
        return out(dict(describe({}, me), reason=f"unreachable: {e}", me=me))
    return out(dict(describe(read_lease(tip), me), me=me, main=base, lease=tip))


def cmd_acquire(args) -> int:
    me = runner_id()
    if not has_origin():
        return out({"acquired": True, "written": False, "reason": "no remote", "me": me})
    try:
        base = fetch_main()
        tip = fetch_lease()
    except RuntimeError as e:
        return refuse(me, f"origin unreachable: {e}")
    d = describe(read_lease(tip), me)
    if held_by_other(d):
        return out(dict(d, acquired=False, me=me,
                        reason=f"held by {d['runner']} for {d['age_s']}s of {d['ttl_s']}s"), 3)
    head = git("rev-parse", "HEAD").stdout.strip()
    if not is_ancestor(base, head):
        where = "behind" if is_ancestor(head, base) else "diverged from"
        return refuse(me, f"local HEAD is {where} origin/main {base[:12]}; "
                          "a runner on a stale ledger must not cycle")
    payload = {"runner": me, "started": iso(now()), "ttl_s": args.ttl}
    try:
        commit = lease_commit(tip, payload, f"lease: {me} acquired")
    except subprocess.CalledProcessError as e:
        return refuse(me, f"could not build the lease commit: {e.stderr.strip()}")
    ok, err = push(commit)
    if not ok:
        # Either the other runner's lease landed first (lost the race) or
        # origin refused the write. Re-read to say which; neither may cycle.
        try:
            d2 = describe(read_lease(fetch_lease()), me)
        except RuntimeError:
            d2 = describe({}, me)
        if held_by_other(d2):
            return out(dict(d2, acquired=False, me=me, reason=f"lost the race: {err}"), 3)
        return refuse(me, f"push refused: {err}")
    result = {"acquired": True, "written": True, "me": me, "sha": commit,
              "started": payload["started"], "ttl_s": args.ttl,
              "replaced": d["runner"]}
    return out(result)


def release_out(result: dict, base: str, code: int = 0) -> int:
    """Print a release result. Commits missing from origin/main `base` make it
    a failure, whatever happened to the lease."""
    ahead = git("rev-list", "--count", f"{base}..HEAD", check=False)
    if ahead.returncode != 0:
        result["error"] = ("could not compare HEAD with origin/main "
                           f"{base[:12]}: {last_line(ahead, 'rev-list failed')}")
        code = 1
    elif int(ahead.stdout) > 0:
        result["unpublished_commits"] = int(ahead.stdout)
        result["error"] = (f"unpublished commits: {int(ahead.stdout)} on HEAD missing from "
                           f"origin/main {base[:12]}; publish them as CYCLE.md step 8 says "
                           "and do not report them as published until they land")
        code = 1
    return out(result, code)


def cmd_release(args) -> int:
    me = runner_id()
    if not has_origin():
        return out({"released": False, "reason": "no remote", "me": me})
    err = "push rejected"
    for _ in range(RELEASE_ATTEMPTS):
        try:
            base = fetch_main()
        except RuntimeError as e:
            return out({"released": False, "me": me, "reason": f"unreachable: {e}"}, 1)
        try:
            tip = fetch_lease()
        except RuntimeError as e:
            return release_out({"released": False, "me": me, "reason": f"unreachable: {e}"}, base, 1)
        d = describe(read_lease(tip), me)
        if not d["held"]:
            return release_out({"released": False, "reason": "no lease held", "me": me}, base)
        if not d["mine"]:
            return release_out({"released": False, "reason": f"held by {d['runner']}, not ours",
                                "me": me, "runner": d["runner"], "age_s": d["age_s"]}, base)
        payload = {"runner": None, "released_by": me, "released": iso(now())}
        try:
            commit = lease_commit(tip, payload, f"lease: {me} released")
        except subprocess.CalledProcessError as e:
            return release_out({"released": False, "me": me,
                                "reason": f"could not build the release commit: {e.stderr.strip()}"},
                               base, 1)
        ok, err = push(commit)
        if ok:
            return release_out({"released": True, "me": me, "sha": commit, "age_s": d["age_s"]}, base)
        # The lease branch moved between the fetch and the push: rebuild on
        # the new tip and try again.
    return release_out({"released": False, "me": me, "reason": err}, base, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    a = sub.add_parser("acquire")
    a.add_argument("--ttl", type=int, default=DEFAULT_TTL_S,
                   help=f"seconds before a held lease expires (default {DEFAULT_TTL_S})")
    sub.add_parser("release")
    sub.add_parser("env")
    args = ap.parse_args()
    return {"check": cmd_check, "acquire": cmd_acquire, "release": cmd_release,
            "env": cmd_env}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

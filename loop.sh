#!/usr/bin/env bash
# Run Mel cycles headlessly.
# Usage: ./loop.sh [cycles] [sleep_minutes]
#
# Each cycle hands CYCLE.md to a headless Claude Code session, then enforces
# the operator/agent boundary locally. CI enforces it again on the pushed
# range, because this script can be bypassed or buggy and CI cannot.
#
# There is no real-money mode and no flag that could create one. Real
# allocation is dormant (`core/real.py`), and this script asserts that before
# every cycle rather than trusting it.
set -euo pipefail
cd "$(dirname "$0")"

CYCLES="${1:-1}"
SLEEP_MIN="${2:-60}"

# One loop per checkout. Two loop.sh processes in the same working tree
# commit over each other and collide with the cloud routine (2026-09-17 to
# 2026-09-21: a second loop ran beside the first for four days). The pid
# file guards the start; a stale file left by a crashed loop is harmless
# because the pid is checked for liveness.
LOCK=".loop.pid"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "ERROR: loop.sh is already running in this checkout (pid $(cat "$LOCK")); refusing to start a second one" >&2
  exit 1
fi
echo $$ > "$LOCK"

# The lease says "a local loop is alive", not "a cycle is in flight". It is
# taken with a TTL longer than the gap between cycles and refreshed at the
# top of each one, so the cloud routine sees it held for as long as this
# loop lives and takes over within LEASE_TTL_S of the laptop sleeping. A
# per-cycle lease covered only the four minutes a cycle runs, so both
# runners woke every hour and both committed: 2026-09-21 to 09-23, four
# divergences and one in journal/allocations.jsonl (cycle 159).
LEASE_TTL_S=$(( (SLEEP_MIN + 30) * 60 ))
trap 'rm -f "$LOCK"; MEL_PUSH_BY_LOOP=1 python3 core/lease.py release >/dev/null 2>&1 || true' EXIT

# Operator-owned. Must stay in step with the PROTECTED regex in
# .github/scripts/boundary.sh; that one is the check that matters, this one is
# the local repair.
PROTECTED_PATHS=(core/ config/ .github/ CYCLE.md CHAIN.md loop.sh CLAUDE.md
                 LICENSE README.md .gitignore journal/operator-notes.md)
PROTECTED_RE='^(core/|config/|\.github/|CYCLE\.md|CHAIN\.md|loop\.sh|CLAUDE\.md|LICENSE|README\.md|\.gitignore|journal/operator-notes\.md)'

for i in $(seq 1 "$CYCLES"); do
  echo "=== cycle $i/$CYCLES $(date -u +%FT%TZ) ==="

  # With HEAD detached, `git push origin main` pushes the stale branch ref and
  # reports success, so the cycle's commits never leave the machine. Reattach
  # before any sync or push logic judges state.
  if ! git symbolic-ref -q HEAD >/dev/null; then
    echo "WARNING: HEAD detached - reattaching main to HEAD" >&2
    git checkout -B main HEAD
  fi

  # Sync before anything else. What the last session left uncommitted is not
  # the record - committed work is - and a stale half-settlement left in the
  # tree is exactly how a row gets settled twice (a00018, 2026-09-27: the
  # cloud had already settled it while the laptop slept). So uncommitted
  # journal edits are saved to work/ and dropped, then:
  #   behind      -> fast-forward;
  #   diverged    -> rebase this machine's commits onto origin if none of them
  #                  touches the ledgers (the prose journals union-merge);
  #   otherwise   -> skip the cycle. Never cycle on a stale ledger.
  SYNC_OK=1
  if git remote get-url origin >/dev/null 2>&1; then
    if git fetch origin main 2>/dev/null; then
      if ! git diff --quiet -- journal/; then
        mkdir -p work/stale
        git diff -- journal/ > "work/stale/$(date -u +%Y%m%dT%H%M%SZ).diff"
        echo "WARNING: dropped uncommitted journal edits from the last session (saved under work/stale/)" >&2
        git checkout -- journal/
      fi
      if git merge-base --is-ancestor HEAD origin/main; then
        git checkout -B main origin/main
      elif ! git merge-base --is-ancestor origin/main HEAD; then
        if git diff --name-only origin/main...HEAD | grep -qE '^journal/(allocations|forecasts)\.jsonl$'; then
          echo "WARNING: diverged and this machine's commits touch the ledger - skipping cycles until the operator reconciles" >&2
          SYNC_OK=0
        elif GIT_EDITOR=true git rebase origin/main >/dev/null 2>&1; then
          echo "diverged: rebased this machine's journal commits onto origin/main" >&2
        else
          git rebase --abort 2>/dev/null || true
          echo "WARNING: diverged and the rebase failed - skipping cycles until the operator reconciles" >&2
          SYNC_OK=0
        fi
      fi
    else
      echo "WARNING: could not fetch origin/main; the runner lease will refuse this tick" >&2
    fi
  fi

  # Real execution is dormant and this is where that is checked, not assumed.
  # `core/real.py doctor` is offline and answers from config/protected.json.
  if ! python3 core/real.py doctor --json | grep -q '"ready": false'; then
    echo "FATAL: core/real.py reports real execution ready. Refusing to cycle." >&2
    exit 1
  fi

  # Runner lease (core/lease.py): one cycle at a time across the cloud
  # routine and this loop. Taken here, in the interactive shell where the
  # keyring is unlocked; MEL_LEASE tells CYCLE.md step 0 it is already taken.
  # Without the lease (the other runner holds it, or it could not be
  # written) the whole tick is skipped. A runner that does not hold the
  # lease has nothing to write: the holder settles the same rows off the
  # same maturities and scores the same book, and two settlements of one
  # row is exactly how the ledger diverged on 2026-09-22.
  SKIP=0
  if [ "$SYNC_OK" -eq 0 ]; then
    # A loop stuck on a divergence must not keep the cloud out: give the
    # lease back so the cloud covers the hours until the operator steps in.
    MEL_PUSH_BY_LOOP=1 python3 core/lease.py release >/dev/null 2>&1 || true
    SKIP=1
  elif ! LEASE="$(MEL_PUSH_BY_LOOP=1 python3 core/lease.py acquire --ttl "$LEASE_TTL_S")"; then
    echo "WARNING: runner lease not taken - skipping this tick: $LEASE" >&2
    SKIP=1
  fi
  if [ "$SKIP" -eq 1 ]; then
    if [ "$i" -lt "$CYCLES" ]; then
      now=$(date +%s); period=$((SLEEP_MIN * 60))
      next=$(( (now / period + 1) * period + 120 ))
      sleep $((next - now))
    fi
    continue
  fi

  # A cycle that starts on a repo failing its own tripwires must repair before
  # it allocates. Skipping the cycle instead would deadlock the loop whenever
  # the break is in strategy/, which is the agent's to fix.
  PROMPT="$(cat CYCLE.md)"
  # The session's tool allowlist blocks printenv and $VAR, so the runner
  # verdict is stated in the prompt itself (cycle 113's proposal).
  PROMPT="Runner context from loop.sh: MEL_LEASE=acquired, MEL_PUSH_BY_LOOP=1. You are on the operator's machine; loop.sh took the lease and pushes for you.

${PROMPT}"
  REPAIR=0
  if ! VALIDATE_OUT="$(python3 core/validate.py 2>&1)"; then
    echo "WARNING: core/validate.py is failing - this cycle repairs, it does not allocate" >&2
    REPAIR=1
    PROMPT="REPAIR CYCLE. core/validate.py is failing. Fix what is yours to fix,
put anything under a protected path in journal/proposals.md, allocate nothing,
and log the cycle. The failure:

${VALIDATE_OUT}

${PROMPT}"
  fi

  # Model per tick (operator, 2026-09-23): Opus 5.5 for a full cycle or a
  # repair, Sonnet 5 for a light tick. Pinned because an unpinned `claude -p`
  # takes the account-level default, which follows the model last picked in
  # any interactive session: from 2026-09-15 to 2026-09-21 that ran these
  # cycles on Fable. The hour test is the one CYCLE.md step 0 applies, and a
  # failed read falls through to Opus, so a mistake costs money rather than
  # running a full cycle on Sonnet. The collision guard is left to CYCLE.md:
  # an operator commit landing before the session's own fetch would flip it,
  # and the error would go the wrong way.
  MODEL=claude-opus-5-5
  MODEL_WHY="full-cycle hour, or schedule.json unreadable"
  if [ "$REPAIR" -eq 1 ]; then
    MODEL_WHY="repair cycle"
  elif python3 -c '
import datetime as dt, json, sys
hours = json.load(open("strategy/schedule.json"))["full_cycle_hours_utc"]
sys.exit(0 if dt.datetime.now(dt.timezone.utc).hour not in hours else 1)
' 2>/dev/null; then
    MODEL=claude-sonnet-5-5
    MODEL_WHY="light tick: not a full-cycle hour"
  fi
  echo "model: $MODEL ($MODEL_WHY)" >&2

  # No WebSearch and no WebFetch: the only network this agent may use is the
  # RPC, through core/rpc.py. The explorer is behind Cloudflare and working
  # around that is not the answer. No `git push` either - this shell pushes,
  # below, where the credential helper can reach an unlocked keyring; a push
  # inside `claude -p` hangs on a prompt no headless session can answer.
  CMD=(claude -p "$PROMPT" --model "$MODEL"
       --allowedTools "Read" "Glob" "Grep" "Edit" "Write"
         "Bash(python3 core/*)"
         "Bash(git add:*)" "Bash(git commit:*)" "Bash(git status:*)"
         "Bash(git log:*)" "Bash(git diff:*)" "Bash(git rev-parse:*)"
         "Bash(git symbolic-ref:*)" "Bash(git merge-base:*)"
         "Bash(git rev-list:*)" "Bash(git fetch:*)"
         "Bash(git checkout -B main origin/main)" "Bash(git checkout -B main HEAD)"
       --permission-mode acceptEdits)

  # MEL_PUSH_BY_LOOP tells CYCLE.md step 8 not to push: this shell pushes
  # below, where the credential helper can reach the keyring. A cloud
  # routine runs the same CYCLE.md without the variable and pushes itself.
  MEL_PUSH_BY_LOOP=1 MEL_LEASE=acquired \
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/usr/bin/true GIT_EDITOR=true \
    "${CMD[@]}" || echo "cycle $i failed; continuing"

  # The boundary, locally. Uncommitted edits to protected paths are reverted;
  # committed ones are reported, because rewriting the agent's history behind
  # its back is worse than a loud warning and a CI failure on the push.
  if ! git diff --quiet HEAD -- "${PROTECTED_PATHS[@]}"; then
    echo "WARNING: agent touched protected files - reverting" >&2
    git checkout -- "${PROTECTED_PATHS[@]}"
  fi
  for sha in $(git rev-list --no-merges -5 HEAD); do
    case "$(git log -1 --format=%s "$sha")" in operator:*) continue ;; esac
    if git diff-tree --no-commit-id --name-only -r "$sha" | grep -qE "$PROTECTED_RE"; then
      echo "WARNING: agent commit $sha touches operator-owned paths - CI will fail this push" >&2
    fi
  done

  # The ledger is written by core/ and committed by the agent, so a hand-edited
  # row looks exactly like an honest one in the diff. core/validate.py is what
  # tells them apart: it replays every row through the caps as of the moment it
  # was written. A cycle that leaves it failing must not be trusted for a score.
  if ! python3 core/validate.py >/dev/null 2>&1; then
    echo "WARNING: core/validate.py fails after this cycle - do not trust the score until the operator looks" >&2
  fi

  if git remote get-url origin >/dev/null 2>&1; then
    if ! git symbolic-ref -q HEAD >/dev/null; then
      echo "WARNING: HEAD detached after cycle - reattaching main to HEAD" >&2
      git checkout -B main HEAD
    fi
    if ! git push origin main; then
      echo "push rejected - rebasing onto origin/main and retrying" >&2
      # core/ appends to journal/rpc-usage.jsonl during the session, and a
      # dirty tree makes `pull --rebase` refuse outright (2026-09-22).
      if ! git diff --quiet -- journal/rpc-usage.jsonl; then
        git add journal/rpc-usage.jsonl
        git commit -q -m "operator: rpc usage telemetry from cycle $i" || true
      fi
      if git pull --rebase --autostash origin main; then
        git push origin main \
          || echo "WARNING: push still failing after rebase - resolve manually" >&2
      else
        # Never hand-resolve a ledger conflict here; leave it for the operator.
        git rebase --abort 2>/dev/null || true
        echo "WARNING: rebase failed - commits are local only, resolve manually" >&2
      fi
    fi
  fi

  # Wake two minutes past the next SLEEP_MIN boundary, not SLEEP_MIN after
  # the session ended. A fixed sleep drifts about a minute an hour and steps
  # over full-cycle hours (cycle 106, 2026-09-21: 06:58 then 08:01).
  if [ "$i" -lt "$CYCLES" ]; then
    now=$(date +%s); period=$((SLEEP_MIN * 60))
    next=$(( (now / period + 1) * period + 120 ))
    sleep $((next - now))
  fi
done

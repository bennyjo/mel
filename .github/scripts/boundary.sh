#!/usr/bin/env bash
# CI boundary guard: no agent commit may touch operator-owned files.
#
# Operator commits are prefixed "operator:"; every other commit is held to
# the boundary. This mirrors the local enforcement in loop.sh, but as an
# independent, publicly visible check — the loop could be bypassed or buggy,
# CI on the pushed range cannot.
set -euo pipefail

BEFORE="${1:?usage: boundary.sh <before-sha> <after-sha>}"
AFTER="${2:?usage: boundary.sh <before-sha> <after-sha>}"

# Must stay in step with PROTECTED_PATHS/PROTECTED_RE in loop.sh. This one is
# the check that matters; loop.sh's is the local repair.
# journal/operator-notes.md is how evidence reaches the agent. The agent reads
# it and never writes it, or the operator's record of what it was told becomes
# the agent's record of what it wishes it had been told.
PROTECTED='^(core/|config/|\.github/|CYCLE\.md|CHAIN\.md|loop\.sh|CLAUDE\.md|LICENSE|README\.md|\.gitignore|journal/operator-notes\.md)'
# The experiment ledger is the agent's, but an experiment's record is only
# worth something if it cannot be rewritten after the verdict: an agent commit
# may only append to it (core/experiments.py).
APPEND_ONLY='^strategy/experiments\.jsonl$'

if [[ "$BEFORE" =~ ^0+$ ]]; then
  # Branch creation: no meaningful range; check only the head commit.
  COMMITS=$(git rev-list --no-merges -n 1 "$AFTER")
else
  COMMITS=$(git rev-list --no-merges "$BEFORE..$AFTER")
fi

fail=0
for sha in $COMMITS; do
  subject=$(git log -1 --format=%s "$sha")
  [[ "$subject" == operator:* ]] && continue
  # GitHub web-flow commits (committer = GitHub) are a human in the UI, not
  # the agent — the agents push over git and can never have this committer.
  committer=$(git log -1 --format=%ce "$sha")
  [[ "$committer" == "noreply@github.com" ]] && continue
  touched=$(git diff-tree --no-commit-id --name-only -r "$sha" | grep -E "$PROTECTED" || true)
  if [[ -n "$touched" ]]; then
    echo "::error::agent commit $sha ('$subject') touches operator-owned paths:"
    echo "$touched" | sed 's/^/    /'
    fail=1
  fi
  rewritten=$(git diff-tree --no-commit-id --numstat -r "$sha" \
    | awk -v re="$APPEND_ONLY" '$3 ~ re && $2 != "0" {print $3}')
  if [[ -n "$rewritten" ]]; then
    echo "::error::agent commit $sha ('$subject') changes or deletes lines in append-only files:"
    echo "$rewritten" | sed 's/^/    /'
    fail=1
  fi
done

if [[ "$fail" -eq 0 ]]; then
  echo "OK — no agent commit in the checked range touches operator-owned paths"
fi
exit "$fail"

#!/usr/bin/env bash
# Prints the newest commit on main that an earlier CI run already held to the
# boundary, so this run can check every commit after it.
#
# Cycle pushes skip CI and a daily run checks them in one batch, so the range
# a run must check is not the pushed range: it is everything since the last
# run whose boundary step actually ran. That step's own outcome does not
# matter. A violation fails the run that finds it, once, as it did when CI ran
# per push. A run whose job never started (a skipped cycle push, or a run
# GitHub could not start for lack of minutes) has no steps and checked
# nothing, so its commits stay in range.
#
# The base comes from the run history, which an agent commit cannot write.
set -euo pipefail

STEP="${1:?usage: checked-base.sh <boundary step name>}"
: "${GITHUB_REPOSITORY:?}" "${GITHUB_RUN_ID:?}"

others=0
for page in $(seq 1 10); do
  runs=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs?branch=main&status=completed&per_page=100&page=$page" \
    | jq -r '.workflow_runs[] | select(.event != "pull_request") | "\(.id) \(.head_sha)"')
  [[ -z "$runs" ]] && break
  while read -r id sha; do
    [[ "$id" == "$GITHUB_RUN_ID" ]] && continue
    others=$((others + 1))
    ran=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$id/jobs" \
      | STEP="$STEP" jq -r '[.jobs[].steps[]? | select(.name == env.STEP)
          | select(.conclusion == "success" or .conclusion == "failure")] | length')
    [[ "$ran" -gt 0 ]] || continue
    if git merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
      echo "$sha"
      exit 0
    fi
    # Main was rewritten past that run. An older run may still be on it.
  done <<< "$runs"
done

if [[ "$others" -eq 0 ]]; then
  # The first run on a fresh repo: there is nothing earlier to find. Print the
  # all-zero base, which boundary.sh reads as "check the head commit only", so
  # this run becomes the base every later run finds. Failing here would leave
  # the repo red until someone dispatched a manual run.
  echo "::notice::no earlier run exists; checking the head commit only" >&2
  echo "0000000000000000000000000000000000000000"
  exit 0
fi

echo "::error::no earlier run checked the boundary on a commit that is still on main" >&2
exit 1

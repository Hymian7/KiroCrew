#!/usr/bin/env bash
# Intent Lock: a PR's frozen goal (the **Goal:** line, ## Why it matters,
# ## Not a goal) is hashed into a github-actions[bot] baseline comment when the
# PR opens. Reads the PR body and comments through the API; runs no PR code.
#   evaluate  pr-readiness.yml: posts the baseline on `opened`, then writes
#             state=ok|skip|changed|missing|unreadable to $GITHUB_OUTPUT.
#   approve   intent-lock.yml: a writer's `/intent approve <head-sha>` moves
#             the baseline to the body the comment saw.
#   --hash    prints the hash of PR_BODY (tests).
set -uo pipefail

BOT='github-actions[bot]'
MARKER='<!-- intent-lock baseline='

HERE="$(cd "$(dirname "$0")" && pwd)"

# The frozen lines come from pr-description-check.sh, the parser that checks
# the same sections; trailing spaces do not count.
frozen_hash() {
  PR_BODY="$1" PR_FROZEN_LINES=1 bash "$HERE/pr-description-check.sh" \
    | sed 's/[[:space:]]*$//' | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-64
}

if [ "${1:-}" = "--hash" ]; then
  frozen_hash "${PR_BODY:-}"
  exit 0
fi

post_comment() {
  jq -n --arg body "$1" '{body: $body}' \
    | gh api --method POST "repos/$REPO/issues/$PR/comments" --input - >/dev/null
}

# Retried once.
post_baseline() {
  local b
  b="$(printf '%s%s -->\nIntent Lock baseline for the frozen goal. Changing it holds PR Readiness until a maintainer comments `/intent approve <head-sha>`.' "$MARKER" "$1")"
  post_comment "$b" || { sleep 2; post_comment "$b"; }
}


# Sets pr_head, pr_created, current and baseline (the LATEST bot baseline, empty
# when none). A failed read returns non-zero: it is never "no baseline".
read_state() {
  local pr_json
  pr_json="$(gh api "repos/$REPO/pulls/$PR")" || return 1
  pr_head="$(jq -r '.head.sha' <<<"$pr_json")"
  pr_created="$(jq -r '.created_at' <<<"$pr_json")"
  current="$(frozen_hash "$(jq -r '.body // ""' <<<"$pr_json")")"
  baseline="$(gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
    --jq ".[] | select(.user.login == \"$BOT\" and (.body | startswith(\"$MARKER\"))) | .body
          | capture(\"^<!-- intent-lock baseline=(?<h>[0-9a-f]{64}) -->\").h")" || return 1
  baseline="${baseline##*$'\n'}"
}

if [ "${1:-}" = "evaluate" ]; then
  state=unreadable
  if read_state; then
    # The body the `opened` event carried, not a later read, is the baseline.
    if [ -z "$baseline" ] && [ "${EVENT:-}" = pull_request_target ] && [ "${ACTION:-}" = opened ]; then
      b="$(frozen_hash "${EVENT_BODY:-}")"
      post_baseline "$b" && baseline="$b"
    fi
    if [ -n "$baseline" ]; then
      if [ "$baseline" = "$current" ]; then state=ok; else state=changed; fi
    # No baseline: a PR opened before this script landed is skipped; one opened
    # after had one posted, so it was deleted or never posted: fail closed.
    elif landed="$(gh api --paginate \
      "repos/$REPO/commits?path=.github/scripts/intent-lock.sh&sha=${DEFAULT_BRANCH:-main}&per_page=100" \
      --jq '.[].commit.committer.date')"; then
      if [[ "$pr_created" > "${landed##*$'\n'}" ]]; then state=missing; else state=skip; fi
    fi
  fi
  echo "pr-readiness: intent lock -- $state"
  echo "state=$state" >> "$GITHUB_OUTPUT"
  exit 0
fi

# approve
line="${COMMENT_BODY%%$'\n'*}"
[[ "${line%$'\r'}" =~ ^/intent\ approve\ ([0-9a-fA-F]{7,40})[[:space:]]*$ ]] || exit 0
requested="$(tr 'A-F' 'a-f' <<<"${BASH_REMATCH[1]}")"  # bash 3.2 has no ${v,,}
refuse() { post_comment "Intent Lock: \`/intent approve\` not accepted. $1"; exit 0; }
permission="$(gh api "repos/$REPO/collaborators/$COMMENT_USER/permission" --jq '.permission' 2>/dev/null)"
case "$permission" in
  admin|maintain|write) ;;
  *) refuse "Only a repository writer can approve a goal change (or GitHub did not confirm it: comment again)." ;;
esac
read_state || { echo "::error::could not read PR #$PR; comment the approval again"; exit 1; }
[[ "$pr_head" == "$requested"* ]] || refuse "\`$requested\` is not the current head; re-run it with \`$pr_head\`."
# The approval covers the body the comment saw; an edit since is not approved.
seen="$(frozen_hash "${EVENT_BODY:-}")"
[ "$seen" = "$current" ] || refuse "The goal changed after the comment was posted; review it and approve again."
post_baseline "$seen" || { echo "::error::could not record the baseline; comment again"; exit 1; }
# PR Readiness has no comment trigger; until it recomputes it keeps blocking.
gh workflow run pr-readiness.yml --repo "$REPO" -f pr="$PR" -f sha="$pr_head" >/dev/null \
  || { echo "::error::could not dispatch pr-readiness.yml; push or edit the PR"; exit 1; }

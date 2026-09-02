#!/usr/bin/env bash
# Installs local git hooks that enforce this repo's branch policy.
#
# WHY A LOCAL HOOK WHEN BRANCH PROTECTION ALREADY EXISTS. GitHub branch
# protection on `test` is the real gate — it rejects a direct push at the
# server. But it rejects it AFTER the push travels, and the error is easy to
# misread as a transient network problem. This hook stops the push before it
# leaves the machine, with a message that says exactly what to do instead, so
# the mistake is caught at the cheapest possible moment. It is a convenience,
# not the security boundary; the server-side rule is (see docs/DEPLOYMENT.md §1).
#
# Run once after cloning:  bash scripts/install-git-hooks.sh
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="${REPO_ROOT}/.git/hooks"
PRE_PUSH="${HOOK_DIR}/pre-push"

mkdir -p "${HOOK_DIR}"

cat > "${PRE_PUSH}" <<'HOOK'
#!/usr/bin/env bash
# Refuse a direct push to a deploy branch. Installed by scripts/install-git-hooks.sh.
#
# `test`, `elite-prod` and `general-prod` deploy on merge and move ONLY through
# pull requests. git feeds each ref being pushed on stdin as:
#   <local ref> <local sha> <remote ref> <remote sha>
set -euo pipefail

PROTECTED="refs/heads/test refs/heads/elite-prod refs/heads/general-prod"

while read -r _local_ref _local_sha remote_ref _remote_sha; do
  for p in ${PROTECTED}; do
    if [ "${remote_ref}" = "${p}" ]; then
      branch="${p#refs/heads/}"
      echo "" >&2
      echo "✋ Refusing to push directly to '${branch}'." >&2
      echo "" >&2
      echo "   '${branch}' deploys to a live VM on merge and moves only through a" >&2
      echo "   pull request. Put this work on a branch and open a PR:" >&2
      echo "" >&2
      echo "     git branch my-work            # keep the commits you just made" >&2
      echo "     git reset --hard origin/${branch}   # restore local ${branch}" >&2
      echo "     git checkout my-work          # continue, then open a PR into ${branch}" >&2
      echo "" >&2
      echo "   See CONTRIBUTING.md and docs/DEPLOYMENT.md §1." >&2
      echo "   (Override for a genuine exception: git push --no-verify — think first.)" >&2
      echo "" >&2
      exit 1
    fi
  done
done

exit 0
HOOK

chmod +x "${PRE_PUSH}"
echo "Installed pre-push hook at ${PRE_PUSH}"
echo "It refuses direct pushes to test / elite-prod / general-prod."

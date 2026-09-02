# Contributing

> ## 🔴 The one rule: branch from `test`, PR into `test`. Never push to `test` directly.
>
> ```bash
> git checkout test && git pull origin test
> git checkout -b feat/your-feature        # ← always work on a branch
> ```
>
> **`test` is deploy-on-merge, so a commit pushed straight to it ships to the
> live test VM with no review and no second pair of eyes.** Every change reaches
> `test` through a pull request — yours or anyone's, one line or a thousand,
> code or docs. There is no "small enough to push directly" exception: the
> defects that have actually bitten this project were the ones no diff showed
> (see [docs/DEPLOYMENT.md §5](docs/DEPLOYMENT.md#5-failures-this-pipeline-has-actually-produced)),
> and a PR is the only place they get a chance to be caught before customers meet
> them.
>
> If you have already committed onto a local `test`, do not push it — move the
> commits onto a branch and open a PR:
>
> ```bash
> git branch feat/your-feature            # name the work you just did
> git reset --hard origin/test            # put local test back to the remote
> git checkout feat/your-feature          # continue on the branch, then PR
> ```
>
> `test` is the only continuously deployed branch, which makes it the only one
> whose code has been seen *running*. A branch cut from anywhere else is cut
> from something nobody has observed working.
>
> **Enforcement.** GitHub branch protection on `test` rejects a direct push (see
> [docs/DEPLOYMENT.md §1](docs/DEPLOYMENT.md#1-start-from-test-always)); the PR
> template asks you to confirm the base; and `scripts/install-git-hooks.sh`
> installs a local `pre-push` hook that stops the push before it leaves your
> machine.

## Branches

| Branch         | Purpose                              | Deploys to    |
| -------------- | ------------------------------------ | ------------- |
| `test`         | Default branch. All work starts here | Test VM       |
| `elite-prod`   | Elite production release line        | Elite prod VM |
| `general-prod` | General production release line      | Gen prod VM   |

`test` is the only branch you branch from, and the only branch you open pull
requests against. You never commit or push to `test` itself — it moves only
when a pull request merges. The two production branches are release targets, not
places to develop.

## Developer workflow

1. Start from an up-to-date `test`:

   ```bash
   git checkout test
   git pull origin test
   ```

2. Create your working branch. Use a `type/short-description` name, e.g.
   `feat/ticket-intake`, `fix/session-timeout`, `chore/bump-deps`:

   ```bash
   git checkout -b feat/your-feature
   ```

3. Commit and push:

   ```bash
   git push -u origin feat/your-feature
   ```

4. Open a pull request **into `test`**. It is the default base, so you should
   not have to change it — if the base shows anything else, correct it before
   requesting review. **Never `git push origin test` to land the work directly**
   — branch protection will reject it, and even if it did not, it would ship to
   the live VM unreviewed.

5. CI must pass and the PR needs an approval before it can merge.

6. On merge, `test` deploys automatically to the test VM — see
   [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). CI and Deploy are **independent**:
   a red CI does not stop a deploy, so check both.

   Two traps worth knowing before you trust a green CI tick:

   - **Lint runs before tests.** A lint failure means the suite never ran.
   - **A change touching neither `backend/**` nor `frontend/**` skips both jobs
     and reports green.** A commit to `scripts/` or `docs/` gets a ✅ that means
     nothing was checked.

Never open a pull request from a feature branch straight into `elite-prod` or
`general-prod`. Production only ever receives code that has already landed in
`test` and been verified there.

## Promotion to production

Once QA has verified the change on the test VM, one of the release approvers
opens a promotion pull request from `test` into `elite-prod` or `general-prod`.

Promotion PRs require review from a code owner. The approvers are listed in
`.github/CODEOWNERS` on each production branch. Merging a promotion PR deploys
to that production VM immediately — there is no second confirmation step, so
confirm QA sign-off before you merge.

## Keeping a long-running branch current

Rebase onto `test` rather than merging `test` into your branch, so history
stays linear:

```bash
git fetch origin
git rebase origin/test
```

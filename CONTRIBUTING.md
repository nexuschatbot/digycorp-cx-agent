# Contributing

## Branches

| Branch         | Purpose                              | Deploys to    |
| -------------- | ------------------------------------ | ------------- |
| `test`         | Default branch. All work starts here | Test VM       |
| `elite-prod`   | Elite production release line        | Elite prod VM |
| `general-prod` | General production release line      | Gen prod VM   |

`test` is the only branch you branch from, and the only branch you open pull
requests against. The two production branches are release targets, not places
to develop.

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
   requesting review.

5. CI must pass and the PR needs an approval before it can merge.

6. On merge, `test` deploys automatically to the test VM.

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

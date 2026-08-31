# Deployment

How code gets from your machine to a running server, what each gate checks, and
what has already gone wrong so nobody rediscovers it.

Environment specifics — VM size, SSH, schedules, secrets — are in
[INFRASTRUCTURE.md](INFRASTRUCTURE.md). This document is the *pipeline*.

---

## 1. Start from `test`. Always.

> ### 🔴 `test` is the base branch for **all** work.
>
> Branch **from** `test`. Open your PR **into** `test`. Nothing else.
>
> ```bash
> git checkout test
> git pull origin test
> git checkout -b fix/your-change      # or feat/…, chore/…
> ```
>
> `elite-prod` and `general-prod` are **release targets, not places to
> develop**. They only ever receive a promotion PR from `test`, and
> `.github/workflows/branch-policy.yml` fails any PR into them from anywhere
> else.

**Why this is a rule and not a preference.** `test` is the only branch that is
continuously deployed, so it is the only branch whose code has been *seen
running*. A branch cut from anywhere else is cut from something nobody has
observed working. Every defect in §5 below was invisible until a real deploy
ran; none of them could have been caught by reading a diff.

**Nothing enforces the *base* automatically** — `branch-policy.yml` guards the
production branches, but no check verifies that your feature branch was cut from
`test`. The PR template asks you to confirm it. Please actually confirm it.

---

## 2. What happens when you merge

Merging into `test` fires two independent workflows. **Neither waits for the
other** — a red CI does not stop a deploy.

```
merge to test
   ├── CI       (.github/workflows/ci.yml)      lint, then tests
   └── Deploy   (.github/workflows/deploy.yml)  build, ship, restart, verify
```

### CI

| job | runs when | does |
|---|---|---|
| `changes` | always | decides which of the two below run, by path |
| `backend` | `backend/**` or `.github/workflows/**` changed | `ruff check .` **then** `pytest -q` |
| `frontend` | `frontend/**` or `.github/workflows/**` changed | `tsc --noEmit`, `vite build` |

Two things about this are worth knowing before you trust a green tick:

- **Lint runs before tests.** A lint error means the suite never ran. It is not
  "tests passed with a style nit".
- **A change touching neither path skips both jobs and reports green.** A commit
  to `scripts/` or `docs/` gets a ✅ that means *nothing was checked*. Latent
  test failures stay hidden until someone next touches `backend/`.

### Deploy

```
checkout (with LFS)  →  build frontend  →  rsync to the VM
    →  ssh: scripts/remote-deploy.sh    →  external health check
```

`remote-deploy.sh` runs on the VM and is where the real work happens:

1. **Ensure Python 3.12.** Installs it from deadsnakes if absent. The box is
   Ubuntu 22.04, whose `python3` is 3.10, and 3.10 cannot resolve
   `onnxruntime==1.28.0` at all.
2. **Rebuild the venv if its interpreter is wrong** — not merely if it is
   missing. A stale venv is the failure that repeats identically forever.
3. `pip install -r requirements.txt`
4. **Install the nginx config** from `deploy/nginx/`, adding the TLS server
   block only when a certificate exists.
5. `nginx -t`, and reload **only if it passes** — a bad config leaves the
   running server running.
6. Restart `digycorp-api`, then poll `/health` for up to **120 s**.

---

## 3. What the server owns, and what the deploy must never touch

The rsync uses `--delete`, so anything on the server that is not in the repo is
deleted unless explicitly excluded. Three things are the **server's**, are
gitignored, and are excluded in `deploy.yml`:

| path | what it is | why losing it hurts |
|---|---|---|
| `backend/.env` | every secret the app has | the service starts and cannot answer |
| `backend/elite_demo.db` | the database | customer data, gone |
| `backend/agent_checkpoints.db` | live conversation state | every in-flight chat, gone |

**If you add another server-owned file, add its exclude in the same commit.**

### Putting the `.env` on a server

Over SSH, from your own machine. Never through `az vm run-command` — that
transmits the script body to the Azure control plane, where it is retained and
readable by anyone with Reader on the subscription.

```bash
scp -i ~/.ssh/digycorp-cx-test_ed25519 backend/.env \
    azureuser@digycorp-cx-test.uaenorth.cloudapp.azure.com:/opt/digycorp-cx-agent/backend/.env
ssh -i ~/.ssh/digycorp-cx-test_ed25519 \
    azureuser@digycorp-cx-test.uaenorth.cloudapp.azure.com 'sudo systemctl restart digycorp-api'
```

The restart is required: `load_dotenv()` runs at import.

**Do not copy a local `.env` verbatim.** At least these differ on a server:

| variable | why |
|---|---|
| `PUBLIC_WEB_BASE_URL` | goes into WhatsApp messages **sent to customers**. A stale tunnel URL ships broken links |
| `REDIS_URL` | Redis is not installed on the test VM. Retrieval falls back to an in-process index and says so |
| `WHATSAPP_ENABLED` | `true` sends real messages to real numbers |
| `PREWARM_PROMPT_CACHE` | `true` costs 8 live API calls per boot — i.e. per deploy |

### Seeding a fresh database

Order matters; it follows the foreign keys.

```bash
cd /opt/digycorp-cx-agent/backend
PY=/opt/digycorp-cx-agent/venv/bin/python
for m in seed_showrooms seed_vehicle_models seed_stock seed_finance_banks \
         seed_accessories seed_customers seed_memory_profiles; do
  $PY -m seed.$m
done
$PY -m app.cli generate-slots
```

Expect 5 showrooms, 9 models, 15 variants, 330 stock rows (7,544 units),
3 finance banks, 5 accessories.

---

## 4. TLS

Live on the test VM. Let's Encrypt, obtained with:

```bash
certbot certonly --webroot -w /var/www/html -d <hostname>
```

**`certonly`, deliberately** — certbot never edits nginx, so `deploy/nginx/`
stays the only description of the server. Renewal is certbot's own systemd
timer and needs nothing from the deploy except that the
`/.well-known/acme-challenge/` location in `site-http.conf` keeps answering on
plain **HTTP**.

`site-tls.conf` is a separate file installed only when a certificate exists,
because nginx will not start with an `ssl_certificate` that is missing and
certbot's HTTP-01 challenge needs nginx already up. A fresh box therefore comes
up on HTTP, gets its certificate through that, and gains HTTPS on the next
deploy.

**TLS is not cosmetic here.** Three browser APIs the app uses are
secure-context-only: `crypto.randomUUID` (was fatal — see §5),
`getUserMedia` (voice), `geolocation` (nearest showroom). The last two simply
do not work over plain HTTP.

Two things deliberately **not** enabled:

- **No HTTP→HTTPS redirect.** Both health gates compare against a literal `200`
  without following redirects, so a `301` fails the deploy on a healthy server.
  Turn it on only together with `-L` in both gates.
- **No HSTS.** Browsers remember it for its `max-age` and it cannot be withdrawn
  from the server side.

---

## 5. Failures this pipeline has actually produced

Every one shipped green somewhere, or failed while the server was fine. They are
here because the diff never showed any of them.

| symptom | cause | fix |
|---|---|---|
| Deploy retried 15× then `exit 1` on a healthy service | the app served `/api/health`; the script polled `/health` — the scaffold's route survived the port, the route did not | serve `/health`, `/` and `/api/health`; a test now reads the polled paths **out of the deploy scripts** |
| CI red for weeks, tests never ran | `ruff check .` found 278 findings, 251 in hand-run `salesforce-setup/` scripts | excluded those; fixed the other 27; **no rule selected or ignored** |
| A 118 MB model shipped as a 134-byte file | `actions/checkout` without `lfs: true`, in **both** CI and deploy | `lfs: true` on the two jobs that read `backend/` |
| — (never fired) | `rsync --delete` would have deleted `.env` and both databases | excludes, above |
| `onnxruntime==1.28.0` unresolvable | VM Python 3.10; app needs 3.12 | install 3.12, rebuild the venv when its interpreter is wrong |
| Deploy **succeeded** and took the site off HTTPS | `[ -f /etc/letsencrypt/live/…/fullchain.pem ]` is false for the deploy user — certbot makes those dirs `0700 root` | `sudo test -f` |
| Blank page, 200, healthy backend | `crypto.randomUUID` is secure-context-only; it threw at first render. Works on `localhost`, so no dev machine can reproduce it | `newId()` fallback via `getRandomValues` |
| Every `/api` route 404 through nginx | `proxy_pass http://127.0.0.1:8000/` — the trailing slash strips the matched prefix | nginx config moved into `deploy/nginx/` and deployed |
| — (would have) | 40 s boot vs a 30 s health wait, once `.env` enabled prewarm + retrieval | 120 s, and it reports how long it took |

**The pattern.** Almost none of these were broken code. They were two correct
things disagreeing — a script and a route, a config and an app, a guard and a
file it could not read. That is what a deploy tests and a test suite cannot.

**And note the shape of the two worst ones.** The certificate check and the
health timeout are both *false negatives in a guard*: one could not see
something that was there, the other gave up before something that was coming.
Both went green. A guard that fails safe still has to be able to see.

---

## 6. Checking a deploy

```bash
gh run list --workflow=deploy.yml --branch=test
gh run view <id> --log | grep -E "Certificate present|API healthy|complete"
```

A healthy run says:

```
==> Reusing virtualenv (Python 3.12.13)
==> Installing nginx config
==> Certificate present — enabling HTTPS
==> API healthy after ~28s
==> Deploy to test complete
```

`No certificate yet — HTTP only` on a box that was serving HTTPS means §5's
certificate bug has returned. Check it before assuming the site is fine — the
deploy will still be green.

Then confirm from outside, because the gates only prove the app answers on
localhost:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://digycorp-cx-test.uaenorth.cloudapp.azure.com/health
curl -s -o /dev/null -w '%{http_code}\n' https://digycorp-cx-test.uaenorth.cloudapp.azure.com/api/vehicles/1
```

And **open the page in a browser.** A 200 from `curl` proved nothing about the
blank page in §5: the HTML, the bundle and the backend were all fine, and the
app was still unusable.

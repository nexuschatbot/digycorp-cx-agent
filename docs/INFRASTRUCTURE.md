# Infrastructure

Everything about the environments and how to operate them.

## Quick reference

| Need                     | Command / URL                                                     |
| ------------------------ | ----------------------------------------------------------------- |
| **Start the test VM**    | `az vm start -g digycorp-cx-test-rg -n digycorp-cx-test-vm`        |
| Stop the test VM         | `az vm deallocate -g digycorp-cx-test-rg -n digycorp-cx-test-vm`   |
| Is it running?           | `az vm get-instance-view -g digycorp-cx-test-rg -n digycorp-cx-test-vm --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus" -o tsv` |
| SSH in                   | `ssh digycorp-cx-test` (or `ssh -i ~/.ssh/digycorp-cx-test_ed25519 azureuser@digycorp-cx-test.uaenorth.cloudapp.azure.com`) |
| Open the app             | **https://**digycorp-cx-test.uaenorth.cloudapp.azure.com           |
| Deploy status            | `gh run list --workflow=deploy.yml --branch=test`                  |

> **The test VM is switched off overnight.** If the site does not load or SSH
> times out, it is almost certainly deallocated — start it with the command
> above rather than debugging the app.
>
> ⚠️ **Auto-shutdown is currently DISABLED** (31 Aug 2026, on request), so the
> VM stays up and billing. Re-enable it with:
>
> ```bash
> az resource update -g DIGYCORP-CX-TEST-RG -n shutdown-computevm-digycorp-cx-test-vm \
>   --resource-type Microsoft.DevTestLab/schedules --set properties.status=Enabled
> ```
>
> 💸 **`PREWARM_PROMPT_CACHE` is `false` on this VM** (1 Sep 2026) and should
> stay false: every restart with it on writes the full set of prompt prefixes
> at the 1h-TTL cache-write rate — measured at ~$0.55–0.90 **per restart**,
> which on a box that redeploys several times a day out-spent all conversations
> combined. Turn it on only for a demo day. See
> [DEPLOYMENT.md](DEPLOYMENT.md) §"Do not copy a local .env verbatim".

## Test environment

| | |
| --- | --- |
| Subscription   | Testing (`b11e83fc-8d89-4898-8550-bcb256f0dea5`) |
| Resource group | `digycorp-cx-test-rg` |
| Region         | `uaenorth` |
| VM             | `digycorp-cx-test-vm`, Standard_D2als_v7 (2 vCPU / 4 GB) |
| OS             | Ubuntu 22.04 LTS — **`python3` is 3.10; the app needs 3.12**, installed from deadsnakes by `scripts/remote-deploy.sh` |
| Hostname       | `digycorp-cx-test.uaenorth.cloudapp.azure.com` |
| Public IP      | `20.74.254.182` (static) |
| SSH user       | `azureuser` |
| Deploy path    | `/opt/digycorp-cx-agent` |

`Standard_B2s` was the intended size but is capacity-restricted in UAE North,
and the v2 B-series has zero quota on this subscription. `D2als_v7` is the
cheapest family with both quota and capacity in the region.

### On the VM

```
/opt/digycorp-cx-agent/
├── backend/        FastAPI source, rsynced from the repo
│   ├── .env             SERVER-OWNED. Excluded from rsync --delete; place it
│   │                    over scp, never via `az vm run-command`
│   ├── elite_demo.db    SERVER-OWNED. The database — excluded likewise
│   └── agent_checkpoints.db  SERVER-OWNED. Live conversation state
├── deploy/nginx/   the nginx config, deployed from the repo
├── frontend/dist/  built React bundle, served by nginx
└── venv/           Python 3.12 — deliberately outside the rsync target,
                    rebuilt automatically if its interpreter is wrong
```

**The three SERVER-OWNED files are gitignored, so `rsync --delete` would remove
them.** They are excluded in `deploy.yml`; if you add a fourth, add its exclude
in the same commit. See [DEPLOYMENT.md §3](DEPLOYMENT.md#3-what-the-server-owns-and-what-the-deploy-must-never-touch).

| Service | Role |
| ------- | ---- |
| `digycorp-api` | uvicorn on `127.0.0.1:8000` |
| `nginx`        | serves `frontend/dist`, proxies `/api`, `/media` and the two WebSockets. Config is **deployed from `deploy/nginx/`**, not hand-edited |
| `certbot.timer`| renews the Let's Encrypt certificate |

> **Do not hand-edit `/etc/nginx/sites-available/digycorp-cx-agent`.** It is
> overwritten on every deploy. It used to be hand-maintained and drifted from
> the application without a diff anywhere to show it —
> [DEPLOYMENT.md §5](DEPLOYMENT.md#5-failures-this-pipeline-has-actually-produced).

```bash
sudo systemctl status digycorp-api
sudo journalctl -u digycorp-api -f
sudo tail -f /var/log/nginx/error.log
```

## Schedules (Pakistan time)

| When     | What              | How |
| -------- | ----------------- | --- |
| 20:00    | VM shuts down     | Azure native auto-shutdown |
| 10:00    | VM starts up      | Automation runbook `Start-TestVM` in `digycorp-cx-automation` |

Auto-shutdown is free and native. Azure has no native scheduled *start*, so the
morning start runs as a PowerShell runbook on a daily schedule.

> **Known gap:** the runbook authenticates with the Automation Account's
> system-assigned managed identity (principal `cf6855a3-a731-457c-b13d-3025d50ae55e`),
> which still needs the **Virtual Machine Contributor** role on
> `digycorp-cx-test-rg`. Creating a role assignment requires Owner or User Access
> Administrator — the project account only has Contributor. Until an admin runs
> the command below, the VM will **not** start on its own each morning.
>
> ```bash
> az role assignment create \
>   --assignee-object-id cf6855a3-a731-457c-b13d-3025d50ae55e \
>   --assignee-principal-type ServicePrincipal \
>   --role "Virtual Machine Contributor" \
>   --scope /subscriptions/b11e83fc-8d89-4898-8550-bcb256f0dea5/resourceGroups/digycorp-cx-test-rg
> ```

## Deployment secrets

Set per environment as repository secrets; see [../README.md](../README.md).
`TEST_*` are configured. `ELITE_PROD_*` and `GENERAL_PROD_*` are not yet set, so
production deploys fail fast at the pre-flight check by design.

## TLS

Live since 31 Aug 2026. Let's Encrypt, `certbot certonly --webroot`, expires
**29 Nov 2026**, renewed by certbot's own systemd timer.

`certonly` deliberately — certbot never edits nginx, so `deploy/nginx/` stays
the only description of the server. Renewal needs only that the
`/.well-known/acme-challenge/` location keeps answering on plain HTTP.

Registered **without an email**, so there are no expiry notices. Add one with
`sudo certbot update_account -m <address>` if you want them.

No HTTP→HTTPS redirect and no HSTS, both deliberate —
[DEPLOYMENT.md §4](DEPLOYMENT.md#4-tls).

## Not done yet

- **No production VMs.** Only the test environment exists.
- **No Postgres.** The app runs on SQLite at
  `backend/elite_demo.db`, seeded on the box. The Alembic step in
  `scripts/remote-deploy.sh` stays commented out because `main.py` migrates at
  boot. S8a in [MIGRATION.md](MIGRATION.md) is the move to Postgres.
- **No Redis.** `REDIS_URL` in `.env` points at a `localhost` that is not
  there; retrieval logs `retrieval cache unavailable — the index will be built
  in process` and carries on. Costs ~9s of every boot.
- **The morning auto-start is still broken** — see the known gap above. It is
  masked right now only because auto-shutdown is disabled.

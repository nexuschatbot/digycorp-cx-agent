# Infrastructure

Everything about the environments and how to operate them.

## Quick reference

| Need                     | Command / URL                                                     |
| ------------------------ | ----------------------------------------------------------------- |
| **Start the test VM**    | `az vm start -g digycorp-cx-test-rg -n digycorp-cx-test-vm`        |
| Stop the test VM         | `az vm deallocate -g digycorp-cx-test-rg -n digycorp-cx-test-vm`   |
| Is it running?           | `az vm get-instance-view -g digycorp-cx-test-rg -n digycorp-cx-test-vm --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus" -o tsv` |
| SSH in                   | `ssh digycorp-cx-test` (or `ssh -i ~/.ssh/digycorp-cx-test_ed25519 azureuser@digycorp-cx-test.uaenorth.cloudapp.azure.com`) |
| Open the app             | http://digycorp-cx-test.uaenorth.cloudapp.azure.com                |
| Deploy status            | `gh run list --workflow=deploy.yml --branch=test`                  |

> **The test VM is switched off overnight.** If the site does not load or SSH
> times out, it is almost certainly deallocated — start it with the command
> above rather than debugging the app.

## Test environment

| | |
| --- | --- |
| Subscription   | Testing (`b11e83fc-8d89-4898-8550-bcb256f0dea5`) |
| Resource group | `digycorp-cx-test-rg` |
| Region         | `uaenorth` |
| VM             | `digycorp-cx-test-vm`, Standard_D2als_v7 (2 vCPU / 4 GB) |
| OS             | Ubuntu 22.04 LTS |
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
│   └── .env        ENVIRONMENT=test
├── frontend/dist/  built React bundle, served by nginx
└── venv/           Python env — deliberately outside the rsync target
```

| Service | Role |
| ------- | ---- |
| `digycorp-api` | uvicorn on `127.0.0.1:8000` |
| `nginx`        | serves `frontend/dist`, proxies `/api` and `/health` to uvicorn — **but uvicorn 404s `/health`; it serves `/api/health`. See [OPEN-ISSUES.md #6](OPEN-ISSUES.md#6-health-404s-so-every-deploy-fails-its-health-gate)** |

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

## Not done yet

- **No TLS.** The site is plain HTTP. Needs a real domain plus certbot before
  anything non-trivial goes on it.
- **No production VMs.** Only the test environment exists.
- **No database.** To be decided; `backend/.env` and the migration step in
  `scripts/remote-deploy.sh` are stubbed for it.

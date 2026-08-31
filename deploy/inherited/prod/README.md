# Production environment — the live client demo

Everything here targets `elite-demo-vm` in `elite-demo-rg`. **Every deploy hits
the live demo**; the script asks you to type `prod` before anything runs.

```bash
./deploy/prod/deploy.sh                 # confirm, then sync + rebuild + restart
./deploy/prod/deploy.sh --yes           # skip the prompt (scripted use)
./deploy/prod/deploy.sh --reset-demo    # ...then the demo-morning ritual (destructive)
./deploy/prod/deploy.sh --logs          # ...then tail logs
```

- URLs: **https://elite-group.digycorp.com** (QR target) with
  `https://elite-demo.uaenorth.cloudapp.azure.com` served alongside as the
  fallback name. After every deploy, check BOTH health lines in the output —
  a deploy once silently dropped the custom domain's TLS.
- Env file shipped to the VM: `deploy/prod/.env` — WhatsApp live both
  directions (token is temporary; swap in a permanent System User token before
  demo day), LangSmith full-text tracing (**uncomment the two
  `LANGSMITH_HIDE_*` lines before the QR goes on screen**).
- Compose overlay: `deploy/prod/docker-compose.yml`.

Demo morning:

```bash
./deploy/prod/deploy.sh --reset-demo
```

Runs `python -m app.cli reset-demo` inside the backend container against
Postgres: drops and re-seeds, regenerates the rolling slot window, recreates the
demo booking through the real write guards, and clears the agent checkpoint
store. Destructive — hence a flag, never the default.

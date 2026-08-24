# Setup — Elite Test Drive Booking backend (Phase 0)

Python 3.11+ required.

## 1. Create a virtualenv and install deps

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure environment

```bash
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY
```

Defaults in `.env.example`:

| Variable                | Default                     | Meaning                                  |
| ----------------------- | --------------------------- | ---------------------------------------- |
| `ANTHROPIC_API_KEY`     | *(empty)*                   | Claude API key — server-side only        |
| `DATABASE_URL`          | `sqlite:///./elite_demo.db` | Dev database                             |
| `CLAUDE_MODEL`          | `claude-sonnet-4-6`         | Agent model                              |
| `SLOT_LOCK_TTL_MINUTES` | `10`                        | Slot-lock time-to-live                   |
| `SLOT_HORIZON_DAYS`     | `14`                        | Rolling days of slots offered            |
| `OPENAI_API_KEY`        | *(empty)*                   | Speech-to-text for voice notes — **optional**, see below |
| `STT_MODEL`             | `gpt-4o-transcribe`         | Transcription model                      |
| `VOICE_MAX_SECONDS`     | `60`                        | Recording length cap                     |
| `VOICE_RETENTION_HOURS` | `48`                        | How long recordings are kept             |

### Voice notes (optional)

Customers can record a voice note instead of typing; it is transcribed and enters
the conversation as an ordinary turn. **Aisha always replies in text** — there is
no TTS and no voice channel (see the 2026-07-21 scope override in `CLAUDE.md`).

**Leave `OPENAI_API_KEY` blank and everything else works exactly as before.** The
app boots, the text demo runs, and the transcribe endpoint answers
`{"ok": false, "reason": "stt_not_configured"}` so the UI offers typing instead.
Voice is additive; it is never a startup requirement.

Two things to know before enabling it:

- ⚠️ **The microphone needs HTTPS.** `getUserMedia` is blocked on plain HTTP
  everywhere except `localhost`. Local dev is fine, but if attendees reach the
  app at `http://<your-ip>:5173` the mic button will show
  "Recording needs a secure (https) connection" on every phone, and no code
  change fixes it. Cheapest workaround for a demo:
  `cloudflared tunnel --url http://localhost:5173`.
- ⚠️ **PDPL:** with this set, customer audio leaves the UAE to a US endpoint —
  the only place in this system that sends customer data out of region. On the
  D1 checklist. Recordings are hard-deleted by "forget me" (rule 12) and expire
  after `VOICE_RETENTION_HOURS` regardless.

### Read-aloud (same key)

A speaker icon on each of Aisha's replies reads that message in the language it
is written in. It uses the SAME `OPENAI_API_KEY`; with it unset, playback falls
back to the browser's own voice, so the control keeps working and only sounds
worse.

⚠️ **This is the heaviest PDPL surface in the system and gets its own D1 entry.**
Voice notes sent the customer's own audio; this sends Aisha's replies, which
quote the customer back to themselves — name, mobile and booking reference —
outside the UAE. Clips are scoped to one session, expire with
`VOICE_RETENTION_HOURS`, and are hard-deleted by "forget me".

Recordings are purged automatically as new ones arrive; for a box left idle for
days, `python -m app.cli purge-voice` sweeps whatever has aged out.

## 3. Run the API (SQLite, dev default)

```bash
uvicorn app.main:app --reload --port 8000
```

Tables are created automatically on startup. Verify:

```bash
curl http://localhost:8000/api/health
# {"api":"ok","database":"ok"}
```

The SQLite file `elite_demo.db` appears in the project root (gitignored).

## 4. Run tests

```bash
pytest            # or: python -m pytest
```

The calendar tests prove S-22: no generated slot can overlap the Friday prayer
window (~11:30–13:30).

## 5. Switch to Postgres (no code changes)

Point `DATABASE_URL` at Postgres and restart — the engine drops the
SQLite-only `check_same_thread` arg automatically. Example:

```bash
# requires a Postgres driver, e.g.  pip install "psycopg[binary]"
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/elite"
uvicorn app.main:app --port 8000
```

Startup will `create_all` the same schema against Postgres. Enum-like fields
become native Postgres ENUM types; JSON list fields map to `JSON`.

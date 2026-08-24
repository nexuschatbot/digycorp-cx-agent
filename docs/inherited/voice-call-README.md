# Voice-call feature — parked 2026-07-28

The feature is COMPLETE and PUSHED on branch `voice-call` in both repos
(backend 1bd8445, frontend df2793a). The working trees and these parent-dir
files were reverted to main so main-branch bug fixing starts clean.

## To resume the feature

1. `git checkout voice-call` in elite-group-backend AND elite-group-frontend.
2. Restore the parent files (they are in NO git repo):
       cp voice-call-parent-files/CLAUDE.voice-call.md CLAUDE.md
       cp voice-call-parent-files/docker-compose.voice-call.yml docker-compose.yml
3. Append `env-voice-call-lines.txt` to elite-group-backend/.env
   (CALL_ENABLED, CALL_VAD_SILENCE_MS=500, CALL_AGENT_MODEL=claude-haiku-4-5,
   CALL_MODE=realtime). PREWARM_PROMPT_CACHE stays false for dev either way.
4. Restart uvicorn; `npm run build` or dev server picks the branch up as-is.

## Where the feature stood when parked

- realtime engine (speech-to-speech) working end to end; last round added the
  uplink noise gate + eased barge threshold — NOT yet confirmed by a live call.
- Room-tuning knobs: NOISE_GATE_RMS / BARGE_OVER_ECHO in src/useCall.ts.
- Outstanding: one clean full booking on the realtime engine; merge decision
  between engines; Arabic deferred; do-not-deploy stands.

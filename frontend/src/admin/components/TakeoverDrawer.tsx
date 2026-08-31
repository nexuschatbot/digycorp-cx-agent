// TakeoverDrawer — the live chat behind the Handoffs queue's "Take over" button
// (human takeover, 2026-08-31).
//
// WHAT IT IS. The moment an operator takes a session over, the AI is out of it
// for good (the backend's runner gate refuses every later agent turn) and this
// drawer IS the conversation: full transcript above so the operator can read
// what happened before they arrived, live messages below, one exit — "End &
// close chat", which runs the same close service as every other ending.
//
// THE NAME. There is no admin auth in this demo (the same D1 relaxation as the
// rest of the console), so the operator types a display name once and the
// browser remembers it. The backend stamps it on the takeover audit row and the
// customer sees it on every staff bubble — a person has a name; "support" does
// not.
//
// FIRST ADMIN WINS, and this UI wears the result honestly: a session someone
// else holds says who, and trying anyway is answered by the server's
// already_taken refusal, shown as text rather than smuggled into a disabled
// button nobody can interrogate.

import { useEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Drawer from '@mui/material/Drawer'
import IconButton from '@mui/material/IconButton'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import SendRoundedIcon from '@mui/icons-material/SendRounded'

import type { HandoffRow } from '../adminApi'
import { Flex } from './ui'

// The operator's remembered display name. localStorage, per-browser: the admin
// console is a desk, not a per-tab conversation, so the sessionStorage
// reasoning from the customer chat does not apply here.
const NAME_KEY = 'elite:admin-name'

interface Line {
  id: string
  speaker: 'customer' | 'agent' | 'system' | 'staff'
  text: string
  lang: string
  name?: string
}

type Phase = 'reading' | 'ready' | 'live' | 'ended' | 'refused'

const REFUSALS: Record<string, string> = {
  already_taken: 'Someone else already holds this conversation.',
  conversation_closed: 'This conversation has already closed — the callback queue is the way to reach them now.',
  not_escalated: 'This session has no escalation to take over.',
  admin_name_required: 'Enter your name first — the customer sees who joined.',
}

export default function TakeoverDrawer({
  row,
  onClose,
  onChanged,
}: {
  row: HandoffRow
  onClose: () => void
  /** The queue behind this drawer changed (a takeover started or the chat was
   *  closed) — reload it so the row wears its new state. */
  onChanged: () => void
}) {
  const [name, setName] = useState(() => {
    try {
      return localStorage.getItem(NAME_KEY) ?? ''
    } catch {
      return ''
    }
  })
  const [phase, setPhase] = useState<Phase>('reading')
  const [refusal, setRefusal] = useState<string | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [draft, setDraft] = useState('')
  const wsRef = useRef<WebSocket | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const holder = row.taken_over_by

  // The transcript, read before anything is touched: the whole point of the
  // takeover is that the operator arrives knowing what has been said. Read
  // through the same GET the customer's reload uses — one transcript, one shape.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch(`/api/conversation/${encodeURIComponent(row.session)}`)
        const convo = res.ok ? await res.json() : null
        if (cancelled) return
        const staffName = convo?.takeover?.admin_name ?? undefined
        setLines(
          (convo?.turns ?? [])
            .filter((t: { text: string }) => t.text?.trim())
            .map((t: { speaker: string; text: string; lang: string }) => ({
              id: crypto.randomUUID(),
              speaker: t.speaker,
              text: t.text,
              lang: t.lang,
              ...(t.speaker === 'staff' && staffName ? { name: staffName } : {}),
            })),
        )
        setPhase(convo?.status === 'closed' && !convo?.takeover?.taken_over ? 'refused' : 'ready')
        if (convo?.status === 'closed' && !convo?.takeover?.taken_over) {
          setRefusal(REFUSALS.conversation_closed)
        }
      } catch {
        if (!cancelled) setPhase('ready') // the socket will say what the GET could not
      }
    })()
    return () => {
      cancelled = true
    }
  }, [row.session])

  // New lines keep the newest message in view.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [lines])

  // The socket dies with the drawer — but the takeover does NOT: the state is
  // server-side, so closing this panel and reopening it rejoins the same hold.
  useEffect(
    () => () => {
      try {
        wsRef.current?.close()
      } catch {
        // already dead
      }
    },
    [],
  )

  const takeOver = () => {
    if (wsRef.current) return // a connect is already in flight
    const trimmed = name.trim()
    if (!trimmed) {
      setRefusal(REFUSALS.admin_name_required)
      return
    }
    try {
      localStorage.setItem(NAME_KEY, trimmed)
    } catch {
      // remembering the name is a convenience, not a requirement
    }
    setRefusal(null)
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(
      `${proto}//${window.location.host}/api/takeover/${encodeURIComponent(row.session)}` +
        `?role=admin&name=${encodeURIComponent(trimmed)}`,
    )
    wsRef.current = ws
    ws.onmessage = (ev) => {
      let frame: { type?: string; [k: string]: unknown }
      try {
        frame = JSON.parse(ev.data as string)
      } catch {
        return
      }
      if (frame.type === 'state') {
        setPhase('live')
        onChanged() // the row now wears "held by <name>"
      } else if (frame.type === 'message') {
        // The other side (the sender's own lines are appended locally on send).
        setLines((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            speaker: (frame.speaker as Line['speaker']) ?? 'customer',
            text: (frame.text as string) ?? '',
            lang: (frame.lang as string) ?? 'en',
            ...(frame.admin_name ? { name: frame.admin_name as string } : {}),
          },
        ])
      } else if (frame.type === 'closed') {
        setPhase('ended')
        onChanged()
      } else if (frame.type === 'error') {
        const code = (frame.error as string) ?? 'error'
        setRefusal(REFUSALS[code] ?? code)
        if (code === 'already_taken' || code === 'conversation_closed' || code === 'not_escalated') {
          setPhase('refused')
        }
      }
    }
    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null
    }
  }

  const send = () => {
    const text = draft.trim()
    const ws = wsRef.current
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'message', text }))
    setLines((prev) => [
      ...prev,
      { id: crypto.randomUUID(), speaker: 'staff', text, lang: 'en', name: name.trim() },
    ])
    setDraft('')
  }

  const endAndClose = () => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'close' }))
  }

  return (
    <Drawer
      anchor="right"
      open
      onClose={onClose}
      slotProps={{ paper: { sx: { width: { xs: '100%', sm: 560 }, display: 'flex' } } }}
    >
      <Box sx={{ p: 2.5, borderBottom: '1px solid', borderColor: 'divider', bgcolor: '#fafbfc' }}>
        <Flex direction="row" align="center" justify="space-between">
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" sx={{ fontSize: 16 }}>
              {row.customer_name ?? 'Unidentified customer'}
            </Typography>
            <Typography variant="caption" color="text.secondary"
                        sx={{ fontFamily: 'ui-monospace, Menlo, monospace' }}>
              {row.customer_mobile ?? row.session.slice(0, 10) + '…'}
            </Typography>
            {row.topic && (
              <Typography variant="body2" sx={{ mt: 0.5 }} color="text.secondary">
                “{row.topic}”
              </Typography>
            )}
          </Box>
          <Flex direction="row" align="center" spacing={1}>
            {phase === 'live' && <Chip size="small" color="success" label="Live — you hold this chat" />}
            {phase === 'ended' && <Chip size="small" label="Chat closed" />}
            {holder && phase !== 'live' && (
              <Chip size="small" variant="outlined" label={`Held by ${holder}`} />
            )}
            <IconButton onClick={onClose} aria-label="Close panel">
              <CloseRoundedIcon />
            </IconButton>
          </Flex>
        </Flex>
      </Box>

      {/* The thread. THE OPERATOR'S SIDE IS THE RIGHT: agent and staff turns
          align right, the customer left — the same orientation the customer
          sees, mirrored. Arabic turns render RTL inside their own bubble. */}
      <Box ref={scrollRef} sx={{ flex: 1, overflowY: 'auto', p: 2.5, bgcolor: '#fff' }}>
        {lines.length === 0 && phase === 'reading' && (
          <Typography variant="body2" color="text.secondary">Loading the conversation…</Typography>
        )}
        {lines.map((l) =>
          l.speaker === 'system' ? (
            <Typography key={l.id} variant="caption" color="text.disabled"
                        sx={{ display: 'block', textAlign: 'center', my: 1 }}>
              {l.text}
            </Typography>
          ) : (
            <Box key={l.id}
                 sx={{ display: 'flex',
                       justifyContent: l.speaker === 'customer' ? 'flex-start' : 'flex-end',
                       mb: 1 }}>
              <Box
                dir={l.lang === 'ar' ? 'rtl' : 'ltr'}
                sx={{
                  maxWidth: '80%',
                  px: 1.5, py: 1,
                  borderRadius: 2,
                  fontSize: 14,
                  whiteSpace: 'pre-wrap',
                  bgcolor: l.speaker === 'customer' ? 'grey.100'
                    : l.speaker === 'staff' ? 'primary.main' : 'grey.200',
                  color: l.speaker === 'staff' ? 'primary.contrastText' : 'text.primary',
                }}
              >
                {(l.speaker === 'staff' || l.speaker === 'agent') && (
                  <Typography variant="caption" sx={{ display: 'block', opacity: 0.75, fontWeight: 600 }}>
                    {l.speaker === 'staff' ? (l.name ?? 'Staff') : 'Aisha (AI)'}
                  </Typography>
                )}
                {l.text}
              </Box>
            </Box>
          ),
        )}
      </Box>

      <Box sx={{ p: 2, borderTop: '1px solid', borderColor: 'divider', bgcolor: '#fafbfc' }}>
        {refusal && (
          <Typography variant="body2" color="error.main" sx={{ mb: 1 }}>
            {refusal}
          </Typography>
        )}
        {phase === 'live' ? (
          <>
            <Flex direction="row" spacing={1} align="center">
              <TextField
                fullWidth
                size="small"
                placeholder="Message the customer…"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    send()
                  }
                }}
                multiline
                maxRows={4}
                autoFocus
              />
              <IconButton color="primary" onClick={send} disabled={!draft.trim()}
                          aria-label="Send message">
                <SendRoundedIcon />
              </IconButton>
            </Flex>
            <Button size="small" color="error" sx={{ mt: 1 }} onClick={endAndClose}>
              End &amp; close chat
            </Button>
          </>
        ) : phase === 'ended' ? (
          <Typography variant="body2" color="text.secondary">
            This conversation has ended. The transcript stays in the Conversations tab.
          </Typography>
        ) : phase === 'refused' ? null : (
          <Flex direction="row" spacing={1} align="center">
            <TextField
              size="small"
              label="Your name"
              helperText="Shown to the customer on every message you send."
              value={name}
              onChange={(e) => setName(e.target.value)}
              sx={{ minWidth: 220 }}
            />
            <Button variant="contained" onClick={takeOver} disabled={!name.trim() || phase === 'reading'}>
              {holder ? 'Rejoin chat' : 'Take over chat'}
            </Button>
          </Flex>
        )}
      </Box>
    </Drawer>
  )
}

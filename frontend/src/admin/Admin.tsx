// Console shell — permanent drawer + routed content.
//
// SEPARATE ROUTE, NOT A DRAWER OVER THE CHAT. The console browses full tables;
// a slide-over sized for a conversation would fight every one of them. App.tsx
// stays completely untouched — no props, no lifted state — so none of this can
// regress the demo the client watches.
//
// ScopedCssBaseline, NOT CssBaseline. The global variant would write MUI's
// resets onto <body>, which the chat widget also renders into. Scoping keeps
// every MUI reset inside this subtree; the console and the chat never fight
// over the same element even though they share a document.
//
// ENGLISH CHROME, BILINGUAL DATA. Labels here are hardcoded English because
// this is an internal operator screen, consistent with the chat's own chrome.
// What stays bilingual is the DATA — an Arabic name or address gets its own
// `dir` per value (rule 3, LOC-01).

import { NavLink, Outlet } from 'react-router-dom'
import { ThemeProvider } from '@mui/material/styles'
import Box from '@mui/material/Box'
import Drawer from '@mui/material/Drawer'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import ListItemIcon from '@mui/material/ListItemIcon'
import ListItemText from '@mui/material/ListItemText'
import ListSubheader from '@mui/material/ListSubheader'
import ScopedCssBaseline from '@mui/material/ScopedCssBaseline'
import Typography from '@mui/material/Typography'
import Divider from '@mui/material/Divider'

import DashboardRoundedIcon from '@mui/icons-material/DashboardRounded'
import DirectionsCarRoundedIcon from '@mui/icons-material/DirectionsCarRounded'
import StorefrontRoundedIcon from '@mui/icons-material/StorefrontRounded'
import EventAvailableRoundedIcon from '@mui/icons-material/EventAvailableRounded'
import BookOnlineRoundedIcon from '@mui/icons-material/BookOnlineRounded'
import ArrowBackRoundedIcon from '@mui/icons-material/ArrowBackRounded'
// Icons for the four hidden nav entries below. Commented out WITH them, not
// left behind: `noUnusedLocals` is on, so an unused import fails `tsc` and
// therefore the production build — a dead import here breaks the deploy.
// import PeopleAltRoundedIcon from '@mui/icons-material/PeopleAltRounded'
// import ForumRoundedIcon from '@mui/icons-material/ForumRounded'
// import NotificationsActiveRoundedIcon from '@mui/icons-material/NotificationsActiveRounded'
import FactCheckRoundedIcon from '@mui/icons-material/FactCheckRounded'
import SupportAgentRoundedIcon from '@mui/icons-material/SupportAgentRounded'
import AltRouteRoundedIcon from '@mui/icons-material/AltRouteRounded'

import { theme } from './theme'

const DRAWER_WIDTH = 248

// Sidebar chrome, supplied by the developer 23 Jul 2026. Kept as named constants
// rather than inlined at each use so a later brand pass edits one line per token
// — and so the amber reads as one decision rather than three coincidences.
const SIDEBAR_BG = 'rgba(19, 21, 23, 1)'
/** The ACTIVE tab's BORDER — not a fill. The pill stays dark and the amber
 *  hairline plus the shadow below are what mark the current page. */
const ACTIVE_BORDER = 'rgba(255, 179, 0, 1)'
const ACTIVE_SHADOW = '0px 4px 14px 0px rgba(0, 0, 0, 0.32)'
const NAV_TEXT = 'rgba(255, 255, 255, 0.72)'
/** Nav label type. Inter is loaded in index.html alongside the two existing
 *  families; without that link this would silently fall back to system-ui. */
const NAV_FONT = {
  fontFamily: "'Inter', system-ui, sans-serif",
  fontWeight: 400,
  fontSize: 12.8,
  lineHeight: '19.31px',
  letterSpacing: 0,
} as const

type NavItem = {
  to: string
  label: string
  Icon: typeof DashboardRoundedIcon
  /** `end` only on the index route, so /admin does not stay highlighted while a
   *  child tab is open. Declared optional on the shared type, otherwise the
   *  array widens to a union and `t.end` stops type-checking on the others. */
  end?: boolean
}

const NAV: { group: string; items: NavItem[] }[] = [
  { group: 'Overview', items: [{ to: '/admin', label: 'Dashboard', end: true, Icon: DashboardRoundedIcon }] },
  {
    group: 'Catalogue',
    items: [
      { to: '/admin/vehicles', label: 'Vehicles', Icon: DirectionsCarRoundedIcon },
      { to: '/admin/showrooms', label: 'Showrooms', Icon: StorefrontRoundedIcon },
      { to: '/admin/slots', label: 'Slots', Icon: EventAvailableRoundedIcon },
    ],
  },
  {
    group: 'Operations',
    items: [
      { to: '/admin/bookings', label: 'Bookings', Icon: BookOnlineRoundedIcon },
      // HIDDEN FROM THE SIDEBAR, NOT DELETED (2026-07-23). Customers,
      // Notifications, Conversations and Audit trail are commented out of the
      // nav only — their routes in main.tsx and their tab components under
      // admin/tabs/ are untouched, so each is still reachable by typing its URL
      // and comes back by uncommenting these lines plus their icon imports.
      // { to: '/admin/customers', label: 'Customers', Icon: PeopleAltRoundedIcon },
      // { to: '/admin/notifications', label: 'Notifications', Icon: NotificationsActiveRoundedIcon },
    ],
  },
  // The 'Agent' group carries the Audit trail (rule 15 / FR-ES visibility): the
  // per-action log where a human-callback escalation, every tool call and every
  // consent change appear. Conversations stays hidden for now (uncomment its line
  // plus ForumRoundedIcon to restore) — a group with items renders fine; it was
  // only commented whole earlier to avoid a bare "AGENT" heading over nothing.
  {
    group: 'Agent',
    items: [
      // { to: '/admin/conversations', label: 'Conversations', Icon: ForumRoundedIcon },
      // HANDOFFS IS FIRST IN THIS GROUP ON PURPOSE (T8.1). It is the only screen
      // here that is a WORK QUEUE rather than a record: somebody is waiting for
      // a call, and the plan's own note is that this became "the finish line for
      // the highest-value conversations" once Aisha started taking money
      // questions. Burying it under the audit trail would be burying the one
      // thing on this panel that needs doing rather than reading.
      { to: '/admin/handoffs', label: 'Handoffs', Icon: SupportAgentRoundedIcon },
      { to: '/admin/routing', label: 'Routing', Icon: AltRouteRoundedIcon },
      { to: '/admin/audit', label: 'Audit trail', Icon: FactCheckRoundedIcon },
    ],
  },
]

export default function Admin() {
  return (
    <ThemeProvider theme={theme}>
      <ScopedCssBaseline
        sx={{
          // styles.css sets `#root { height: 100%; display: flex }` for the
          // chat's centred 100dvh column. That FIXES this panel to exactly one
          // viewport tall — a flex item in a 100%-height row cannot grow past
          // its container — so a `minHeight:100vh` here did nothing: the box was
          // already pinned to 100vh and its grey background stopped at the fold,
          // letting the white <body> show through below it (the seam).
          //
          // The fix is to STOP the page growing and scroll INSIDE instead: this
          // panel is exactly the viewport, the drawer stays put, and `main`
          // below owns the scroll. The grey then always fills the viewport
          // because the thing painting it never leaves it.
          flex: '1 1 auto',
          minWidth: 0,
          display: 'flex',
          height: '100vh',
          overflow: 'hidden',
          bgcolor: 'background.default',
        }}
      >
        <Drawer
          variant="permanent"
          sx={{
            width: DRAWER_WIDTH,
            flexShrink: 0,
            '& .MuiDrawer-paper': {
              width: DRAWER_WIDTH,
              boxSizing: 'border-box',
              background: SIDEBAR_BG,
              color: '#fff',
              borderRight: 'none',
            },
          }}
        >
          <Box sx={{ p: 2.5, display: 'flex', alignItems: 'center', gap: 1.5 }}>
            {/* The real Elite mark, replacing the "E" placeholder tile. It is a
                white script wordmark on transparency, so it needs no plate
                behind it on this background — and `objectFit: contain` keeps it
                from stretching if the asset is ever re-exported at another
                aspect ratio. Decorative: the wordmark beside it names the
                product, so this is hidden from assistive tech. */}
            <Box
              component="img"
              src="/EliteLogo.png"
              alt=""
              aria-hidden="true"
              sx={{ width: 36, height: 36, objectFit: 'contain', flexShrink: 0 }}
            />
            <Box sx={{ minWidth: 0 }}>
              <Typography
                sx={{
                  fontFamily: "'Space Grotesk', system-ui, sans-serif",
                  fontWeight: 700,
                  fontSize: 15,
                  lineHeight: '18px',
                  letterSpacing: 0,
                  color: 'rgba(255, 255, 255, 1)',
                }}
              >
                Elite Console
              </Typography>
              <Typography sx={{ fontSize: 11, color: 'rgba(255,255,255,.5)' }}>
                Test drive booking agent
              </Typography>
            </Box>
          </Box>

          <Divider sx={{ borderColor: 'rgba(255,255,255,.08)' }} />

          <Box sx={{ overflowY: 'auto', flex: 1, py: 1 }}>
            {NAV.map((section) => (
              <List
                key={section.group}
                dense
                subheader={
                  <ListSubheader
                    disableSticky
                    sx={{
                      bgcolor: 'transparent',
                      color: 'rgba(255,255,255,.35)',
                      fontSize: 10.5,
                      fontWeight: 600,
                      letterSpacing: '.09em',
                      textTransform: 'uppercase',
                      lineHeight: 2.6,
                    }}
                  >
                    {section.group}
                  </ListSubheader>
                }
              >
                {section.items.map(({ to, label, end, Icon }) => (
                  <ListItemButton
                    key={to}
                    component={NavLink}
                    to={to}
                    end={end}
                    sx={{
                      mx: 1,
                      borderRadius: 1.5,
                      color: NAV_TEXT,
                      // A TRANSPARENT border on every item, not just the active
                      // one. The active state adds only a colour here, so the
                      // row cannot grow by a pixel and shove its neighbours as
                      // the page changes.
                      border: '1px solid transparent',
                      '&:hover': { bgcolor: 'rgba(255,255,255,.07)', color: '#fff' },
                      '&.active': {
                        // BORDER, NOT FILL. The pill keeps the sidebar's own
                        // background; the amber hairline and the shadow are what
                        // mark it, so the label stays on the same dark ground as
                        // every other row and needs no second text colour.
                        borderColor: ACTIVE_BORDER,
                        boxShadow: ACTIVE_SHADOW,
                        '& .MuiListItemIcon-root': { color: ACTIVE_BORDER },
                      },
                    }}
                  >
                    <ListItemIcon sx={{ minWidth: 34, color: 'rgba(255,255,255,.6)' }}>
                      <Icon fontSize="small" />
                    </ListItemIcon>
                    <ListItemText
                      slotProps={{ primary: { sx: NAV_FONT } }}
                      primary={label}
                    />
                  </ListItemButton>
                ))}
              </List>
            ))}
          </Box>

          <Divider sx={{ borderColor: 'rgba(255,255,255,.08)' }} />
          <Box sx={{ p: 1.5 }}>
            <ListItemButton
              component="a"
              href="/"
              sx={{
                borderRadius: 1.5,
                color: 'rgba(255,255,255,.55)',
                '&:hover': { bgcolor: 'rgba(255,255,255,.07)', color: '#fff' },
              }}
            >
              <ListItemIcon sx={{ minWidth: 34, color: 'inherit' }}>
                <ArrowBackRoundedIcon fontSize="small" />
              </ListItemIcon>
              {/* Same type as the tabs above it — it sits in the same list and
                  reading as a different species is what made it look bolted on. */}
              <ListItemText slotProps={{ primary: { sx: NAV_FONT } }} primary="Back to chat" />
            </ListItemButton>
          </Box>
        </Drawer>

        {/* Extra bottom padding beyond the symmetric page inset, so the last
            row of cards is not flush against the viewport edge when a tab
            scrolls to its end. Applies to every console tab, not just the
            dashboard — they all share this main region.

            bgcolor HERE, not only on the ScopedCssBaseline parent, fixes a
            background SEAM. styles.css sets `#root { height: 100%; display:
            flex }` for the customer chat's centred column, which stretches the
            grey ScopedCssBaseline box to exactly one viewport tall — so on a
            page taller than 100vh the grey stopped at the fold and the white
            <body> showed through below it. This <main> grows with its content,
            so painting the grey here covers the full scroll height. minHeight
            keeps a short tab grey to the bottom too. */}
        <Box
          component="main"
          sx={{
            flexGrow: 1,
            minWidth: 0,
            // main IS the scroll region now (the parent is fixed to the
            // viewport and clips). Grey is painted here and fills the visible
            // area at every scroll position, so no white body can show.
            height: '100%',
            overflowY: 'auto',
            bgcolor: 'background.default',
            p: { xs: 2, md: 4 },
            pb: { xs: 5, md: 7 },
          }}
        >
          <Outlet />
        </Box>
      </ScopedCssBaseline>
    </ThemeProvider>
  )
}

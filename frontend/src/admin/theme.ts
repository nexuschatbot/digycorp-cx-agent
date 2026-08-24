// MUI theme for the console.
//
// DERIVED FROM THE EXISTING TOKENS, NOT FROM MATERIAL'S DEFAULTS. styles.css
// already carries the palette and the two typefaces the brand pass will edit
// (--color-accent, Space Grotesk, IBM Plex Sans Arabic). Shipping MUI's default
// indigo/Roboto would put a second, rival design language in the same repo and
// make the console look like a different product from the chat.
//
// Scoped to the console: this theme is applied by Admin.tsx only, and the admin
// routes are lazy-loaded, so none of MUI reaches the customer chat bundle.

import { createTheme } from '@mui/material/styles'

const BRAND = '#1a5cff'

export const theme = createTheme({
  cssVariables: true,
  palette: {
    mode: 'light',
    primary: { main: BRAND, contrastText: '#ffffff' },
    success: { main: '#12855a' },
    warning: { main: '#a8620a' },
    error: { main: '#c0392f' },
    info: { main: '#2b5bd7' },
    background: { default: '#f5f6f8', paper: '#ffffff' },
    text: { primary: '#12151a', secondary: '#4a5261' },
    divider: '#e6e9ef',
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: "'IBM Plex Sans Arabic', system-ui, sans-serif",
    h1: { fontFamily: "'Space Grotesk', system-ui, sans-serif", fontWeight: 600 },
    h2: { fontFamily: "'Space Grotesk', system-ui, sans-serif", fontWeight: 600 },
    h3: { fontFamily: "'Space Grotesk', system-ui, sans-serif", fontWeight: 600 },
    h4: { fontFamily: "'Space Grotesk', system-ui, sans-serif", fontWeight: 600 },
    h5: { fontFamily: "'Space Grotesk', system-ui, sans-serif", fontWeight: 600 },
    h6: { fontFamily: "'Space Grotesk', system-ui, sans-serif", fontWeight: 600 },
    button: { textTransform: 'none', fontWeight: 500 },
    overline: { fontWeight: 600, letterSpacing: '0.06em' },
  },
  components: {
    MuiPaper: {
      styleOverrides: {
        // Material's default elevation shadows are heavier than a dense data
        // screen wants — they make every card shout. Flattened to a hairline
        // border plus a whisper of shadow.
        root: { backgroundImage: 'none' },
      },
    },
    MuiCard: {
      defaultProps: { elevation: 0 },
      styleOverrides: {
        root: {
          border: '1px solid #e6e9ef',
          boxShadow: '0 1px 2px rgba(16,22,34,0.05)',
          transition: 'box-shadow .15s ease',
          '&:hover': { boxShadow: '0 2px 10px rgba(16,22,34,0.08)' },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { fontWeight: 500 },
        sizeSmall: { height: 22, fontSize: '0.72rem' },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
    },
    MuiTextField: {
      defaultProps: { size: 'small' },
    },
    MuiSelect: {
      defaultProps: { size: 'small' },
    },
    MuiTooltip: {
      defaultProps: { arrow: true },
    },
  },
})

/** Status → MUI colour, one mapping used by every tab.
 *
 *  Semantic groups, not a colour per value: an operator scanning a column makes
 *  three distinctions — fine / inert / ended-or-undone — so a status the seed
 *  grows later inherits a sensible default rather than rendering unstyled. */
export function statusColor(
  value: string | null | undefined,
): 'success' | 'warning' | 'error' | 'info' | 'default' {
  switch (value) {
    case 'open':
    case 'active':
    case 'confirmed':
    case 'sent':
    case 'live':
    case 'agent':
      return 'success'
    case 'locked':
    case 'scheduled':
    case 'admin':
      return 'warning'
    case 'cancelled':
      return 'error'
    case 'booked':
    case 'rescheduled':
    case 'customer':
      return 'info'
    default:
      return 'default'
  }
}

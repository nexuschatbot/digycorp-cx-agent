// Per-message direction (FR-CX-03 / LOC-01, transport rendering only).
//
// The rule is deliberately per-BUBBLE, not per-conversation: a chat can switch
// EN<->AR turn to turn (S-19), and a single Arabic sentence with "Jetour T2" or a
// +971 number inside it must still render RTL as a whole. Detection is on the
// presence of any Arabic-script codepoint — this renders Arabic SCRIPT correctly
// regardless of the dialogue's quality (which stays minimal-prompt this stage).

const ARABIC = /[؀-ۿ]/

export function isRtl(text: string): boolean {
  return ARABIC.test(text)
}

export function dirFor(text: string): 'rtl' | 'ltr' {
  return isRtl(text) ? 'rtl' : 'ltr'
}

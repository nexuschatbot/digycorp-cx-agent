// Inline booking-confirmation receipt — a glanceable UI signal that a booking
// landed, driven by the `booking` SSE event (not the agent's prose). Shows the TD
// reference. The tick is a real SVG element (not a ✓ character) so it renders
// consistently and the layout mirrors cleanly in an Arabic conversation.

export function BookingReceipt({ bookingRef, dir }: { bookingRef: string; dir: 'rtl' | 'ltr' }) {
  return (
    <div className={`row row--receipt row--${dir}`}>
      <div className="receipt" dir={dir}>
        <span className="receipt-tick" aria-hidden="true">
          <svg viewBox="0 0 16 16" width="14" height="14" role="img" focusable="false">
            <path
              d="M13.5 4.5 6.5 11.5 3 8"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        <span className="receipt-label">Booked</span>
        <span className="receipt-ref">{bookingRef}</span>
      </div>
    </div>
  )
}

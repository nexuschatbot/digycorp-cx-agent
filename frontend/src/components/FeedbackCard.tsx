// Post-chat feedback on Aisha (the AGENT), shown when the conversation closes:
// a 5-star rating with a standard word label, plus an optional comment. On submit
// it POSTs to /api/feedback, which stores it and the Salesforce sidecar mirrors it
// to Test_Drive_Agent_Feedback__c. Distinct from the post-drive survey.
import { useState } from 'react'

const WORDS = ['', 'Poor', 'Fair', 'Good', 'Very good', 'Excellent']

export function FeedbackCard({
  onSubmit,
}: {
  onSubmit: (stars: number, comment: string) => Promise<void>
}) {
  const [stars, setStars] = useState(0)
  const [hover, setHover] = useState(0)
  const [comment, setComment] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'done' | 'error'>('idle')

  if (state === 'done') {
    return (
      <div className="fb-card fb-card--done">
        <span className="fb-done-tick" aria-hidden="true">✓</span>
        Thank you for your feedback!
      </div>
    )
  }

  const active = hover || stars
  const submit = async () => {
    if (stars < 1 || state === 'sending') return
    setState('sending')
    try {
      await onSubmit(stars, comment.trim())
      setState('done')
    } catch {
      setState('error')
    }
  }

  return (
    <div className="fb-card">
      <p className="fb-title">How was your chat with Aisha?</p>
      <div className="fb-stars" role="radiogroup" aria-label="Star rating">
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            type="button"
            className={`fb-star${n <= active ? ' fb-star--on' : ''}`}
            onMouseEnter={() => setHover(n)}
            onMouseLeave={() => setHover(0)}
            onFocus={() => setHover(n)}
            onBlur={() => setHover(0)}
            onClick={() => setStars(n)}
            aria-label={`${n} star${n > 1 ? 's' : ''}`}
            aria-pressed={n <= stars}
          >
            ★
          </button>
        ))}
      </div>
      <p className="fb-word">{WORDS[active] || ' '}</p>
      <textarea
        className="fb-comment"
        placeholder="Add a comment (optional)"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        rows={2}
        maxLength={2000}
      />
      {state === 'error' && <p className="fb-error">Couldn’t send that — please try again.</p>}
      <button
        type="button"
        className="btn btn--send fb-submit"
        disabled={stars < 1 || state === 'sending'}
        onClick={submit}
      >
        {state === 'sending' ? 'Sending…' : 'Submit feedback'}
      </button>
    </div>
  )
}

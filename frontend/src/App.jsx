import { useEffect, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

export default function App() {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((res) => res.json())
      .then(setHealth)
      .catch(() => setHealth({ status: 'unreachable' }))
  }, [])

  return (
    <main>
      <h1>Digycorp CX Agent</h1>
      <p>Backend status: {health ? health.status : 'checking…'}</p>
    </main>
  )
}

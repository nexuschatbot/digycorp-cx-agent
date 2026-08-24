import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App'
import './styles.css'

// THE ADMIN ROUTES ARE LAZY, AND THAT IS THE POINT OF THE SPLIT.
//
// The console runs on MUI (~300 kB with Emotion and the DataGrid). The customer
// chat does not use a byte of it. Importing these eagerly would put that weight
// in the single bundle every QR-code attendee downloads over conference mobile
// data for a chat they will never leave — which is the opposite of AD-B's whole
// reason for choosing plain HTTP streaming.
//
// Lazy + a Suspense boundary means Vite emits the console as its own chunk,
// fetched only when someone actually navigates to /admin.
const Admin = lazy(() => import('./admin/Admin'))
const Overview = lazy(() => import('./admin/tabs/Overview'))
const Vehicles = lazy(() => import('./admin/tabs/Vehicles'))
const Showrooms = lazy(() => import('./admin/tabs/Showrooms'))
const Slots = lazy(() => import('./admin/tabs/Slots'))
const Bookings = lazy(() => import('./admin/tabs/Bookings'))
const Customers = lazy(() => import('./admin/tabs/Customers'))
const Conversations = lazy(() => import('./admin/tabs/Conversations'))
const Notifications = lazy(() => import('./admin/tabs/Notifications'))
const Audit = lazy(() => import('./admin/tabs/Audit'))

const InsightsList = lazy(() =>
  import('./insights/Insights').then((m) => ({ default: m.InsightsList })),
)
const SessionInsight = lazy(() =>
  import('./insights/Insights').then((m) => ({ default: m.SessionInsight })),
)

// Two roots: the customer chat at "/" and the operator console at "/admin".
// App.tsx is mounted exactly as before — no props, no wrapper, no shared state
// — so nothing the console does can regress the demo the client watches.
//
// ⚠️ A deep link to /admin needs history fallback from whatever serves the
// build. The Vite dev server does it automatically; nginx in
// frontend/Dockerfile does NOT until it has `try_files $uri /index.html`.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Suspense fallback={null}>
        <Routes>
          <Route path="/" element={<App />} />
          <Route path="/insights" element={<InsightsList />} />
          <Route path="/insights/:sessionId" element={<SessionInsight />} />
          <Route path="/admin" element={<Admin />}>
            <Route index element={<Overview />} />
            <Route path="vehicles" element={<Vehicles />} />
            <Route path="showrooms" element={<Showrooms />} />
            <Route path="slots" element={<Slots />} />
            <Route path="bookings" element={<Bookings />} />
            <Route path="customers" element={<Customers />} />
            <Route path="conversations" element={<Conversations />} />
            <Route path="notifications" element={<Notifications />} />
            <Route path="audit" element={<Audit />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  </StrictMode>,
)

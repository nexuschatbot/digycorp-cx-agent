// Branch map for the dashboard — Leaflet over OpenStreetMap tiles.
//
// A LOCKED VIEW, ON PURPOSE. No pan, no zoom, no scroll-wheel. Two reasons, and
// the second is the one that matters on stage: a map embedded in a scrolling
// page steals the wheel, so scrolling the dashboard with the cursor over this
// panel would zoom the map to the middle of the ocean instead of moving the
// page. Locking it also means nothing can be left knocked out of position by
// whoever used the console last.
//
// TILES COME FROM THE NETWORK, and that is a real demo-day dependency: on a
// blocked or flat conference connection the pins still render (they are our own
// markup) but the map behind them is blank grey. Stated here rather than
// discovered live — the same reason useVoice names the HTTPS blocker.
//
// ⚠️ THE COORDINATES ARE APPROXIMATE, PENDING ELITE SIGN-OFF — district
// positions, not surveyed showroom entrances. They are seeded on the Showroom
// row; a branch with no coordinates never reaches this component, because the
// service drops it rather than pinning it at (0,0).
//
// LEAFLET IS IMPERATIVE, so this is the one place in the console with a manual
// DOM lifecycle. It lives behind the dashboard's lazy route, so the ~40 kB never
// reaches the customer chat bundle (main.tsx's whole reason for splitting).

import { useEffect, useRef } from 'react'
import Box from '@mui/material/Box'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

import type { MapPin } from '../adminApi'

/** Centre and zoom chosen to hold all five branches — Ajman in the north-east
 *  to Al Ain in the south — in one frame at the panel's size. */
const UAE_CENTRE: [number, number] = [24.9, 55.1]
const UAE_ZOOM = 7.4

/** The pin. A DIV MARKER, not Leaflet's default image marker: the default is a
 *  PNG resolved relative to the CSS, which bundlers famously break, and this
 *  needs to carry a count and a name anyway. Built as markup so it inherits the
 *  page's fonts rather than looking like a different product. */
function pinHtml(pin: MapPin, labelOffset: number, flip: boolean): string {
  // Drop a leading brand word ("Jetour Al Quoz" -> "Al Quoz"). On a Jetour-only
  // map (CLAUDE.md: the demo ships Jetour alone) the brand is on every pin, so
  // repeating it is pure noise — and the full names overran the 432px card. Done
  // generically off the first token rather than hardcoding "Jetour", so a
  // second brand later still trims its own prefix rather than needing an edit.
  // The full name stays intact in the leaderboard row beneath the map.
  const short = pin.name.replace(/^\S+\s+/, '') || pin.name
  // The name is catalogue data, not user input, but it lands in innerHTML — so
  // escape it rather than trusting that to stay true.
  const name = short.replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string,
  )
  // The DOT stays on the coordinate and only the LABEL moves (see
  // `labelOffsets`) — a nudged dot would be a wrong claim about where a branch
  // is, while a nudged label is just a label that had to make room.
  //
  // `flip` puts the label on the LEFT of the dot for a branch near the right
  // edge. Ajman is the far north-east of the country and its label ran off the
  // 432px card, which reads as a rendering bug rather than as a map that has
  // run out of room. row-reverse rather than a second template, so the two
  // sides cannot drift apart.
  return `
    <div class="branch-pin" style="flex-direction: ${flip ? 'row-reverse' : 'row'}">
      <span class="branch-pin-dot"></span>
      <span class="branch-pin-label" style="transform: translateY(${labelOffset}px)">${name}: ${pin.bookings}</span>
    </div>`
}

/** Push apart labels that would otherwise sit on top of each other.
 *
 *  Three of the five branches are inside greater Dubai — Al Quoz, Deira and
 *  Ajman span about 30 km, which at this zoom is roughly one label's height, so
 *  drawn naively their three labels overlap into an unreadable stack.
 *
 *  Computed from the ACTUAL projected positions rather than hardcoded per
 *  branch: a coordinate correction (they are pending Elite sign-off) or a sixth
 *  showroom must not silently reintroduce the collision this exists to fix.
 *  Safe to compute once because the map is locked — no pan, no zoom, so the
 *  projection never changes under it.
 */
function labelOffsets(map: L.Map, pins: MapPin[]): number[] {
  const LABEL_H = 22 // label height plus a hair of breathing room
  const points = pins.map((p) => map.latLngToContainerPoint([p.lat, p.lng]).y)
  const offsets = new Array<number>(pins.length).fill(0)
  // North to south, so each label makes room below the one above it.
  const order = points.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y)
  let lastBottom = -Infinity
  for (const { y, i } of order) {
    const shifted = Math.max(y, lastBottom + LABEL_H)
    offsets[i] = shifted - y
    lastBottom = shifted
  }
  return offsets
}

export function BranchMap({ pins }: { pins: MapPin[] }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)

  // Create once. Re-creating on every pin change would tear down and refetch
  // the tiles on each 30-second refresh, which flashes the panel grey.
  useEffect(() => {
    if (!hostRef.current || mapRef.current) return
    const map = L.map(hostRef.current, {
      center: UAE_CENTRE,
      zoom: UAE_ZOOM,
      zoomControl: false,
      attributionControl: true,
      // The locked view, spelled out interaction by interaction.
      dragging: false,
      scrollWheelZoom: false,
      doubleClickZoom: false,
      boxZoom: false,
      keyboard: false,
      touchZoom: false,
    })
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      // Required by the OSM tile usage policy — this is someone else's
      // bandwidth, and the credit is the price of using it.
      attribution: '&copy; OpenStreetMap',
    }).addTo(map)
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    return () => {
      map.remove()
      mapRef.current = null
      layerRef.current = null
    }
  }, [])

  // Pins refresh in place as the window changes or the 30s poll lands.
  useEffect(() => {
    const layer = layerRef.current
    const map = mapRef.current
    if (!layer || !map) return
    layer.clearLayers()
    const offsets = labelOffsets(map, pins)
    // Past this fraction of the width, a right-hand label would run off the
    // card, so it swaps to the left of its dot.
    const flipAfter = map.getSize().x * 0.55
    for (const [i, pin] of pins.entries()) {
      const flip = map.latLngToContainerPoint([pin.lat, pin.lng]).x > flipAfter
      L.marker([pin.lat, pin.lng], {
        interactive: false,
        icon: L.divIcon({
          html: pinHtml(pin, offsets[i], flip),
          className: 'branch-pin-icon',
          // Sized generously and anchored at the dot, so a long branch name
          // does not shift the pin off the place it is marking.
          iconSize: [0, 0],
          iconAnchor: [0, 0],
        }),
      }).addTo(layer)
    }
  }, [pins])

  return (
    <Box
      ref={hostRef}
      sx={{
        height: 300,
        borderRadius: 2,
        overflow: 'hidden',
        // Shows through wherever tiles have not loaded — a neutral map-ish
        // grey rather than white, so a blocked network reads as "map not
        // loaded" instead of "panel broken".
        bgcolor: '#e8eaed',
        '& .leaflet-container': { height: '100%', width: '100%', background: '#e8eaed' },
        // The pin itself. Positioned relative to its anchor, so the dot sits on
        // the coordinate and the label hangs to its right.
        '& .branch-pin': {
          position: 'absolute',
          transform: 'translate(-7px, -7px)',
          display: 'flex',
          alignItems: 'center',
          gap: '4px',
          whiteSpace: 'nowrap',
        },
        '& .branch-pin-dot': {
          width: 14,
          height: 14,
          borderRadius: '50%',
          background: '#e02424',
          border: '2px solid #fff',
          boxShadow: '0 1px 4px rgba(0,0,0,.4)',
          flexShrink: 0,
        },
        '& .branch-pin-label': {
          background: '#e02424',
          color: '#fff',
          fontFamily: "'Inter', system-ui, sans-serif",
          fontSize: 11,
          fontWeight: 600,
          lineHeight: 1,
          padding: '4px 8px',
          borderRadius: 999,
          boxShadow: '0 1px 4px rgba(0,0,0,.3)',
        },
      }}
    />
  )
}

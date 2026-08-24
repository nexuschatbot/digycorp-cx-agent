// Paged-list hook shared by every data-browser tab.
//
// One hook rather than per-tab state, because the failure and paging behaviour
// must be identical on all of them: nine tabs each hand-rolling "loading, then
// rows, unless error" is nine chances for one of them to render an empty table
// on a failed request — the exact conflation adminApi.ts exists to avoid.
//
// IN-FLIGHT REQUESTS ARE ABORTED ON CHANGE. Paging fast, or switching tabs
// mid-load, otherwise lets a slow earlier response land after a newer one and
// repaint the table with stale rows. AbortController is already the codebase's
// idiom for this (AD-E wires it to the chat Stop button); the same primitive
// solves the much smaller problem here.

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  isAbort,
  type AdminOptions,
  type Envelope,
  type QueryParams,
  type Result,
} from './adminApi'

export const PAGE_SIZE = 25

type Fetcher<T> = (
  params: QueryParams,
  opts: AdminOptions,
) => Promise<Result<Envelope<T>>>

export interface ListState<T> {
  rows: T[]
  total: number
  limit: number
  offset: number
  loading: boolean
  error: string | null
  setOffset: (next: number) => void
  reload: () => void
}

export function useList<T>(
  fetcher: Fetcher<T>,
  filters: QueryParams = {},
  pageSize: number = PAGE_SIZE,
): ListState<T> {
  const [rows, setRows] = useState<T[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  const abortRef = useRef<AbortController | null>(null)

  // Filters arrive as a fresh object literal each render, so depending on the
  // object identity would refetch forever. Serialising is the cheap fix and is
  // stable for the small flat filter maps these tabs use.
  const filterKey = JSON.stringify(filters)

  // The fetcher is a module-level function in practice, but a tab could pass an
  // inline closure; keeping it in a ref stops that from re-triggering the fetch.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    setLoading(true)
    const active = JSON.parse(filterKey) as QueryParams

    fetcherRef
      .current({ ...active, limit: pageSize, offset }, { signal: ctrl.signal })
      .then((res) => {
        if (ctrl.signal.aborted) return
        if (res.ok) {
          setRows(res.data.rows)
          setTotal(res.data.total)
          setError(null)
        } else if (!isAbort(res)) {
          // Clear the rows too. Leaving the previous page visible under an error
          // banner would show numbers that no longer describe anything — the
          // same "count nothing measured" problem rule 16 warns about.
          setRows([])
          setTotal(0)
          setError(res.error)
        }
        setLoading(false)
      })

    return () => ctrl.abort()
  }, [filterKey, offset, pageSize, nonce])

  // Changing a filter must return to page 1 — holding offset=75 while narrowing
  // to a 3-row result set would show an empty page that looks like no data.
  useEffect(() => {
    setOffset(0)
  }, [filterKey])

  const reload = useCallback(() => setNonce((n) => n + 1), [])

  return {
    rows,
    total,
    limit: pageSize,
    offset,
    loading,
    error,
    setOffset,
    reload,
  }
}

export interface ResourceState<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
}

/** For endpoints that are NOT paginated envelopes — i.e. the two contracted
 *  ones that return a bare list (audit-for-session, notifications) plus
 *  metrics. Their shapes are signed in CLAUDE.md, so the console adapts to them
 *  rather than reshaping them into an envelope for the convenience of useList.
 *
 *  `key` re-runs the fetch when the thing being requested changes (e.g. the
 *  selected session on the conversation drawer); pass null to fetch nothing. */
export function useResource<T>(
  fetcher: (opts: AdminOptions) => Promise<Result<T>>,
  key: string | null = '',
): ResourceState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(key !== null)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    if (key === null) {
      setData(null)
      setLoading(false)
      setError(null)
      return
    }
    const ctrl = new AbortController()
    setLoading(true)
    fetcherRef.current({ signal: ctrl.signal }).then((res) => {
      if (ctrl.signal.aborted) return
      if (res.ok) {
        setData(res.data)
        setError(null)
      } else if (!isAbort(res)) {
        setData(null)
        setError(res.error)
      }
      setLoading(false)
    })
    return () => ctrl.abort()
  }, [key, nonce])

  return {
    data,
    loading,
    error,
    reload: useCallback(() => setNonce((n) => n + 1), []),
  }
}

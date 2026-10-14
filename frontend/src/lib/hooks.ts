import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Job } from './types'

export interface Async<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
}

/** Загрузка данных с повторной загрузкой по reload(). Ответ устаревшего
 *  запроса (если deps сменились, пока он шёл) отбрасывается. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]): Async<T> {
  const [state, setState] = useState<{ data?: T; error?: Error; loading: boolean }>({ loading: true })
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let alive = true
    setState((s) => ({ ...s, loading: true }))
    load().then(
      (data) => alive && setState({ data, loading: false }),
      (error: Error) => alive && setState({ error, loading: false }),
    )
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  const reload = useCallback(() => setTick((t) => t + 1), [])
  return { data: state.data, error: state.error, loading: state.loading, reload }
}

/** Вызывать fn каждые ms, пока enabled. */
export function useInterval(fn: () => void, ms: number, enabled = true): void {
  const ref = useRef(fn)
  useEffect(() => {
    ref.current = fn
  })
  useEffect(() => {
    if (!enabled) return
    const id = window.setInterval(() => ref.current(), ms)
    return () => window.clearInterval(id)
  }, [ms, enabled])
}

const ACTIVE: Job['state'][] = ['queued', 'running']

/** Следить за задачей, пока она в очереди или выполняется. */
export function useJob(pid: string, jobId: string | undefined): Job | undefined {
  const [fetched, setFetched] = useState<Job>()
  // Ответ по прошлой задаче не показываем, пока не пришёл ответ по новой
  const job = fetched && fetched.id === jobId ? fetched : undefined
  const refresh = useCallback(() => {
    if (jobId) api.job(pid, jobId).then(setFetched, () => undefined)
  }, [pid, jobId])
  useEffect(refresh, [refresh])
  useInterval(refresh, 1000, !!jobId && (!job || ACTIVE.includes(job.state)))
  return job
}

/** Мелкая настройка в браузере (последний проект, вкладка). Может не
 *  сохраниться (приватный режим) — тогда просто живёт до перезагрузки. */
export function usePref<T extends string>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      return (window.localStorage.getItem(key) as T | null) ?? initial
    } catch {
      return initial
    }
  })
  const set = useCallback(
    (v: T) => {
      setValue(v)
      try {
        window.localStorage.setItem(key, v)
      } catch {
        // настройка не сохранится, это не ошибка
      }
    },
    [key],
  )
  return [value, set]
}

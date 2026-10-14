// Общие элементы по странице «Монтаж · Стиль»: ошибка, состояние задачи,
// выпадающий список, плеер со шкалой. Оформление — в styles/components.css.
import { useCallback, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from 'react'
import { ApiError } from '../lib/api'
import { fmtUs } from '../lib/time'
import type { Job } from '../lib/types'

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? `${error.message} (${error.code})` : String(error)
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <p role="alert" data-state="error" className="error-text">
      {errorMessage(error)}
    </p>
  )
}

/** Экран целиком: пусто или ошибка, с подсказкой, что делать. */
export function StatePanel({ state, title, children }: {
  state: 'empty' | 'error'
  title: string
  children?: ReactNode
}) {
  return (
    <div className="state-panel" data-state={state} role={state === 'error' ? 'alert' : undefined}>
      <h2>{title}</h2>
      {children}
    </div>
  )
}

export const JOB_STATE: Record<Job['state'], string> = {
  queued: 'в очереди',
  running: 'идёт',
  completed: 'готово',
  failed: 'ошибка',
  cancelled: 'отменено',
  interrupted: 'прервано',
}

/** Состояние задачи: значок, ход и пояснение под ним. */
export function JobStatus({ job, bar = true, note = true }: { job: Job | undefined; bar?: boolean; note?: boolean }) {
  if (!job) return null
  const pct = Math.round(job.progress * 100)
  const extra = [
    job.error ?? (job.state !== 'completed' && job.state !== 'running' ? job.message : ''),
    job.state === 'running' ? job.message : '',
    job.draft ? 'черновик: версия не утверждена' : '',
    job.stale ? 'устарел: версия больше не текущая' : '',
  ].filter(Boolean).join(' · ')
  return (
    <span className="job-status" data-state={job.state}>
      <span className="job-status-line">
        <span className="mark" data-state={job.state}>
          {JOB_STATE[job.state]}
          {job.state === 'running' && <> · <span className="mono">{pct}%</span></>}
        </span>
        {bar && job.state === 'running' && (
          <span className="progress" role="progressbar" aria-label="Ход" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${pct}%` }} />
          </span>
        )}
      </span>
      {note && extra && <span className="job-status-note">{extra}</span>}
    </span>
  )
}

/** Прежнее имя: состояние задачи одной строкой. */
export function JobLine({ job }: { job: Job | undefined }) {
  return <JobStatus job={job} />
}

export function Select({ small, className, ...props }: React.SelectHTMLAttributes<HTMLSelectElement> & { small?: boolean }) {
  return (
    <span className={['select', small ? 'sm' : '', className ?? ''].filter(Boolean).join(' ')}>
      <select {...props} />
    </span>
  )
}

export interface PlayerHandle {
  toggle: () => void
  play: () => void
  pause: () => void
  /** Сдвиг на us (может быть отрицательным), в пределах окна */
  seekBy: (us: number) => void
  seekTo: (us: number) => void
}

/** Плеер с окном показа [fromUs, toUs] во времени файла.
 *  offsetUs прибавляется к показываемому времени (у пробного ролика —
 *  время монтажа). cut — вырезаемый кусок пунктиром, jointUs — черта стыка. */
export function VideoPlayer({
  src, title, meta, label, fromUs = 0, toUs, startUs, offsetUs = 0, cut, jointUs, stopAtEnd = true,
  handle, onTime,
}: {
  src: string
  title?: ReactNode
  meta?: ReactNode
  label: string
  fromUs?: number
  toUs?: number
  startUs?: number
  offsetUs?: number
  cut?: [number, number]
  jointUs?: number
  stopAtEnd?: boolean
  handle?: Ref<PlayerHandle>
  onTime?: (us: number) => void
}) {
  const ref = useRef<HTMLVideoElement>(null)
  const [now, setNow] = useState(startUs ?? fromUs)
  const [duration, setDuration] = useState<number>()
  const [playing, setPlaying] = useState(false)
  const end = toUs ?? duration ?? fromUs
  const span = Math.max(1, end - fromUs)

  const seekTo = useCallback((us: number) => {
    const v = ref.current
    if (!v) return
    const t = Math.min(Math.max(us, fromUs), end)
    v.currentTime = t / 1e6
    setNow(t)
  }, [fromUs, end])

  useImperativeHandle(handle, () => ({
    toggle: () => { const v = ref.current; if (v) void (v.paused ? v.play() : v.pause()) },
    play: () => { void ref.current?.play() },
    pause: () => ref.current?.pause(),
    seekBy: (us: number) => seekTo((ref.current?.currentTime ?? 0) * 1e6 + us),
    seekTo,
  }), [seekTo])

  useEffect(() => {
    const v = ref.current
    if (!v) return
    const start = () => {
      setDuration(Number.isFinite(v.duration) ? v.duration * 1e6 : undefined)
      v.currentTime = (startUs ?? fromUs) / 1e6
      setNow(startUs ?? fromUs)
    }
    if (v.readyState >= 1) start()
    v.addEventListener('loadedmetadata', start)
    return () => v.removeEventListener('loadedmetadata', start)
  }, [src, startUs, fromUs])

  // Пока идёт показ — курсор плавно, по кадрам экрана
  useEffect(() => {
    if (!playing) return
    let id = 0
    const tick = () => {
      const v = ref.current
      if (v) {
        const us = v.currentTime * 1e6
        setNow(us)
        onTime?.(us)
        if (stopAtEnd && toUs !== undefined && us >= toUs) v.pause()
      }
      id = requestAnimationFrame(tick)
    }
    id = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(id)
  }, [playing, stopAtEnd, toUs, onTime])

  const at = (us: number) => `${Math.min(100, Math.max(0, ((us - fromUs) / span) * 100))}%`
  const fromPointer = (e: React.PointerEvent<HTMLDivElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    seekTo(fromUs + ((e.clientX - box.left) / box.width) * span)
  }

  return (
    <section className="player" aria-label={label}>
      {(title || meta) && (
        <div className="player-head">
          {title && <h3>{title}</h3>}
          {meta && <span>{meta}</span>}
        </div>
      )}
      <div className="video-frame">
        <video
          ref={ref}
          src={src}
          preload="metadata"
          aria-label={label}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onTimeUpdate={(e) => { if (!playing) setNow(e.currentTarget.currentTime * 1e6) }}
          onClick={() => { const v = ref.current; if (v) void (v.paused ? v.play() : v.pause()) }}
        />
      </div>
      <div className="player-controls">
        <button
          type="button"
          className="play-btn"
          aria-label={playing ? 'Пауза' : 'Воспроизвести'}
          aria-pressed={playing}
          onClick={() => { const v = ref.current; if (v) void (v.paused ? v.play() : v.pause()) }}
        />
        <span className="player-time">
          {fmtUs(now + offsetUs, 1)} <span>/ {fmtUs(end + offsetUs, 1)}</span>
        </span>
        <div
          className="scrub"
          role="slider"
          tabIndex={0}
          aria-label="Позиция"
          aria-valuemin={0}
          aria-valuemax={Math.round(span / 1000)}
          aria-valuenow={Math.round((now - fromUs) / 1000)}
          aria-valuetext={fmtUs(now + offsetUs, 1)}
          onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); fromPointer(e) }}
          onPointerMove={(e) => { if (e.buttons & 1) fromPointer(e) }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
              e.preventDefault()
              e.stopPropagation()
              seekTo(now + (e.key === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? 1_000_000 : 100_000))
            }
          }}
        >
          <span className="scrub-track" />
          <span className="scrub-fill" style={{ width: at(now) }} />
          {cut && <span className="scrub-cut" style={{ left: at(cut[0]), width: `calc(${at(cut[1])} - ${at(cut[0])})` }} />}
          {jointUs !== undefined && <span className="scrub-joint" style={{ left: at(jointUs) }} />}
          <span className="scrub-head" style={{ left: at(now) }} />
        </div>
      </div>
    </section>
  )
}

/** Прежний простой плеер: окно показа в исходнике, подпись — для чтения с экрана. */
export function Player({ src, startUs, endUs, label }: {
  src: string
  startUs: number
  endUs?: number
  label: string
}) {
  return <VideoPlayer src={src} label={label} fromUs={startUs} toUs={endUs} />
}

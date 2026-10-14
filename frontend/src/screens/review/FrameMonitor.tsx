// Окно «До» или «После» на экране проверки: кадр как в ролике — экран,
// лицо, раскладка, свои кадры, графика — тот же монитор, что на таймлайне
// (screens/Monitor.tsx, только кадр), в окне вокруг правки. Полоса — как у
// плеера: вырезанное пунктиром, черта стыка. Ничего не собирает: кадр
// рисуется в браузере по лёгким копиям.
import { useCallback, useImperativeHandle, useMemo, useRef, useState, type ReactNode, type Ref } from 'react'
import { ErrorText, type PlayerHandle } from '../../components/common'
import { api } from '../../lib/api'
import { syncedCamera } from '../../lib/camera'
import { whereInVersion } from '../../lib/edit'
import type { Change, FrameWindow } from '../../lib/frames'
import { useAsync } from '../../lib/hooks'
import { assetRef } from '../../lib/review'
import { fmtUs, frameToUs, usToFrame, type Rate } from '../../lib/time'
import type { Asset, Project, Revision } from '../../lib/types'
import { Monitor, type Player } from '../Monitor'

export function FrameMonitor({
  project, revisionId, rate, place, change, sourceAsset, title, meta, extra, label, emptyNote, handle,
}: {
  project: Project
  revisionId: string | null
  rate: Rate
  /** Где окно в этой версии; undefined — показывать в ней нечего */
  place: (rev: Revision) => FrameWindow | undefined
  change?: Change
  /** Время файла для seekTo (щелчок по слову в «Тексте вокруг»); без него — время версии */
  sourceAsset?: string
  title: ReactNode
  meta?: ReactNode
  /** Справа в заголовке: кнопка точного пробного ролика */
  extra?: ReactNode
  label: string
  /** Почему нечего показать (места в версии нет) */
  emptyNote: string
  handle?: Ref<PlayerHandle>
}) {
  const pid = project.id
  const data = useAsync(async () => {
    if (!revisionId) return undefined
    const [revision, joints, layer, drawn] = await Promise.all([
      api.revision(pid, revisionId), api.joints(pid, revisionId), api.visual(pid, revisionId), api.graphics(pid, revisionId),
    ])
    return { revision, joints: joints.map((j) => j.out_frame), layer, graphics: drawn.graphics }
  }, [pid, revisionId])
  const cameras = useMemo(
    () => Object.fromEntries(project.assets.map((a) => [a.id, syncedCamera(project.assets, a.id)])) as Record<string, Asset | undefined>,
    [project.assets],
  )
  const rev = data.data?.revision
  // Окно — один раз на загруженную версию: place у вызывающих — замыкание на
  // правку, а окно, меняющееся каждый кадр, перезапускало бы просмотр
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const win = useMemo(() => (rev ? place(rev) : undefined), [rev])

  // Правка выключена («До») или включена («После») — поверх того, что решил автор
  const kind = change?.kind
  const id = change?.id
  const on = change?.on
  const layer = useMemo(() => {
    const v = data.data?.layer
    if (!v || kind !== 'shot') return v
    return {
      ...v,
      segments: v.segments.map((s) => (s.id !== id ? s
        : { ...s, review: on ? (s.review === 'reverted' ? 'pending' as const : s.review) : 'reverted' as const })),
    }
  }, [data.data, kind, id, on])
  const graphics = useMemo(() => {
    const list = data.data?.graphics ?? []
    if (kind !== 'graphic') return list
    return list.map((g) => (g.id !== id ? g
      : { ...g, review: on ? (g.review === 'reverted' ? 'pending' as const : g.review) : 'reverted' as const }))
  }, [data.data, kind, id, on])

  const player = useRef<Player>(null)
  const [moved, setMoved] = useState<number>()
  const [playing, setPlaying] = useState(false)
  const ph = win ? Math.min(Math.max(moved ?? win.from, win.from), win.to - 1) : 0

  const seek = useCallback((frame: number) => {
    if (win) setMoved(Math.min(Math.max(Math.round(frame), win.from), win.to - 1))
  }, [win])
  const toggle = useCallback(() => {
    if (!win) return
    if (playing) {
      player.current?.pause()
      return
    }
    // С конца окна — заново с начала, как у плеера
    if (ph >= win.to - 1) setMoved(win.from)
    player.current?.play(1)
  }, [win, playing, ph])
  // Просмотр дошёл до конца окна — стоп
  const onTime = useCallback((frame: number) => {
    if (win && frame >= win.to - 1) {
      player.current?.pause()
      setMoved(win.to - 1)
      return
    }
    setMoved(frame)
  }, [win])

  useImperativeHandle(handle, () => ({
    toggle,
    play: () => { if (!playing) toggle() },
    pause: () => player.current?.pause(),
    seekBy: (us: number) => seek(ph + usToFrame(us, rate)),
    seekTo: (us: number) => {
      if (!rev) return
      const f = sourceAsset ? whereInVersion(rev, rate, sourceAsset, us)?.frame : usToFrame(us, rate)
      if (f !== undefined) seek(f)
    },
  }), [toggle, playing, seek, ph, rate, rev, sourceAsset])

  const head = (
    <div className="player-head">
      <h3>{title}</h3>
      <div className="rv-frame-side">
        {meta && <span>{meta}</span>}
        {extra}
      </div>
    </div>
  )
  if (!data.data || !rev || !win) {
    return (
      <section className="player" aria-label={label}>
        {head}
        <div className="video-frame pending" data-state={data.error ? 'error' : undefined}>
          {data.error ? <ErrorText error={data.error} />
            : !revisionId || (rev && !win) ? <p>{emptyNote}</p>
            : <p>Собираем кадр…</p>}
        </div>
      </section>
    )
  }

  const span = Math.max(1, win.to - win.from)
  const at = (f: number) => `${Math.min(100, Math.max(0, ((f - win.from) / span) * 100))}%`
  const fromPointer = (e: React.PointerEvent<HTMLDivElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    seek(win.from + ((e.clientX - box.left) / box.width) * span)
  }
  const clip = rev.clips.find((c) => c.out_start_frame <= ph && ph < c.out_end_frame)

  return (
    <section className="player" aria-label={label}>
      {head}
      <Monitor
        project={project}
        revision={rev}
        rate={rate}
        playhead={ph}
        onTime={onTime}
        handle={player}
        layer={layer}
        graphics={graphics}
        joints={data.data.joints}
        cameras={cameras}
        editable={false}
        onChanged={data.reload}
        bare
        onPlaying={setPlaying}
      />
      <div className="player-controls">
        <button type="button" className="play-btn" aria-label={playing ? 'Пауза' : 'Воспроизвести'} aria-pressed={playing}
          onClick={toggle} />
        <span className="player-time">
          {fmtUs(frameToUs(ph, rate), 1)} <span>/ {fmtUs(frameToUs(win.to, rate), 1)}</span>
        </span>
        {clip && (
          <span className="mono faint tl-src">
            {assetRef(project.assets, clip.asset_id)} {fmtUs(clip.src_start_us + frameToUs(ph - clip.out_start_frame, rate), 1)}
          </span>
        )}
        <div
          className="scrub"
          role="slider"
          tabIndex={0}
          aria-label="Позиция"
          aria-valuemin={win.from}
          aria-valuemax={win.to - 1}
          aria-valuenow={ph}
          aria-valuetext={fmtUs(frameToUs(ph, rate), 1)}
          onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); fromPointer(e) }}
          onPointerMove={(e) => { if (e.buttons & 1) fromPointer(e) }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
              e.preventDefault()
              e.stopPropagation()
              seek(ph + (e.key === 'ArrowRight' ? 1 : -1) * usToFrame(e.shiftKey ? 1_000_000 : 100_000, rate))
            }
          }}
        >
          <span className="scrub-track" />
          <span className="scrub-fill" style={{ width: at(ph) }} />
          {win.mark && <span className="scrub-cut" style={{ left: at(win.mark[0]), width: `calc(${at(win.mark[1])} - ${at(win.mark[0])})` }} />}
          {win.joint !== undefined && <span className="scrub-joint" style={{ left: at(win.joint) }} />}
          <span className="scrub-head" style={{ left: at(ph) }} />
        </div>
      </div>
    </section>
  )
}

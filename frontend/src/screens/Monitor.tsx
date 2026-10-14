// Монитор программы, как в Premiere Pro (устройство — docs/tech.md, «Монитор программы»):
// кадр собирается так же, как в готовом ролике — экран, лицо, раскладки, свои
// кадры, видео-фон (lib/layout.ts повторяет размеры render/compose.py), графика
// поверх — та же HTML-страница, что рисуется в ролик (graphics/preview.html),
// промотанная на момент курсора. Окно лица, рамку экрана и графику можно
// двигать мышью и менять размер за угол (Motion в Premiere) — сохраняется в
// участок кадра под курсором, в раскладку проекта или в графику.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import { cameraUs } from '../lib/camera'
import { whereInVersion } from '../lib/edit'
import { graphicTitle } from '../lib/layer'
import { cameraScale, coverCrop, graphicsAt, pipGeometry, sideGeometry, specAt, type Box, type Spec } from '../lib/layout'
import { assetRef } from '../lib/review'
import { fmtUs, frameToUs, usToFrame, type Rate } from '../lib/time'
import type { Asset, GraphicItem, Project, Revision, VisualLayer } from '../lib/types'

const W = 1280
const H = 720
const PAGE_TEMPLATES = ['page_insert']

export type Player = { toggle: () => void; play: (rate: number) => void; pause: () => void }
type Layer = 'face' | 'card' | 'graphic'
type Place = { x: number; y: number; scale: number }
type Drag = { layer: Layer; mode: 'move' | 'size'; x0: number; y0: number; box: Box; place?: Place }

/** Кадр ролика -> кусок версии и время файла в нём. */
function sourceAt(rev: Revision, rate: Rate, frame: number) {
  const index = rev.clips.findIndex((c) => c.out_start_frame <= frame && frame < c.out_end_frame)
  if (index < 0) return undefined
  const clip = rev.clips[index]
  return { index, clip, us: clip.src_start_us + frameToUs(frame - clip.out_start_frame, rate) }
}

function roundRect(ctx: CanvasRenderingContext2D, [x, y, w, h]: Box, r: number) {
  ctx.beginPath()
  ctx.roundRect(x, y, w, h, r)
}

export function Monitor({ project, revision, rate, playhead, onTime, handle, layer, graphics, joints, cameras, editable, onChanged, bare, onPlaying }: {
  project: Project
  revision: Revision
  rate: Rate
  playhead: number
  onTime: (frame: number) => void
  handle: React.RefObject<Player | null>
  layer?: VisualLayer
  graphics: GraphicItem[]
  joints: number[]
  cameras: Record<string, Asset | undefined>
  /** Двигать можно только в текущей версии */
  editable: boolean
  /** Участок или графика поправлены — обновить слой кадра и графику */
  onChanged: () => void
  /** Только кадр, без заголовка и кнопок: свои полоса и кнопки — у того, кто
   *  вставил (окна «До» и «После» на экране проверки); щелчок по кадру — пуск и пауза */
  bare?: boolean
  /** Просмотр пошёл или встал — для кнопки ▶ того, кто вставил монитор без кнопок */
  onPlaying?: (on: boolean) => void
}) {
  const pid = project.id
  const fps = rate.num / rate.den
  const canvas = useRef<HTMLCanvasElement>(null)
  const screen = useRef<HTMLVideoElement>(null)
  const cam = useRef<HTMLVideoElement>(null)
  const broll = useRef<HTMLVideoElement>(null)
  const bg = useRef<HTMLVideoElement>(null)
  const frames = useRef<Map<string, HTMLIFrameElement>>(new Map())
  const [playing, setPlaying] = useState(false)
  const playingRef = useRef(false)
  const frameRef = useRef(playhead)
  // Курсор, который пришёл снаружи, и тот, что просмотр сообщил сам: разные —
  // значит, курсор передвинули (тянут мышью по таймлайну) во время просмотра
  const playheadRef = useRef(playhead)
  // До кадра просмотра: цикл просмотра должен видеть новый курсор раньше, чем затрёт его своим
  useLayoutEffect(() => { playheadRef.current = playhead }, [playhead])
  const reported = useRef(playhead)
  const [selected, setSelected] = useState<Layer>()
  const [draft, setDraftState] = useState<{ face?: Spec['face']; card?: Spec['card']; place?: Place }>({})
  // Последнее положение — сразу и в ref: мышь отпустили раньше, чем React перерисовал
  const draftRef = useRef(draft)
  const setDraft = useCallback((next: (cur: typeof draft) => typeof draft) => {
    draftRef.current = next(draftRef.current)
    setDraftState(draftRef.current)
  }, [])
  const drag = useRef<Drag | null>(null)
  const [error, setError] = useState<string>()
  const [pages, setPages] = useState<Record<string, { width: number; height: number; bg: string }>>({})
  // Размеры кадров экрана и камеры — для рамок, которые тянут мышью
  const [dims, setDims] = useState({ sw: 0, sh: 0, cw: 0, ch: 0 })

  const at = sourceAt(revision, rate, playhead)
  const camera = at ? cameras[at.clip.asset_id] : undefined
  const here = useMemo(() => specAt(layer, playhead), [layer, playhead])
  const needsCamera = ['camera', 'pip', 'side'].includes(here.spec.layout)
  const spec: Spec = useMemo(() => {
    const base = needsCamera && !camera ? { layout: 'screen' as const } : here.spec
    return { ...base, ...(draft.face ? { face: draft.face } : {}), ...(draft.card ? { card: draft.card } : {}) }
  }, [here.spec, needsCamera, camera, draft])
  const shown = useMemo(() => graphicsAt(graphics, playhead, fps), [graphics, playhead, fps])
  const graphic = shown[0]?.item

  // Снимки страниц — для вставок страниц на мониторе
  useEffect(() => {
    if (!graphics.some((g) => PAGE_TEMPLATES.includes(g.template))) return
    api.pages(pid).then((list) => setPages(Object.fromEntries(list.map((p) => [p.id, p]))), () => undefined)
  }, [pid, graphics])

  // Новая точка — черновик положения сбрасывается, выбранный слой — если его тут нет
  useEffect(() => {
    setDraft(() => ({}))
  }, [here.segment?.id, graphic?.id, layer, graphics, setDraft])

  const draw = useCallback(() => {
    const c = canvas.current
    const ctx = c?.getContext('2d')
    if (!c || !ctx) return
    const frame = frameRef.current
    const s = screen.current
    const v = cam.current
    ctx.fillStyle = '#000'
    ctx.fillRect(0, 0, W, H)
    const ready = (el: HTMLVideoElement | null): el is HTMLVideoElement => !!el && el.readyState >= 2 && el.videoWidth > 0
    const fit = (el: HTMLVideoElement, box: Box, crop?: { x: number; y: number; w: number; h: number }) => {
      const sx = (crop?.x ?? 0) * el.videoWidth
      const sy = (crop?.y ?? 0) * el.videoHeight
      const sw = (crop?.w ?? 1) * el.videoWidth
      const sh = (crop?.h ?? 1) * el.videoHeight
      const k = Math.min(box[2] / sw, box[3] / sh)
      const dw = sw * k
      const dh = sh * k
      ctx.drawImage(el, sx, sy, sw, sh, box[0] + (box[2] - dw) / 2, box[1] + (box[3] - dh) / 2, dw, dh)
    }
    const cover = (el: HTMLVideoElement, box: Box, scale = 1, cx = 0.5, cy = 0.5) => {
      const k = coverCrop(el.videoWidth, el.videoHeight, box[2], box[3], scale, cx, cy)
      ctx.drawImage(el, k.sx, k.sy, k.sw, k.sh, box[0], box[1], box[2], box[3])
    }
    const lay = spec.layout
    if (lay === 'broll' && ready(broll.current)) cover(broll.current, [0, 0, W, H])
    else if (lay === 'camera' && ready(v)) {
      const m = cameraScale(spec, here.range, joints, frame, fps)
      cover(v, [0, 0, W, H], m.scale, m.cx, m.cy)
    } else if (lay === 'side' && ready(s)) {
      const g = sideGeometry(spec, W, H, s.videoWidth, s.videoHeight)
      if (spec.bg && ready(bg.current)) cover(bg.current, [0, 0, W, H])
      else {
        // Фон, как в сборке: почти чёрный с еле заметной размытой сеткой
        ctx.fillStyle = '#1b1b1b'
        ctx.fillRect(0, 0, W, H)
        ctx.strokeStyle = 'rgba(255,255,255,0.035)'
        ctx.lineWidth = 2
        const cell = Math.round(W / 24)
        for (let x = 0; x < W; x += cell) ctx.strokeRect(x, -2, cell, H + 4)
        for (let y = 0; y < H; y += cell) ctx.strokeRect(-2, y, W + 4, cell)
      }
      let drop = 0
      if (spec.drop && here.range) {
        const n = frame - here.range[0]
        const len = here.range[1] - here.range[0]
        const k = Math.max(1, Math.min(Math.round(0.4 * fps), Math.floor(len / 2)))
        const q = Math.min(1, n / k, (len - 1 - n) / k)
        drop = spec.drop * H * q * q * (3 - 2 * q)
      }
      const card: Box = [g.card[0], g.card[1] + drop, g.card[2], g.card[3]]
      ctx.save()
      roundRect(ctx, card, g.radius)
      ctx.clip()
      const r = spec.region ?? { x: 0, y: 0, w: 1, h: 1 }
      ctx.drawImage(s, r.x * s.videoWidth, r.y * s.videoHeight, r.w * s.videoWidth, r.h * s.videoHeight, ...card)
      ctx.restore()
      ctx.fillStyle = '#fff'
      ctx.fillRect(...g.face)
      if (ready(v)) {
        const side = v.videoHeight
        const sx = Math.max(0, Math.min(v.videoWidth - side, v.videoWidth * (spec.face_cx ?? 0.5) - side / 2))
        const b = g.border
        ctx.drawImage(v, sx, 0, side, side, g.face[0] + b, g.face[1] + b, g.face[2] - 2 * b, g.face[3] - 2 * b)
      }
    } else if (ready(s)) {
      if (lay === 'screen' && spec.zoom) fit(s, [0, 0, W, H], spec.zoom)
      else fit(s, [0, 0, W, H])
      if (lay === 'pip' && ready(v)) {
        const box = pipGeometry(spec, W, H, v.videoWidth, v.videoHeight)
        const p = spec.punch
        cover(v, box, p?.scale ?? 1, p?.cx ?? 0.5, p?.cy ?? 0.5)
      }
    }
    // Графика поверх: промотать каждое окно на свой момент
    for (const { item, ms } of graphicsAt(graphics, frame, fps)) {
      frames.current.get(item.id)?.contentWindow?.postMessage({ t: ms }, location.origin)
    }
  }, [spec, here.range, joints, fps, graphics])

  // Пауза: курсор передвинули — поставить видео на этот момент и нарисовать
  useEffect(() => {
    frameRef.current = playhead
    if (playingRef.current) return
    const s = screen.current
    if (s && at) {
      const t = at.us / 1e6
      if (Math.abs(s.currentTime - t) > 0.5 / fps) s.currentTime = t
      const v = cam.current
      const ct = camera ? cameraUs(camera, at.us) : undefined
      if (v && ct !== undefined && Math.abs(v.currentTime - ct / 1e6) > 0.5 / fps) v.currentTime = ct / 1e6
    }
    if (here.spec.layout === 'broll' && broll.current && here.range) {
      broll.current.currentTime = (here.spec.at ?? 0) + (playhead - here.range[0]) / fps
    }
    if (here.spec.bg && bg.current && bg.current.duration) bg.current.currentTime = (playhead / fps) % bg.current.duration
    draw()
    // playing — и в миг остановки: видео встаёт ровно под курсор
  }, [playhead, at, camera, fps, here, draw, playing])

  // Просмотр: запись экрана ведёт время, камера держится рядом по сведению;
  // на конце куска — следующий кусок версии
  useEffect(() => {
    if (!playing) return
    let raf = 0
    // Какая запись сейчас играет: курсор утащили на другую — её видео ещё не загружено
    let played = sourceAt(revision, rate, frameRef.current)?.clip.asset_id
    const tick = () => {
      const s = screen.current
      if (playheadRef.current !== reported.current) {
        // Курсор передвинули во время просмотра — перескочить туда и играть дальше
        const to = sourceAt(revision, rate, playheadRef.current)
        if (!s || !to || to.clip.asset_id !== played) {
          // Другая запись: её видео ещё не загружено — остановиться на новом месте
          handle.current?.pause()
          return
        }
        s.currentTime = to.us / 1e6
        frameRef.current = playheadRef.current
        reported.current = playheadRef.current
      }
      let cur = sourceAt(revision, rate, frameRef.current)
      if (s && cur) {
        let us = s.currentTime * 1e6
        const slack = 1e6 / fps
        if (us < cur.clip.src_start_us - slack || us > cur.clip.src_end_us + slack) {
          // Видео не в куске, где курсор: версия поменялась — например, вырезали
          // кусок, где идёт просмотр. Продолжить с того же места записи, а
          // вырезанное перепрыгнуть к следующему оставленному куску
          const w = whereInVersion(revision, rate, cur.clip.asset_id, us)
          if (!w) {
            s.pause()
            return
          }
          if (w.jumped) s.currentTime = w.us / 1e6
          us = w.us
          frameRef.current = w.frame
          cur = sourceAt(revision, rate, w.frame) ?? cur
        } else if (us >= cur.clip.src_end_us - 1000) {
          const next = revision.clips[cur.index + 1]
          if (!next) {
            s.pause()
            return
          }
          if (next.asset_id === cur.clip.asset_id) s.currentTime = next.src_start_us / 1e6
          frameRef.current = next.out_start_frame
        } else {
          frameRef.current = cur.clip.out_start_frame + Math.max(0, usToFrame(us - cur.clip.src_start_us, rate))
        }
        const v = cam.current
        const camNow = cameras[cur.clip.asset_id]
        const ct = camNow ? cameraUs(camNow, Math.min(us, cur.clip.src_end_us)) : undefined
        if (v && ct !== undefined && Math.abs(v.currentTime - ct / 1e6) > 0.12) v.currentTime = ct / 1e6
        played = cur.clip.asset_id
        reported.current = frameRef.current
        onTime(frameRef.current)
        draw()
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing, revision, rate, fps, cameras, onTime, draw, handle])

  const onPlayingRef = useRef(onPlaying)
  useLayoutEffect(() => { onPlayingRef.current = onPlaying })
  const setRunning = useCallback((on: boolean, speed = 1) => {
    const all = [screen.current, cam.current, broll.current, bg.current].filter(Boolean) as HTMLVideoElement[]
    for (const el of all) {
      el.playbackRate = speed
      if (on) void el.play().catch(() => undefined)
      else el.pause()
    }
    playingRef.current = on
    setPlaying(on)
    onPlayingRef.current?.(on)
  }, [])
  useEffect(() => {
    handle.current = {
      toggle: () => setRunning(!playingRef.current),
      play: (speed: number) => setRunning(true, speed),
      pause: () => setRunning(false),
    }
  }, [handle, setRunning])

  const register = useCallback((id: string, f: HTMLIFrameElement | null) => {
    if (f) frames.current.set(id, f)
    else frames.current.delete(id)
  }, [])

  // Звук ролика: с камеры, если выбран её звук, иначе запись экрана
  const camSound = project.audio_source === 'camera' && !!camera

  // ---------- двигать мышью (Motion) ----------

  const box = useCallback((l: Layer): Box | undefined => {
    if (l === 'graphic') {
      const p = draft.place ?? (graphic?.props.place as Place | undefined) ?? { x: 0, y: 0, scale: 1 }
      const w = W * p.scale
      const h = H * p.scale
      return [W / 2 + p.x * W - w / 2, H / 2 + p.y * H - h / 2, w, h]
    }
    if (spec.layout === 'side' && dims.sw) {
      const g = sideGeometry(spec, W, H, dims.sw, dims.sh)
      return l === 'face' ? g.face : g.card
    }
    if (spec.layout === 'pip' && l === 'face' && dims.cw) return pipGeometry(spec, W, H, dims.cw, dims.ch)
    return undefined
  }, [spec, graphic, draft, dims])

  const layers: Layer[] = [
    ...(spec.layout === 'side' || spec.layout === 'pip' ? ['face' as const] : []),
    ...(spec.layout === 'side' ? ['card' as const] : []),
    ...(graphic ? ['graphic' as const] : []),
  ]
  const active = selected && layers.includes(selected) ? selected : undefined

  const startDrag = (e: React.PointerEvent, l: Layer, mode: 'move' | 'size') => {
    if (!editable) return
    const b = box(l)
    if (!b) return
    e.stopPropagation()
    e.currentTarget.setPointerCapture(e.pointerId)
    setSelected(l)
    const place = l === 'graphic' ? draft.place ?? (graphic?.props.place as Place | undefined) ?? { x: 0, y: 0, scale: 1 } : undefined
    drag.current = { layer: l, mode, x0: e.clientX, y0: e.clientY, box: b, place }
  }
  const moveDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d) return
    const k = W / e.currentTarget.getBoundingClientRect().width
    const dx = (e.clientX - d.x0) * k
    const dy = (e.clientY - d.y0) * k
    const [x, y, w, h] = d.box
    const r4 = (n: number) => Math.round(n * 10000) / 10000
    // Лицо и экран не уходят за край кадра
    const nx = (bw: number) => Math.max(0, Math.min(W - bw, d.mode === 'move' ? x + dx : x)) / W
    const ny = (bh: number) => Math.max(0, Math.min(H - bh, d.mode === 'move' ? y + dy : y)) / H
    if (d.layer === 'graphic' && d.place) {
      const p = d.mode === 'move'
        ? { ...d.place, x: r4(d.place.x + dx / W), y: r4(d.place.y + dy / H) }
        : { ...d.place, scale: r4(Math.max(0.3, Math.min(3, d.place.scale * (w + dx) / w))) }
      setDraft((cur) => ({ ...cur, place: p }))
    } else if (d.layer === 'face') {
      const size = d.mode === 'size' ? Math.max(0.05, Math.min(0.9, (W - x) / W, (w + dx) / W)) : w / W
      const fh = (h * size * W) / w
      setDraft((cur) => ({ ...cur, face: { x: r4(nx(size * W)), y: r4(ny(fh)), size: r4(size) } }))
    } else {
      const cw = d.mode === 'size' ? Math.max(0.1, Math.min(1, (W - x) / W, (w + dx) / W)) : w / W
      const ch = (h * cw * W) / w
      setDraft((cur) => ({ ...cur, card: { x: r4(nx(cw * W)), y: r4(ny(ch)), w: r4(cw) } }))
    }
  }
  /** Сохранить положение слоя (или вернуть на место): графика — в её поля,
   *  лицо и экран — в участок кадра под курсором или в раскладку проекта. */
  const save = useCallback(async (l: Layer, reset = false) => {
    const draft = draftRef.current
    try {
      setError(undefined)
      if (l === 'graphic') {
        if (!graphic) return
        const props = { ...graphic.props }
        if (reset) delete props.place
        else if (draft.place) props.place = draft.place
        await api.patchGraphic(pid, graphic.id, props)
      } else {
        const { layout, ...params } = here.spec
        if (l === 'face') {
          if (reset) delete params.face
          else if (draft.face) params.face = draft.face
        } else if (reset) delete params.card
        else if (draft.card) params.card = draft.card
        if (here.segment) await api.patchVisual(pid, here.segment.id, params)
        else await api.setLayout(pid, layout, params)
      }
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [graphic, here, pid, onChanged])
  const endDrag = () => {
    const d = drag.current
    if (!d) return
    drag.current = null
    void save(d.layer)
  }

  const k = (n: number) => `${(n / W) * 100}%`
  const kh = (n: number) => `${(n / H) * 100}%`
  const where = here.segment ? 'в этом участке кадра' : 'для всего проекта (своего участка здесь нет)'

  const frame = (
    <div className="video-frame mon-frame">
      <canvas ref={canvas} width={W} height={H} className="mon-canvas"
        onClick={() => (bare ? setRunning(!playingRef.current) : setSelected(undefined))} />
      {shown.map(({ item, total }) => (
        <GraphicFrame key={item.id} pid={pid} item={item} totalMs={total} pages={pages}
          place={item.id === graphic?.id ? draft.place : undefined} register={register} onReady={draw} />
      ))}
      {editable && layers.length > 0 && (
        <div className="mon-edit" onPointerMove={moveDrag} onPointerUp={endDrag}
          onPointerDown={(e) => { if (e.target === e.currentTarget) setSelected(undefined) }}>
          {/* Снизу вверх: экран, лицо; выбранный кнопкой слой — поверх всех. Графика
              (рамка во весь кадр) — только когда выбрана */}
          {(['card', 'face', 'graphic'] as const)
            .filter((l) => layers.includes(l) && (l !== 'graphic' || active === 'graphic'))
            .sort((a, b) => Number(a === active) - Number(b === active))
            .map((l) => {
            const b = box(l)
            if (!b) return null
            return (
              <div key={l} className="mon-box" data-layer={l} data-active={active === l ? '' : undefined}
                style={{ left: k(b[0]), top: kh(b[1]), width: k(b[2]), height: kh(b[3]) }}
                onPointerDown={(e) => startDrag(e, l, 'move')}>
                <span className="mon-tag">{l === 'face' ? 'Лицо' : l === 'card' ? 'Экран' : graphicTitle(graphic!.template, graphic!.props)}</span>
                <i className="mon-size" onPointerDown={(e) => startDrag(e, l, 'size')} aria-label="Размер" />
              </div>
            )
          })}
        </div>
      )}
      <video ref={screen} className="mon-src" src={at ? api.proxyUrl(pid, at.clip.asset_id) : undefined} preload="auto"
        muted={camSound} playsInline onLoadedData={draw} onSeeked={draw}
        onLoadedMetadata={(e) => { const v = e.currentTarget; setDims((d) => ({ ...d, sw: v.videoWidth, sh: v.videoHeight })) }} />
      <video ref={cam} className="mon-src" src={camera ? api.proxyUrl(pid, camera.id) : undefined} preload="auto"
        muted={!camSound} playsInline onLoadedData={draw} onSeeked={draw}
        onLoadedMetadata={(e) => { const v = e.currentTarget; setDims((d) => ({ ...d, cw: v.videoWidth, ch: v.videoHeight })) }} />
      <video ref={broll} className="mon-src" src={spec.layout === 'broll' && spec.clip ? api.brollUrl(pid, spec.clip) : undefined}
        preload="auto" muted playsInline onLoadedData={draw} onSeeked={draw} />
      <video ref={bg} className="mon-src" src={spec.bg ? api.backgroundUrl(pid, spec.bg) : undefined}
        preload="auto" muted loop playsInline onLoadedData={draw} onSeeked={draw} />
    </div>
  )
  if (bare) return frame

  return (
    <section className="player tl-player mon" aria-labelledby="tl-pv">
      <div className="tl-player-head">
        <h2 id="tl-pv" className="block-title">Монитор программы</h2>
        {editable && layers.length > 0 ? (
          <div role="group" aria-label="Что двигать" className="mon-layers">
            {layers.map((l) => (
              <button key={l} type="button" className="btn btn-quiet btn-sm" aria-pressed={active === l}
                onClick={() => setSelected(active === l ? undefined : l)}>
                {l === 'face' ? 'Лицо' : l === 'card' ? 'Экран' : 'Графика'}
              </button>
            ))}
          </div>
        ) : <span className="chip sm" data-state="queued">кадр как в ролике, по лёгким копиям</span>}
      </div>
      {frame}
      <div className="player-controls">
        <button type="button" className="play-btn" aria-label={playing ? 'Пауза' : 'Воспроизвести'} aria-pressed={playing}
          onClick={() => setRunning(!playingRef.current)} />
        <span className="player-time">{fmtUs(frameToUs(playhead, rate), 1)} <span>/ {fmtUs(revision.duration_us, 1)}</span></span>
        {at && <span className="mono faint tl-src">{assetRef(project.assets, at.clip.asset_id)} {fmtUs(at.us, 1)}</span>}
        {/* Текст графики — в этой же строке, не отдельной под ней: отдельная появлялась
            и пропадала с курсором и двигала таймлайн вниз (просьба автора 13.10) */}
        {graphic && editable && !active && <GraphicFields key={graphic.id} item={graphic} onSave={async (props) => {
          try {
            await api.patchGraphic(pid, graphic.id, props)
            onChanged()
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e))
          }
        }} />}
        {active && editable && (
          <span className="mon-hint">
            {active === 'graphic' ? 'Графика — тяните, угол — размер' : `${active === 'face' ? 'Лицо' : 'Экран'} — ${where}`}
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => void save(active, true)}>вернуть на место</button>
          </span>
        )}
      </div>
      {error && <p className="error-text" role="alert">{error}</p>}
    </section>
  )
}

/** Окно графики поверх монитора: собирает страницу шаблона и проматывается монитором. */
function GraphicFrame({ pid, item, totalMs, pages, place, register, onReady }: {
  pid: string
  item: GraphicItem
  totalMs: number
  pages: Record<string, { width: number; height: number; bg: string }>
  place?: Place
  register: (id: string, f: HTMLIFrameElement | null) => void
  onReady: () => void
}) {
  const ref = useRef<HTMLIFrameElement>(null)
  const [loaded, setLoaded] = useState(false)
  const ready = useRef(onReady)
  ready.current = onReady
  useEffect(() => {
    const f = ref.current
    register(item.id, f)
    const listen = (e: MessageEvent) => {
      if (e.source !== f?.contentWindow) return
      if (e.data?.loaded) setLoaded(true)
      if (e.data?.ready) ready.current()
    }
    window.addEventListener('message', listen)
    return () => {
      window.removeEventListener('message', listen)
      register(item.id, null)
    }
  }, [item.id, register])
  useEffect(() => {
    if (!loaded) return
    const props: Record<string, unknown> = { ...item.props, ...(place ? { place } : {}) }
    if (PAGE_TEMPLATES.includes(item.template)) {
      const pg = pages[String(item.props.page)]
      if (!pg) return
      Object.assign(props, { src: `/api/projects/${pid}/pages/${item.props.page}/preview?width=1600`, w: pg.width, h: pg.height, bg: pg.bg })
    }
    ref.current?.contentWindow?.postMessage({ build: { template: item.template, props, durationMs: totalMs } }, location.origin)
  }, [loaded, item, place, totalMs, pages, pid])
  return <iframe ref={ref} className="mon-graphic" src="/graphics/preview.html" title={item.template} aria-hidden="true" tabIndex={-1} />
}

/** Поля графики — как Essential Graphics: текст правится здесь, в ролик идёт сразу. */
function GraphicFields({ item, onSave }: { item: GraphicItem; onSave: (props: Record<string, unknown>) => Promise<void> }) {
  const keys = Object.entries(item.props).filter(([key, v]) => typeof v === 'string' && !['page', 'icon', 'color', 'tone', 'side', 'position', 'trend', 'kind', 'enter', 'motion'].includes(key))
  const [values, setValues] = useState<Record<string, string>>(Object.fromEntries(keys.map(([key, v]) => [key, String(v)])))
  if (!keys.length) return null
  const changed = keys.some(([key, v]) => values[key] !== v)
  return (
    <form className="mon-fields" onSubmit={(e) => { e.preventDefault(); void onSave({ ...item.props, ...values }) }}>
      <span className="faint">Текст графики</span>
      {keys.map(([key]) => (
        <input key={key} type="text" value={values[key]} aria-label={key} onChange={(e) => setValues((cur) => ({ ...cur, [key]: e.target.value }))} />
      ))}
      <button type="submit" className="btn btn-sm" disabled={!changed}>Сохранить</button>
    </form>
  )
}

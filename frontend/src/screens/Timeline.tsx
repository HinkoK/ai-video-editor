// Таймлайн по макету автора: черновой просмотр, метки и комментарии, вся
// версия на одном холсте (canvas) — шкала времени, дорожка графики (сверху,
// как V3 в Premiere: каждая графика — блок с подписью), дорожки камеры и
// экрана (полосы кадров из лёгких копий; камера над экраном, по времени
// сведения со звуком экрана), звук ролика по кускам версии, стыки,
// комментарии, метки и курсор. Высоту дорожек автор меняет, потянув за нижний
// край (запоминается в браузере). Один элемент страницы на весь час, а не по
// элементу на кадр.
// Ручной монтаж как в Premiere Pro (lib/edit.ts): лезвие, выделение кусков,
// Q/W, метки I/O, удаление со сдвигом, отмена — теми же клавишами;
// перестановка — ⌘X «взять кусок», ⌘⇧V «вставить со сдвигом».
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Tab } from '../App'
import { ErrorText, Select, StatePanel } from '../components/common'
import { api, ApiError } from '../lib/api'
import { useAsync, usePref } from '../lib/hooks'
import { cameraUs, syncedCamera } from '../lib/camera'
import { blockOf, editPoints, keyAction, mergeRanges, moveOp, nextPoint, pieceAt, prevPoint, rangeOps, shuttle, trimToNext, trimToPrev, type Block } from '../lib/edit'
import { graphicTitle } from '../lib/layer'
import { assetRef } from '../lib/review'
import { GAP, MARKS, RULER, clampHeight, edgeAt, laneAt, layoutLanes, neededHeight, parseHeights, type TrackHeights } from '../lib/tracks'
import { Monitor, type Player } from './Monitor'
import { fmtUs, frameToUs, parseRate, usToFrame, type Rate } from '../lib/time'
import type { Asset, Joint, Project, Revision, RevisionSummary, Thumbs, Waveform } from '../lib/types'

/** Полоса кадров файла: готова, нет лёгкой копии, считается или ошибка. */
type Strip =
  | { state: 'ready'; meta: Thumbs; image: HTMLImageElement }
  | { state: 'loading' | 'no_proxy' | 'error' }

function revisionLabel(r: RevisionSummary, current: string | null): string {
  if (r.id === current) return `${r.number} · текущая`
  if (r.state === 'candidate') return `${r.number} · предложение`
  if (r.state === 'discarded') return `${r.number} · отклонена`
  return String(r.number)
}


function isTyping(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
}

export function Timeline({ project, onOpenTab, onChanged }: {
  project: Project
  onOpenTab?: (tab: Tab) => void
  /** Версия изменилась (ручной вырез, отмена) — обновить проект */
  onChanged?: () => void
}) {
  const pid = project.id
  const current = project.current_revision_id
  const revisions = useAsync(() => api.revisions(pid), [pid, current])
  const [chosen, setChosen] = useState<string>()
  const rid = chosen && revisions.data?.some((r) => r.id === chosen) ? chosen : current
  const revision = useAsync(() => (rid ? api.revision(pid, rid) : Promise.resolve(undefined)), [pid, rid])
  const joints = useAsync(() => (rid ? api.joints(pid, rid) : Promise.resolve([] as Joint[])), [pid, rid])
  // Волна каждого файла целиком, до 5000 точек: для обзора хватает
  const waves = useAsync(
    () => Promise.all(project.assets.map((a) =>
      api.waveform(pid, a.id, 0, a.duration_us, 5000).then((w) => [a.id, w] as const, () => [a.id, undefined] as const),
    )).then((pairs) => Object.fromEntries(pairs) as Record<string, Waveform | undefined>),
    [pid, project.assets.length],
  )
  const strips = useStrips(pid, project.assets.map((a) => a.id))
  // Камера каждой записи экрана — дорожка над экраном; звук ролика — её, если выбран звук камеры
  const cameras = useMemo(
    () => Object.fromEntries(project.assets.map((a) => [a.id, syncedCamera(project.assets, a.id)])) as Record<string, Asset | undefined>,
    [project.assets],
  )
  const comments = useAsync(() => (rid ? api.comments(pid, rid) : Promise.resolve([])), [pid, rid])
  const markers = useAsync(() => (rid ? api.markers(pid, rid) : Promise.resolve([])), [pid, rid])
  // Слой кадра: где лицо целиком, приближение экрана, экран без лица
  const layer = useAsync(() => (rid ? api.visual(pid, rid) : Promise.resolve(undefined)), [pid, rid])
  const drawn = useAsync(() => (rid ? api.graphics(pid, rid) : Promise.resolve(undefined)), [pid, rid])
  // Высоты дорожек — настройка браузера, как последний проект
  const [tracksPref, setTracksPref] = usePref<string>('longform.tracks', '')
  const heights = useMemo(() => parseHeights(tracksPref), [tracksPref])
  const [playhead, setPlayhead] = useState(0)
  const [view, setView] = useState<[number, number]>()
  const [error, setError] = useState<unknown>()
  const player = useRef<Player>(null)
  // Ручной монтаж (как в Premiere): инструмент, надрезы лезвием, выделенные
  // куски, метки входа и выхода, скорость J/L, отмена и возврат версий
  const [tool, setTool] = useState<'select' | 'razor'>('select')
  const [cuts, setCuts] = useState<number[]>([])
  const [picked, setPicked] = useState<[number, number][]>([])
  const [inPt, setInPt] = useState<number>()
  const [outPt, setOutPt] = useState<number>()
  const [busy, setBusy] = useState(false)
  const [busyText, setBusyText] = useState('Режу…')
  const [note, setNote] = useState<string>()
  // ⌘X: взятый кусок записи (версия не меняется) и где он был в ролике
  const [taken, setTaken] = useState<{ block: Block; frames: [number, number]; revId: string }>()
  const [speed, setSpeed] = useState(0)
  const undoStack = useRef<string[]>([])
  const redoStack = useRef<string[]>([])

  const rev = revision.data
  const rate = useMemo(() => (rev ? parseRate(rev.fps) : undefined), [rev])
  const points = useMemo(
    () => (rev ? editPoints(rev.total_frames, (joints.data ?? []).map((j) => j.out_frame), cuts) : []),
    [rev, joints.data, cuts],
  )
  // Новая версия — надрезы, выделение и метки относились к старой
  useEffect(() => {
    setCuts([])
    setPicked([])
    setInPt(undefined)
    setOutPt(undefined)
  }, [rev?.id])

  // J — назад: браузер не умеет играть видео задом наперёд, поэтому курсор
  // шагает назад десять раз в секунду, а просмотр показывает кадр под ним
  useEffect(() => {
    if (speed >= 0 || !rate) return
    const step = Math.max(1, Math.round((rate.num / rate.den) * 0.1 * -speed))
    const t = window.setInterval(() => setPlayhead((f) => {
      if (f - step <= 0) setSpeed(0)
      return Math.max(0, f - step)
    }), 100)
    return () => window.clearInterval(t)
  }, [speed, rate])

  /** Вырезать отрезки ролика (кадры): правка от автора и сразу утверждение —
   *  новая версия, всё после выреза сдвигается (ripple). */
  const cut = useCallback(async (ranges: [number, number][], label: string) => {
    if (!rev || !rate || busy) return
    if (rev.id !== current) {
      setNote('Резать можно только текущую версию — выберите её в списке «Версия»')
      return
    }
    const merged = mergeRanges(ranges)
    const ops = merged.flatMap(([a, b]) => rangeOps(rev, rate, a, b, `вырезано вручную на таймлайне: ${label}`))
    if (!ops.length) return
    setBusy(true)
    setBusyText('Режу…')
    setError(undefined)
    try {
      const prop = await api.createProposal(pid, {
        base_revision_id: rev.id, rationale: `Ручной монтаж на таймлайне: ${label}`, policy_version: 'manual', ops,
      })
      await api.approve(pid, prop.id, { exclude_op_ids: [], acknowledge_op_ids: prop.ops.map((o) => o.id), overrides: {} })
      undoStack.current.push(rev.id)
      redoStack.current = []
      const frames = merged.reduce((sum, [a, b]) => sum + b - a, 0)
      setNote(`${label}: вырезано ${fmtUs(frameToUs(frames, rate), 1)}. Отменить — ⌘Z`)
      setChosen(undefined)
      setPlayhead(merged[0][0])
      onChanged?.()
    } catch (e) {
      setNote(undefined)
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [rev, rate, busy, current, pid, onChanged])

  /** ⌘X: взять кусок — выделенный или отрезок I–O. Версия не меняется. */
  const take = useCallback((range: [number, number] | undefined) => {
    if (!rev || !rate) return
    if (!range || range[0] >= range[1]) {
      setNote('Выделите один кусок (V и щелчок по дорожке) или поставьте метки I и O, потом ⌘X')
      return
    }
    const block = blockOf(rev, rate, range[0], range[1])
    if (!block) {
      setNote('Выделение из разных мест записи: переставить можно только сплошной кусок одной записи')
      return
    }
    setTaken({ block, frames: range, revId: rev.id })
    setNote(`Взято ${fmtUs(frameToUs(range[1] - range[0], rate), 1)} — поставьте курсор и ⌘⇧V`)
  }, [rev, rate])

  /** ⌘⇧V: вставить взятый кусок со сдвигом перед кадром под курсором — правка
   *  Автора и сразу утверждение, как у выреза; отмена — ⌘Z. */
  const insert = useCallback(async () => {
    if (!rev || !rate || busy) return
    if (!taken) {
      setNote('Сначала возьмите кусок: выделите его и ⌘X')
      return
    }
    if (rev.id !== current) {
      setNote('Переставлять можно только текущую версию — выберите её в списке «Версия»')
      return
    }
    const [a, b] = taken.frames
    if (taken.revId === rev.id && a < playhead && playhead < b) {
      setNote('Курсор внутри взятого куска — поставьте его туда, куда кусок переставить')
      return
    }
    const length = fmtUs(frameToUs(b - a, rate), 1)
    setBusy(true)
    setBusyText('Переставляю…')
    setError(undefined)
    try {
      const prop = await api.createProposal(pid, {
        base_revision_id: rev.id, rationale: 'Ручной монтаж на таймлайне: ⌘X и ⌘⇧V — перестановка',
        policy_version: 'manual', ops: [moveOp(taken.block, playhead, 'переставлено вручную на таймлайне (⌘X, ⌘⇧V)')],
      })
      await api.approve(pid, prop.id, { exclude_op_ids: [], acknowledge_op_ids: prop.ops.map((o) => o.id), overrides: {} })
      undoStack.current.push(rev.id)
      redoStack.current = []
      setTaken(undefined)
      setNote(`Переставлено ${length}. Отменить — ⌘Z`)
      setChosen(undefined)
      // Курсор — на начало куска на новом месте
      setPlayhead(taken.revId === rev.id && b <= playhead ? playhead - (b - a) : playhead)
      onChanged?.()
    } catch (e) {
      setNote(undefined)
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [rev, rate, busy, taken, current, playhead, pid, onChanged])

  /** ⌘Z и ⇧⌘Z: вернуть версию, какой она была до правки (откат — тоже новая версия). */
  const travel = useCallback(async (back: boolean) => {
    if (!rev || busy) return
    const from = back ? undoStack : redoStack
    const to = back ? redoStack : undoStack
    const target = from.current.pop()
    if (!target) {
      setNote(back ? 'Отменять нечего' : 'Возвращать нечего')
      return
    }
    setBusy(true)
    setError(undefined)
    try {
      await api.revert(pid, target)
      to.current.push(rev.id)
      setNote(back ? 'Отменено. Вернуть — ⇧⌘Z' : 'Возвращено')
      setChosen(undefined)
      onChanged?.()
    } catch (e) {
      from.current.push(target)
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [rev, busy, pid, onChanged])

  const addMarker = useCallback(async () => {
    if (!rev) return
    setError(undefined)
    try {
      await api.addMarker(pid, { edited_frame: playhead, revision_id: rev.id })
      markers.reload()
    } catch (e) {
      setError(e)
    }
  }, [pid, rev, playhead, markers])

  // Клавиши — как в Premiere Pro (lib/edit.ts, keyAction)
  useEffect(() => {
    if (!rev || !rate) return
    const total = rev.total_frames
    const last = total - 1
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target)) return
      const act = keyAction(e)
      if (!act) return
      e.preventDefault()
      const clamp = (f: number) => Math.max(0, Math.min(last, f))
      const halt = () => {
        player.current?.pause()
        setSpeed(0)
      }
      const zoomBy = (factor: number) => setView((cur) => {
        const [a, b] = cur && cur[1] <= total ? cur : [0, total]
        const span = Math.max(Math.round(rate.num / rate.den), Math.min(total, Math.round((b - a) * factor)))
        const start = Math.max(0, Math.min(total - span, Math.round(playhead - span / 2)))
        return start === 0 && span === total ? undefined : [start, start + span]
      })
      const inOut = (): [number, number] | undefined =>
        inPt !== undefined || outPt !== undefined ? [inPt ?? 0, outPt ?? total] : undefined
      switch (act) {
        case 'toggle': setSpeed(0); player.current?.toggle(); break
        case 'stop': halt(); break
        case 'forward': { const s = shuttle(speed, 1); setSpeed(s); player.current?.play(s); break }
        case 'reverse': { player.current?.pause(); setSpeed(shuttle(speed, -1)); break }
        case 'frameBack': halt(); setPlayhead((f) => clamp(f - 1)); break
        case 'frameFwd': halt(); setPlayhead((f) => clamp(f + 1)); break
        case 'frameBack5': halt(); setPlayhead((f) => clamp(f - 5)); break
        case 'frameFwd5': halt(); setPlayhead((f) => clamp(f + 5)); break
        case 'prevEdit': halt(); setPlayhead((f) => prevPoint(points, f) ?? 0); break
        case 'nextEdit': halt(); setPlayhead((f) => clamp(nextPoint(points, f) ?? last)); break
        case 'home': halt(); setPlayhead(0); break
        case 'end': halt(); setPlayhead(last); break
        case 'markIn': setInPt(playhead); if (outPt !== undefined && outPt <= playhead) setOutPt(undefined); break
        case 'markOut': setOutPt(playhead + 1); if (inPt !== undefined && inPt > playhead) setInPt(undefined); break
        case 'clearIn': setInPt(undefined); break
        case 'clearOut': setOutPt(undefined); break
        case 'clearInOut': setInPt(undefined); setOutPt(undefined); break
        case 'extract':
        case 'lift': {
          const r = inOut()
          if (r && r[0] < r[1]) void cut([r], 'отрезок между метками I и O')
          else setNote('Поставьте метки: I — вход, O — выход')
          break
        }
        case 'addEdit': if (!points.includes(playhead)) setCuts((c) => [...c, playhead]); break
        case 'trimPrev': { const r = trimToPrev(points, playhead); if (r) void cut([r], 'Q — от прошлой склейки до курсора'); break }
        case 'trimNext': { const r = trimToNext(points, playhead); if (r) void cut([r], 'W — от курсора до следующей склейки'); break }
        case 'delete': {
          const r = inOut()
          if (picked.length) void cut(picked, picked.length === 1 ? 'выделенный кусок' : `выделенные куски (${picked.length})`)
          else if (r && r[0] < r[1]) void cut([r], 'отрезок между метками I и O')
          else setNote('Выделите кусок (V и щелчок по дорожке) или поставьте метки I и O')
          break
        }
        case 'undo': void travel(true); break
        case 'redo': void travel(false); break
        case 'zoomIn': zoomBy(0.5); break
        case 'zoomOut': zoomBy(2); break
        case 'zoomFit': setView(undefined); break
        case 'select': setTool('select'); break
        case 'razor': setTool('razor'); break
        case 'marker': void addMarker(); break
        case 'take': {
          const r = inOut()
          if (picked.length > 1) setNote('⌘X берёт один кусок: выделите один или поставьте метки I и O')
          else take(picked.length ? picked[0] : r)
          break
        }
        case 'insertShift': void insert(); break
        case 'paste': setNote(taken ? 'Вставить со сдвигом — ⌘⇧V, как в Premiere' : 'Сначала возьмите кусок: ⌘X, потом ⌘⇧V'); break
        case 'escape':
          setPicked([])
          if (taken) {
            setTaken(undefined)
            setNote('Взятый кусок снят')
          }
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rev, rate, points, playhead, inPt, outPt, picked, speed, cut, travel, addMarker, take, insert, taken])

  if (!current) {
    return (
      <StatePanel state="empty" title="Версий пока нет">
        <p>
          Таймлайн показывает версию целиком. Подключите файлы и создайте первую версию в разделе{' '}
          <a href="#" onClick={(e) => { e.preventDefault(); onOpenTab?.('setup') }}>Проект</a>.
        </p>
      </StatePanel>
    )
  }
  if (revision.error) {
    return (
      <StatePanel state="error" title="Не удалось загрузить версию">
        <pre className="error-box">{revision.error.message}</pre>
        <p>Обновите страницу. Если ошибка повторяется — посмотрите журнал сервера.</p>
      </StatePanel>
    )
  }
  if (!rev || !rate) {
    return (
      <div className="screen tl-loading" data-state="loading" aria-busy="true">
        <p className="loading-line" role="status">Загружаю версию…</p>
        <div className="tl-canvas-box" aria-hidden="true" />
      </div>
    )
  }

  const [v0, v1] = view && view[1] <= rev.total_frames ? view : [0, rev.total_frames]
  const zoom = (factor: number) => {
    const span = Math.max(Math.round(rate.num / rate.den), Math.min(rev.total_frames, Math.round((v1 - v0) * factor)))
    const start = Math.max(0, Math.min(rev.total_frames - span, Math.round(playhead - span / 2)))
    setView(start === 0 && span === rev.total_frames ? undefined : [start, start + span])
  }
  const outside = {
    markers: (markers.data ?? []).filter((m) => !m.in_revision),
    comments: (comments.data ?? []).filter((c) => !c.in_revision),
  }

  return (
    <div className="screen fill tl">
      <div className="subbar tl-bar">
        <label className="inline-label">
          Версия
          <Select small value={rev.id} onChange={(e) => { setChosen(e.target.value); setView(undefined); setPlayhead(0) }}>
            {(revisions.data ?? [{ id: rev.id, number: rev.number, state: rev.state } as RevisionSummary]).map((r) => (
              <option key={r.id} value={r.id}>{revisionLabel(r, current)}</option>
            ))}
          </Select>
        </label>
        <p className="mono faint tl-stats">
          {fmtUs(rev.duration_us, 1)} · стыков {joints.data?.length ?? '…'} · кусков {rev.clips.length}
        </p>
        <p className="faint tl-note" role="status">{busy ? busyText : note}</p>
        <div role="group" aria-label="Инструменты монтажа" className="tl-tools">
          <button type="button" className="btn btn-quiet btn-sm" aria-pressed={tool === 'select'} onClick={() => setTool('select')}>
            <kbd>V</kbd>Выделение
          </button>
          <button type="button" className="btn btn-quiet btn-sm" aria-pressed={tool === 'razor'} onClick={() => setTool('razor')}>
            <kbd>C</kbd>Лезвие
          </button>
          <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => void travel(true)}>
            <kbd>⌘Z</kbd>Отменить
          </button>
          <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => void travel(false)}>
            <kbd>⇧⌘Z</kbd>Вернуть
          </button>
        </div>
        <details className="tl-keys">
          <summary>Клавиши как в Premiere</summary>
          <dl>
            <dt><kbd>Пробел</kbd> <kbd>J</kbd><kbd>K</kbd><kbd>L</kbd></dt><dd>просмотр, назад, стоп, вперёд (ещё раз — быстрее)</dd>
            <dt><kbd>←</kbd><kbd>→</kbd></dt><dd>кадр (с Shift — 5 кадров)</dd>
            <dt><kbd>↑</kbd><kbd>↓</kbd></dt><dd>прошлая и следующая склейка</dd>
            <dt><kbd>V</kbd> <kbd>C</kbd></dt><dd>выделение, лезвие (щелчок по дорожке — надрез)</dd>
            <dt><kbd>⌘K</kbd></dt><dd>надрез под курсором</dd>
            <dt><kbd>Q</kbd> <kbd>W</kbd></dt><dd>вырезать от прошлой склейки до курсора, от курсора до следующей</dd>
            <dt><kbd>I</kbd> <kbd>O</kbd> <kbd>'</kbd></dt><dd>вход, выход, вырезать отрезок между ними (<kbd>⌥X</kbd> — снять метки)</dd>
            <dt><kbd>⌫</kbd></dt><dd>удалить выделенные куски (Shift — к нескольким), всё следом сдвигается</dd>
            <dt><kbd>⌘X</kbd> <kbd>⌘⇧V</kbd></dt><dd>взять кусок (выделенный или I–O), вставить со сдвигом перед кадром под курсором — перестановка</dd>
            <dt><kbd>⌘Z</kbd> <kbd>⇧⌘Z</kbd></dt><dd>отменить, вернуть</dd>
            <dt><kbd>=</kbd> <kbd>-</kbd> <kbd>{'\\'}</kbd></dt><dd>крупнее, мельче, весь ролик</dd>
            <dt><kbd>M</kbd></dt><dd>метка</dd>
            <dt>край дорожки</dt><dd>потянуть вверх или вниз — дорожка уже или шире; щелчок по графике — курсор на её начало</dd>
          </dl>
        </details>
      </div>

      <div className="tl-top">
        <Monitor
          project={project}
          revision={rev}
          rate={rate}
          playhead={playhead}
          onTime={setPlayhead}
          handle={player}
          layer={layer.data}
          graphics={drawn.data?.graphics ?? []}
          joints={(joints.data ?? []).map((j) => j.out_frame)}
          cameras={cameras}
          editable={rev.id === current}
          onChanged={() => { layer.reload(); drawn.reload() }}
        />
        <section className="tl-notes" aria-labelledby="tl-marks">
          <h2 id="tl-marks" className="block-title">Метки и комментарии</h2>
          <ErrorText error={error ?? markers.error ?? comments.error} />
          <div className="tl-list">
            <h3 className="group-title">Метки</h3>
            {markers.data?.length === 0 && <p className="faint tl-empty">Меток нет. Клавиша M ставит метку у курсора.</p>}
            <ol className="tl-marks">
              {(markers.data ?? []).map((m) => (
                <li key={m.id} data-in-revision={m.in_revision ? 'yes' : 'no'}>
                  <span className="mono tl-marker-name">M{m.number}</span>
                  {m.in_revision ? (
                    <button type="button" className="time-link" onClick={() => setPlayhead(m.edited_frame as number)}>
                      {fmtUs(frameToUs(m.edited_frame as number, rate), 1)}
                    </button>
                  ) : <span className="mono faint">—</span>}
                  <span className="tl-row-extra">
                    {m.kind === 'chapter' && <span className="chip sm plain tl-chapter-chip">глава</span>}
                    {!m.in_revision && (
                      <>
                        <span className="chip sm" data-in-revision="no">не входит в эту версию</span>
                        <span className="mono faint">исходник {assetRef(project.assets, m.asset_id)} {fmtUs(m.src_us, 1)}</span>
                      </>
                    )}
                    {m.label && <span className={m.kind === 'chapter' ? 'tl-chapter-title' : undefined}>{m.label}</span>}
                    {m.created_by === 'agent' && <span className="faint">поставил Claude</span>}
                  </span>
                  <button type="button" className="btn-quiet btn tl-row-action" onClick={() => api.removeMarker(pid, m.id).then(markers.reload, setError)}>
                    удалить
                  </button>
                </li>
              ))}
            </ol>
            <ChapterForm
              at={fmtUs(frameToUs(playhead, rate), 1)}
              onAdd={async (title) => {
                setError(undefined)
                try {
                  await api.addMarker(pid, { edited_frame: playhead, revision_id: rev.id, label: title, kind: 'chapter' })
                  markers.reload()
                } catch (e) {
                  setError(e)
                  throw e
                }
              }}
            />
          </div>
          <div className="tl-list">
            <h3 className="group-title">Комментарии</h3>
            {comments.data?.length === 0 && <p className="faint tl-empty">Комментариев нет.</p>}
            <ol className="tl-comments">
              {(comments.data ?? []).map((c) => (
                <li key={c.id} data-state={c.status} data-in-revision={c.in_revision ? 'yes' : 'no'}>
                  {c.in_revision ? (
                    <button type="button" className="time-link" onClick={() => setPlayhead(c.edited_frame as number)}>
                      {fmtUs(frameToUs(c.edited_frame as number, rate), 1)}
                    </button>
                  ) : <span className="mono faint tl-dash">—</span>}
                  <div className="tl-comment-body">
                    <p>{c.text}</p>
                    <span className="tl-row-extra">
                      {!c.in_revision && (
                        <>
                          <span className="chip sm" data-in-revision="no">не входит в эту версию</span>
                          <span className="mono faint">исходник {assetRef(project.assets, c.asset_id)} {fmtUs(c.src_us, 1)}</span>
                        </>
                      )}
                      {(c.author === 'agent' || c.status === 'resolved') && (
                        <span className="faint">{[c.author === 'agent' ? 'Claude' : '', c.status === 'resolved' ? 'закрыт' : ''].filter(Boolean).join(' · ')}</span>
                      )}
                    </span>
                  </div>
                  {c.status === 'open' && (
                    <button type="button" className="btn-quiet btn tl-row-action" onClick={() => api.resolveComment(pid, c.id).then(comments.reload, setError)}>
                      закрыть
                    </button>
                  )}
                </li>
              ))}
            </ol>
            <CommentForm
              at={fmtUs(frameToUs(playhead, rate), 1)}
              onAdd={async (text) => {
                setError(undefined)
                try {
                  await api.addComment(pid, { text, revision_id: rev.id, edited_frame: playhead })
                  comments.reload()
                } catch (e) {
                  setError(e)
                  throw e
                }
              }}
            />
          </div>
        </section>
      </div>

      <section className="tl-whole" aria-labelledby="tl-cv">
        <div className="tl-whole-head">
          <h2 id="tl-cv" className="block-title">
            {view ? <>Версия {rev.number} · <span className="mono">{fmtUs(frameToUs(v0, rate), 1)}–{fmtUs(frameToUs(v1, rate), 1)}</span></> : <>Версия {rev.number} целиком</>}
          </h2>
          <div role="group" aria-label="Масштаб" className="tl-zoom">
            <button type="button" className="btn btn-quiet" onClick={() => zoom(0.25)}>крупнее</button>
            <button type="button" className="btn btn-quiet" onClick={() => zoom(4)} disabled={!view}>мельче</button>
            <button type="button" className="btn btn-quiet" onClick={() => setView(undefined)} disabled={!view}>весь ролик</button>
          </div>
          <ul className="tl-legend" aria-label="Обозначения">
            <li><span aria-hidden="true" className="lg-video" />кадры камеры и экрана</li>
            <li><span aria-hidden="true" className="lg-shot" />кадр: лицо · приближение</li>
            <li><span aria-hidden="true" className="lg-graphic" />графика (верхняя дорожка)</li>
            <li><span aria-hidden="true" className="lg-wave" />звук ролика</li>
            <li><span aria-hidden="true" className="lg-joint" />стык</li>
            <li><span aria-hidden="true" className="lg-comment" />комментарий</li>
            <li><span aria-hidden="true" className="lg-mark mono">M1</span>метка</li>
            <li><span aria-hidden="true" className="lg-cursor" />курсор</li>
          </ul>
        </div>
        <div className="tl-canvas-box">
          <TimelineCanvas
            revision={rev}
            rate={rate}
            waves={waves.data ?? {}}
            strips={strips}
            cameras={cameras}
            soundFromCamera={project.audio_source === 'camera'}
            joints={(joints.data ?? []).map((j) => j.out_frame)}
            view={[v0, v1]}
            playhead={playhead}
            commentFrames={(comments.data ?? []).filter((c) => c.in_revision && c.status === 'open').map((c) => c.edited_frame as number)}
            shots={(layer.data?.segments ?? []).filter((v) => v.review !== 'reverted').flatMap((v) =>
              v.edited_ranges.map(([a, b]) => ({ a, b, kind: v.layout === 'screen' ? (v.params.zoom ? 'zoom' : 'screen') : v.layout })))}
            graphics={(drawn.data?.graphics ?? []).filter((g) => g.review !== 'reverted').flatMap((g) =>
              g.edited_ranges.map(([a, b]) => ({ a, b, label: graphicTitle(g.template, g.props) })))}
            heights={heights}
            onHeights={(h) => setTracksPref(JSON.stringify(h))}
            markers={(markers.data ?? []).filter((m) => m.in_revision).map((m) => ({
              label: m.kind === 'chapter' ? m.label : `M${m.number}`,
              frame: m.edited_frame as number,
              chapter: m.kind === 'chapter',
            }))}
            onSeek={setPlayhead}
            tool={tool}
            cuts={cuts}
            picked={picked}
            inOut={[inPt, outPt]}
            onRazor={(f) => { if (!points.includes(f)) setCuts((c) => [...c, f]) }}
            onPick={(f, add) => {
              const piece = pieceAt(points, f)
              if (!piece) return
              setPicked((cur) => {
                if (!add) return [piece]
                const has = cur.some(([a]) => a === piece[0])
                return has ? cur.filter(([a]) => a !== piece[0]) : [...cur, piece]
              })
            }}
          />
        </div>
        <StripNote project={project} revision={rev} strips={strips} cameras={cameras} onOpenTab={onOpenTab} />
        {(outside.markers.length > 0 || outside.comments.length > 0) && (
          <p className="faint tl-foot">
            {[
              outside.markers.map((m) => `M${m.number}`).join(', '),
              outside.comments.length > 0 ? `${outside.comments.length} ${outside.comments.length === 1 ? 'комментарий' : 'комментария'}` : '',
            ].filter(Boolean).join(' и ')}{' '}
            {outside.markers.length + outside.comments.length === 1 ? 'стоит' : 'стоят'} на вырезанном месте: в этой версии их нет, они в списке выше.
          </p>
        )}
      </section>
    </div>
  )
}

function CommentForm({ at, onAdd }: { at: string; onAdd: (text: string) => Promise<void> }) {
  const [text, setText] = useState('')
  return (
    <form
      className="tl-comment-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) void onAdd(text.trim()).then(() => setText(''), () => undefined)
      }}
    >
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-label="Комментарий к месту курсора"
        placeholder={`Комментарий к ${at}`}
      />
      <button type="submit" className="btn btn-sm" disabled={!text.trim()}>Добавить</button>
    </form>
  )
}

function ChapterForm({ at, onAdd }: { at: string; onAdd: (title: string) => Promise<void> }) {
  const [title, setTitle] = useState('')
  return (
    <form
      className="tl-comment-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (title.trim()) void onAdd(title.trim()).then(() => setTitle(''), () => undefined)
      }}
    >
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        aria-label="Название главы у курсора"
        placeholder={`Глава у курсора (${at}): название`}
      />
      <button type="submit" className="btn btn-sm" disabled={!title.trim()}>Глава</button>
    </form>
  )
}

/** Полосы кадров всех файлов проекта. Нет лёгкой копии — так и помечаем. */
function useStrips(pid: string, assetIds: string[]): Record<string, Strip> {
  const [strips, setStrips] = useState<Record<string, Strip>>({})
  const key = assetIds.join(',')
  useEffect(() => {
    let alive = true
    const set = (aid: string, s: Strip) => alive && setStrips((all) => ({ ...all, [aid]: s }))
    for (const aid of key.split(',').filter(Boolean)) {
      set(aid, { state: 'loading' })
      api.thumbs(pid, aid).then(
        (meta) => {
          const image = new Image()
          image.onload = () => set(aid, { state: 'ready', meta, image })
          image.onerror = () => set(aid, { state: 'error' })
          image.src = `${api.thumbsUrl(pid, aid)}?v=${meta.version}-${meta.count}-${meta.step_us}`
        },
        (e) => set(aid, { state: e instanceof ApiError && e.code === 'no_proxy' ? 'no_proxy' : 'error' }),
      )
    }
    return () => {
      alive = false
    }
  }, [pid, key])
  return strips
}

function StripNote({ project, revision, strips, cameras, onOpenTab }: {
  project: Project
  revision: Revision
  strips: Record<string, Strip>
  cameras: Record<string, Asset | undefined>
  onOpenTab?: (tab: Tab) => void
}) {
  const used = [...new Set(revision.clips.flatMap((c) => [c.asset_id, cameras[c.asset_id]?.id ?? []].flat()))]
  const missing = used.filter((a) => strips[a]?.state === 'no_proxy')
  const failed = used.filter((a) => strips[a]?.state === 'error')
  if (missing.length === 0 && failed.length === 0) return null
  const names = (ids: string[]) => ids.map((a) => assetRef(project.assets, a)).join(', ')
  return (
    <p className="faint tl-foot">
      {missing.length > 0 && (
        <>
          Кадров видео нет для {names(missing)}: нужна лёгкая копия, её делают в разделе{' '}
          <a href="#" onClick={(e) => { e.preventDefault(); onOpenTab?.('setup') }}>Проект</a>.{' '}
        </>
      )}
      {failed.length > 0 && <>Не удалось собрать кадры для {names(failed)}.</>}
    </p>
  )
}

// Полосы холста сверху вниз: шкала времени, метки, графика, камера, экран,
// звук (lib/tracks.ts)
const PAD_X = 12

const TICKS_S = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]

function TimelineCanvas({ revision, rate, waves, strips, cameras = {}, soundFromCamera = false, joints, view, playhead, commentFrames,
  markers, shots = [], graphics = [], heights, onHeights, onSeek, tool = 'select', cuts = [], picked = [], inOut = [undefined, undefined],
  onRazor, onPick }: {
  revision: Revision
  rate: Rate
  waves: Record<string, Waveform | undefined>
  strips: Record<string, Strip>
  /** Запись экрана -> сведённая с ней камера */
  cameras?: Record<string, Asset | undefined>
  soundFromCamera?: boolean
  joints: number[]
  view: [number, number]
  playhead: number
  commentFrames: number[]
  markers: { label: string; frame: number; chapter: boolean }[]
  shots?: { a: number; b: number; kind: string }[]
  /** Графика ролика — блоки на верхней дорожке */
  graphics?: { a: number; b: number; label: string }[]
  heights: TrackHeights
  onHeights?: (h: TrackHeights) => void
  onSeek: (frame: number) => void
  /** Ручной монтаж: инструмент, надрезы, выделенные куски, метки входа и выхода */
  tool?: 'select' | 'razor'
  cuts?: number[]
  picked?: [number, number][]
  inOut?: [number | undefined, number | undefined]
  onRazor?: (frame: number) => void
  onPick?: (frame: number, add: boolean) => void
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState<[number, number]>([0, 0])
  const [hover, setHover] = useState<number>()
  const [edgeHover, setEdgeHover] = useState(false)
  const scrubbing = useRef(false)
  // Тянем край дорожки: какая, откуда начали, какой была высота
  const resizing = useRef<{ key: keyof TrackHeights; y: number; h: number } | undefined>(undefined)
  const [dragging, setDragging] = useState(false)
  const [v0, v1] = view
  // Дорожка камеры — только если хоть у одного куска есть сведённая камера
  const withCamera = revision.clips.some((c) => cameras[c.asset_id])
  const lanes = useMemo(() => layoutLanes(heights, withCamera, size[1]), [heights, withCamera, size])
  const need = neededHeight(heights, withCamera)

  // Размер холста — по коробке: окно меняется, холст перерисовывается
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const observer = new ResizeObserver(() => setSize([canvas.clientWidth, canvas.clientHeight]))
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const canvas = ref.current
    const [w, h] = size
    if (!canvas || !w || !h) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.round(w * dpr)
    canvas.height = Math.round(h * dpr)
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)
    // Цвета — из стилей автора: волна = color холста, остальное — токены
    const cs = getComputedStyle(canvas)
    const v = (name: string) => cs.getPropertyValue(name).trim()
    const col = {
      wave: cs.color, joint: v('--line-control'), comment: v('--text-2'), mark: v('--text'), markBg: v('--surface-3'),
      cursor: v('--accent'), a: v('--surface-1'), b: v('--surface-2'), ruler: v('--line'), label: v('--text-3'),
      empty: v('--surface-2'), chapterBg: v('--surface-4'), warn: v('--warn'),
    }
    const mono = `12px ${v('--font-mono') || 'monospace'}`
    const span = Math.max(1, v1 - v0)
    const X = (frame: number) => PAD_X + ((frame - v0) / span) * (w - PAD_X * 2)
    const lanesTop = RULER + MARKS
    const lane = (key: keyof TrackHeights) => lanes.find((l) => l.key === key)
    const gLane = lane('graphics') as { top: number; height: number }
    const cLane = lane('camera')
    const sLane = lane('screen') as { top: number; height: number }
    const aLane = lane('audio') as { top: number; height: number }
    const cameraTop = cLane?.top ?? sLane.top
    const videoTop = sLane.top
    const audioTop = aLane.top
    const bottom = audioTop + aLane.height
    const mid = (audioTop + bottom) / 2
    const half = Math.max(2, (bottom - audioTop) / 2 - 2)
    const visible = revision.clips.filter((c) => c.out_end_frame > v0 && c.out_start_frame < v1)

    // Дорожка графики — ровным фоном: это не куски записи
    ctx.fillStyle = col.a
    ctx.fillRect(PAD_X, gLane.top, w - PAD_X * 2, gLane.height)
    // Куски версии — чередуются фоном по дорожкам записи
    visible.forEach((clip) => {
      const i = revision.clips.indexOf(clip)
      const xa = Math.max(PAD_X, X(clip.out_start_frame))
      const xb = Math.min(w - PAD_X, X(clip.out_end_frame))
      ctx.fillStyle = i % 2 ? col.b : col.a
      ctx.fillRect(xa, cameraTop, xb - xa, bottom - cameraTop)
    })

    ctx.font = mono
    ctx.textBaseline = 'middle'
    const note = (text: string, xa: number, xb: number, top: number, hh: number) => {
      if (hh >= 16 && xb - xa > ctx.measureText(text).width + 12) {
        ctx.fillStyle = col.label
        ctx.fillText(text, xa + 6, top + hh / 2)
      }
    }
    // Полоса кадров файла на дорожке: fileUs — время файла в момент записи
    // экрана (у камеры — по сведению); undefined — файл тогда не снимал
    const drawStrip = (clip: Revision['clips'][number], top: number, hh: number, strip: Strip | undefined,
      fileUs: (mainUs: number) => number | undefined, gapText: string) => {
      const xa = Math.max(PAD_X, X(clip.out_start_frame))
      const xb = Math.min(w - PAD_X, X(clip.out_end_frame))
      if (xb - xa < 1) return
      ctx.save()
      ctx.beginPath()
      ctx.rect(xa, top, xb - xa, hh)
      ctx.clip()
      if (strip?.state === 'ready') {
        const { meta, image } = strip
        const tileW = Math.max(4, Math.round((hh * meta.tile_w) / meta.tile_h))
        let gap = false
        // Кадр в начале каждой ячейки; у первого — начало куска
        for (let x = X(clip.out_start_frame); x < xb; x += tileW) {
          if (x + tileW < xa) continue
          const frame = v0 + ((Math.max(x, xa) - PAD_X) / (w - PAD_X * 2)) * span
          const us = fileUs(clip.src_start_us + frameToUs(Math.max(0, frame - clip.out_start_frame), rate))
          if (us === undefined) {
            ctx.fillStyle = col.empty
            ctx.fillRect(x, top, tileW, hh)
            gap = true
            continue
          }
          const k = Math.max(0, Math.min(meta.count - 1, Math.round(us / meta.step_us)))
          const sx = (k % meta.cols) * meta.tile_w
          const sy = Math.floor(k / meta.cols) * meta.tile_h
          ctx.drawImage(image, sx, sy, meta.tile_w, meta.tile_h, x, top, tileW, hh)
        }
        if (gap && fileUs(clip.src_start_us) === undefined) note(gapText, xa, xb, top, hh)
      } else {
        ctx.fillStyle = col.empty
        ctx.fillRect(xa, top, xb - xa, hh)
        note(strip?.state === 'no_proxy' ? 'нет лёгкой копии' : strip?.state === 'error' ? 'кадры не собрались' : 'кадры…', xa, xb, top, hh)
      }
      ctx.restore()
    }

    // Дорожки камеры и экрана: кадры кусками, по времени файла
    for (const clip of visible) {
      const camera = cameras[clip.asset_id]
      if (cLane) {
        if (camera) drawStrip(clip, cLane.top, cLane.height, strips[camera.id], (us) => cameraUs(camera, us), 'камера не снимала')
        else {
          const xa = Math.max(PAD_X, X(clip.out_start_frame))
          const xb = Math.min(w - PAD_X, X(clip.out_end_frame))
          note('камеры нет', xa, xb, cLane.top, cLane.height)
        }
      }
      drawStrip(clip, videoTop, sLane.height, strips[clip.asset_id], (us) => us, '')
    }

    // Звук ролика: столбики через 2 пикселя по громкости файла — камеры, если
    // в ролик идёт её звук и громкость камеры измерена, иначе записи экрана
    // (та же речь в то же время)
    ctx.fillStyle = col.wave
    for (const clip of visible) {
      const own = soundFromCamera ? cameras[clip.asset_id] : undefined
      const camera = own && waves[own.id] ? own : undefined
      const wave = waves[camera?.id ?? clip.asset_id]
      if (!wave) continue
      const xa = Math.max(PAD_X, X(clip.out_start_frame))
      const xb = Math.min(w - PAD_X, X(clip.out_end_frame))
      const usPerPoint = (wave.to_us - wave.from_us) / wave.points
      for (let px = Math.ceil(xa); px < xb; px += 2) {
        const frame = v0 + ((px - PAD_X) / (w - PAD_X * 2)) * span
        const mainUs = clip.src_start_us + frameToUs(frame - clip.out_start_frame, rate)
        const srcUs = camera ? cameraUs(camera, mainUs) : mainUs
        if (srcUs === undefined) continue
        const db = wave.peaks_db[Math.floor((srcUs - wave.from_us) / usPerPoint)]
        if (db === undefined) continue
        const level = Math.max(0, Math.min(1, (db + 70) / 70))
        const a = Math.max(0.5, level * half)
        ctx.fillRect(px, mid - a, 1, a * 2)
      }
    }

    // Слой кадра — полоса над дорожками кадров (в зазоре под графикой):
    // лицо целиком, приближение, экран без лица
    for (const sh of shots) {
      if (sh.b <= v0 || sh.a >= v1) continue
      const xa = Math.max(PAD_X, X(sh.a))
      const xb = Math.min(w - PAD_X, X(sh.b))
      ctx.fillStyle = sh.kind === 'zoom' ? col.cursor : sh.kind === 'camera' ? col.mark : col.label
      ctx.fillRect(xa, cameraTop - GAP + 1, Math.max(1, xb - xa), 3)
    }

    // Графика — блоки на верхней дорожке с подписью, что на экране
    for (const g of graphics) {
      if (g.b <= v0 || g.a >= v1) continue
      const xa = Math.max(PAD_X, X(g.a))
      const xb = Math.min(w - PAD_X, X(g.b))
      const bw = Math.max(2, xb - xa)
      const top = gLane.top + 1
      const gh = gLane.height - 2
      ctx.fillStyle = col.warn
      ctx.globalAlpha = 0.3
      ctx.fillRect(xa, top, bw, gh)
      ctx.globalAlpha = 1
      ctx.strokeStyle = col.warn
      ctx.lineWidth = 1
      ctx.strokeRect(xa + 0.5, top + 0.5, Math.max(1, bw - 1), gh - 1)
      if (gh >= 14 && bw > 28) {
        ctx.save()
        ctx.beginPath()
        ctx.rect(xa + 1, top, bw - 2, gh)
        ctx.clip()
        ctx.fillStyle = col.mark
        ctx.fillText(g.label, xa + 6, top + gh / 2)
        ctx.restore()
      }
    }

    // Метки входа и выхода (I/O): полоса на шкале и лёгкая заливка дорожек
    const [ia, ib] = inOut
    if (ia !== undefined || ib !== undefined) {
      const xa = Math.max(PAD_X, X(ia ?? 0))
      const xb = Math.min(w - PAD_X, X(ib ?? revision.total_frames))
      if (xb > xa) {
        ctx.fillStyle = col.cursor
        ctx.globalAlpha = 0.12
        ctx.fillRect(xa, lanesTop, xb - xa, bottom - lanesTop)
        ctx.globalAlpha = 0.7
        ctx.fillRect(xa, RULER - 5, xb - xa, 4)
        ctx.globalAlpha = 1
        for (const [f, dir] of [[ia, 1], [ib, -1]] as const) {
          if (f === undefined || f < v0 || f > v1) continue
          const x = Math.round(X(f))
          ctx.fillRect(x - (dir < 0 ? 2 : 0), RULER - 12, 2, 12)
          ctx.fillRect(dir > 0 ? x : x - 8, RULER - 12, 8, 2)
        }
      }
    }

    // Выделенные куски: заливка цветом курсора и рамка через все дорожки
    for (const [a, b] of picked) {
      if (b <= v0 || a >= v1) continue
      const xa = Math.max(PAD_X, X(a))
      const xb = Math.min(w - PAD_X, X(b))
      ctx.fillStyle = col.cursor
      ctx.globalAlpha = 0.28
      ctx.fillRect(xa, lanesTop, xb - xa, bottom - lanesTop)
      ctx.globalAlpha = 1
      ctx.strokeStyle = col.cursor
      ctx.lineWidth = 2
      ctx.strokeRect(xa + 1, lanesTop + 1, Math.max(1, xb - xa - 2), bottom - lanesTop - 2)
      ctx.lineWidth = 1
    }

    // Надрезы лезвием — пунктир через все дорожки; под мышью — где будет надрез
    ctx.setLineDash([4, 3])
    ctx.strokeStyle = col.mark
    for (const c of cuts) {
      if (c <= v0 || c >= v1) continue
      const x = Math.round(X(c)) + 0.5
      ctx.beginPath()
      ctx.moveTo(x, lanesTop)
      ctx.lineTo(x, bottom)
      ctx.stroke()
    }
    if (tool === 'razor' && hover !== undefined && hover > v0 && hover < v1) {
      const x = Math.round(X(hover)) + 0.5
      ctx.strokeStyle = col.cursor
      ctx.beginPath()
      ctx.moveTo(x, lanesTop)
      ctx.lineTo(x, bottom)
      ctx.stroke()
    }
    ctx.setLineDash([])

    // Стыки — через обе дорожки
    ctx.fillStyle = col.joint
    for (const j of joints) {
      if (j <= v0 || j >= v1) continue
      ctx.fillRect(Math.round(X(j)), lanesTop, 1, bottom - lanesTop)
    }

    // Шкала времени: подписи не теснее ~90 пикселей
    ctx.strokeStyle = col.ruler
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(0, RULER - 0.5)
    ctx.lineTo(w, RULER - 0.5)
    ctx.stroke()
    const secondsPerPx = frameToUs(span, rate) / 1e6 / Math.max(1, w - PAD_X * 2)
    const major = TICKS_S.find((s) => s / secondsPerPx >= 90) ?? 3600
    const minor = major / 5
    const t0 = frameToUs(v0, rate) / 1e6
    const t1 = frameToUs(v1, rate) / 1e6
    for (let s = Math.ceil(t0 / minor) * minor; s <= t1; s += minor) {
      const px = Math.round(X(usToFrame(s * 1e6, rate))) + 0.5
      const isMajor = Math.abs(s / major - Math.round(s / major)) < 1e-6
      ctx.strokeStyle = isMajor ? col.label : col.ruler
      ctx.beginPath()
      ctx.moveTo(px, isMajor ? 14 : 19)
      ctx.lineTo(px, RULER - 1)
      ctx.stroke()
      if (isMajor) {
        ctx.fillStyle = col.label
        ctx.fillText(fmtUs(s * 1e6, 1).replace(/\.0$/, ''), px + 4, 10)
      }
    }

    // Комментарии — пунктир
    ctx.strokeStyle = col.comment
    ctx.setLineDash([3, 3])
    for (const f of commentFrames) {
      if (f < v0 || f > v1) continue
      const px = Math.round(X(f)) + 0.5
      ctx.beginPath()
      ctx.moveTo(px, RULER + 2)
      ctx.lineTo(px, bottom)
      ctx.stroke()
    }
    ctx.setLineDash([])

    // Метки — сплошная линия и номер в полосе меток; у главы — её название
    for (const m of markers) {
      if (m.frame < v0 || m.frame > v1) continue
      const px = Math.round(X(m.frame))
      const label = m.label.length > 28 ? `${m.label.slice(0, 27)}…` : m.label
      const tw = ctx.measureText(label).width + 8
      ctx.fillStyle = m.chapter ? col.chapterBg : col.markBg
      ctx.fillRect(px + 2, RULER + 2, tw, MARKS - 4)
      ctx.fillStyle = col.mark
      ctx.fillRect(px, RULER + 2, 1, bottom - RULER - 2)
      ctx.fillText(label, px + 6, RULER + MARKS / 2)
    }

    // Курсор
    if (playhead >= v0 && playhead <= v1) {
      const cx = Math.round(X(playhead))
      ctx.fillStyle = col.cursor
      ctx.fillRect(cx - 1, RULER, 2, bottom - RULER)
      ctx.beginPath()
      ctx.moveTo(cx - 5, RULER)
      ctx.lineTo(cx + 5, RULER)
      ctx.lineTo(cx, RULER + 6)
      ctx.closePath()
      ctx.fill()
    }
  }, [size, revision, rate, waves, strips, cameras, soundFromCamera, joints, v0, v1, playhead, commentFrames, markers, shots,
    graphics, lanes, tool, cuts, picked, inOut, hover])

  const frameAt = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const frame = v0 + ((e.clientX - box.left - PAD_X) / (box.width - PAD_X * 2)) * (v1 - v0)
    return Math.max(0, Math.min(revision.total_frames - 1, Math.round(frame)))
  }
  // Край дорожки — тянуть высоту; шкала, полоса меток и дорожка графики —
  // курсор (тянуть — прокрутка; щелчок по графике — на её начало); дорожки
  // записи — инструмент: выделение куска (Shift или ⌘ — добавить) или надрез
  const yAt = (e: React.PointerEvent<HTMLCanvasElement>) => e.clientY - e.currentTarget.getBoundingClientRect().top
  const down = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const y = yAt(e)
    const edge = onHeights ? edgeAt(lanes, y) : undefined
    if (edge) {
      resizing.current = { key: edge, y, h: heights[edge] }
      setDragging(true)
      e.currentTarget.setPointerCapture(e.pointerId)
      return
    }
    if (laneAt(lanes, y) === 'graphics') {
      const f = frameAt(e)
      const hit = graphics.find((g) => g.a <= f && f < g.b)
      scrubbing.current = true
      e.currentTarget.setPointerCapture(e.pointerId)
      onSeek(hit ? hit.a : f)
      return
    }
    if (y < RULER + MARKS || !onPick) {
      scrubbing.current = true
      e.currentTarget.setPointerCapture(e.pointerId)
      onSeek(frameAt(e))
    } else if (tool === 'razor') onRazor?.(frameAt(e))
    else onPick(frameAt(e), e.shiftKey || e.metaKey)
  }

  return (
    <canvas
      ref={ref}
      className="tl-canvas"
      role="slider"
      tabIndex={0}
      aria-label={`Версия ${revision.number}: дорожка графики, кадры камеры и экрана и звук по ${revision.clips.length} кускам, ${joints.length} стыков`}
      aria-valuemin={0}
      aria-valuemax={revision.total_frames}
      aria-valuenow={playhead}
      aria-valuetext={fmtUs(frameToUs(playhead, rate), 1)}
      style={{
        height: `max(100%, ${need}px)`,
        cursor: dragging || edgeHover ? 'row-resize' : tool === 'razor' ? 'crosshair' : undefined,
      }}
      onPointerDown={down}
      onPointerMove={(e) => {
        const r = resizing.current
        if (r && e.buttons & 1) {
          onHeights?.({ ...heights, [r.key]: clampHeight(r.key, r.h + yAt(e) - r.y) })
          return
        }
        setEdgeHover(!!onHeights && !!edgeAt(lanes, yAt(e)))
        if (scrubbing.current && e.buttons & 1) onSeek(frameAt(e))
        if (tool === 'razor') setHover(frameAt(e))
      }}
      onPointerUp={() => { scrubbing.current = false; resizing.current = undefined; setDragging(false) }}
      onPointerLeave={() => { setHover(undefined); setEdgeHover(false) }}
    >
      Версия {revision.number}: {revision.clips.length} кусков, {joints.length} стыков.
    </canvas>
  )
}

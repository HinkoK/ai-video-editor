// Расшифровка по макету «Монтаж · Таймлайн и Расшифровка» (08): строка
// файла и поиска, список фраз с исправлением прямо в строке, справа лёгкая
// копия файла. Список виртуальный: час речи — тысяча с лишним фраз, в
// странице только видимые. Поиск идёт по всем файлам проекта и на время
// заменяет список найденным.
import { useVirtualizer } from '@tanstack/react-virtual'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ErrorText, Select, StatePanel, VideoPlayer, errorMessage, type PlayerHandle } from '../components/common'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { fmtUs } from '../lib/time'
import { groupHits, hasProxy, highlight, segmentIndexAt, segmentStart, timingText } from '../lib/transcript'
import type { Tab } from '../App'
import type { Asset, Project, Segment } from '../lib/types'

const FLAG_LABEL: Record<string, string> = {
  high_impact: 'важные слова',
  no_speech: 'нет речи — возможно, выдумка распознавания',
  suspect_phrase: 'похоже на выдумку распознавания',
  low_confidence: 'низкая уверенность',
  repeated: 'та же фраза подряд — возможно, петля распознавания',
}

/** Больше стольких найденных фраз сервер не отдаёт (его limit по умолчанию) */
const SEARCH_LIMIT = 50
/** и больше стольких фраз файла за один запрос (limit в api.transcript) */
const LIST_LIMIT = 2000

const fileName = (a: Asset | undefined) => a?.path.split('/').pop() ?? ''
const isTyping = (t: EventTarget | null) =>
  t instanceof HTMLElement && !!t.closest('input, textarea, select, [contenteditable="true"]')

type Props = { project: Project; onOpenTab?: (tab: Tab) => void }

export function Transcript(props: Props) {
  // Другой проект — экран с чистого листа: файл, поиск, текущая фраза
  return <TranscriptScreen key={props.project.id} {...props} />
}

function TranscriptScreen({ project, onOpenTab }: Props) {
  const pid = project.id
  // Камера не расшифровывается: её звук только для синхронизации
  const spoken = project.assets.filter((a) => a.role !== 'camera')
  const [chosen, setChosen] = useState(spoken[0]?.id)
  const assetId = spoken.some((a) => a.id === chosen) ? chosen : spoken[0]?.id
  const asset = spoken.find((a) => a.id === assetId)
  const name = fileName(asset)
  const fileRef = (a: Asset) => `A${project.assets.indexOf(a) + 1} · ${fileName(a)}`

  // Текущая фраза — та, к которой перешли или которая звучит в плеере
  const [currentId, setCurrentId] = useState<string>()
  // Куда поставить плеер (время файла); меняется только по переходу
  const [jump, setJump] = useState<number>()
  const player = useRef<PlayerHandle>(null)
  // Исправленные фразы — сразу в списке, без повторной загрузки
  const [patched, setPatched] = useState<Record<string, Segment>>({})

  const data = useAsync(
    () => (assetId ? api.transcript(pid, assetId).then((r) => ({ assetId, ...r })) : Promise.resolve(undefined)),
    [pid, assetId],
  )
  const jobs = useAsync(() => api.jobs(pid), [pid])
  // Пока грузится другой файл, useAsync держит прежний ответ — его не показываем
  const fresh = data.data && data.data.assetId === assetId ? data.data : undefined
  const segments = useMemo(() => fresh?.segments.map((s) => patched[s.id] ?? s), [fresh, patched])
  const currentIndex = useMemo(
    () => (segments && currentId ? segments.findIndex((s) => s.id === currentId) : -1),
    [segments, currentId],
  )
  const proxy = !assetId ? false : jobs.error ? true : jobs.data ? hasProxy(jobs.data, assetId) : undefined

  const chooseFile = (id: string) => {
    setChosen(id)
    setJump(undefined)
    setCurrentId(undefined)
  }

  /** Перейти к фразе: она становится текущей, плеер — на её начало. */
  const go = (s: Segment) => {
    const us = segmentStart(s)
    setCurrentId(s.id)
    setJump(us)
    if (s.asset_id === assetId) player.current?.seekTo(us)
    else setChosen(s.asset_id)
  }

  // Плеер играет — подсвечиваем фразу, которая звучит
  const onTime = useCallback((us: number) => {
    if (!segments) return
    const i = segmentIndexAt(segments, us)
    if (i >= 0) setCurrentId(segments[i].id)
  }, [segments])

  const fix = async (s: Segment, text: string) => {
    const saved = await api.fixSegment(pid, s.id, text)
    setPatched((p) => ({ ...p, [saved.id]: saved }))
  }

  // ── Поиск ──
  const searchRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [found, setFound] = useState<{ q: string; hits?: Segment[]; error?: unknown; busy: boolean }>()
  const asked = useRef('')
  const seq = useRef(0)
  const runSearch = useCallback(async (q: string) => {
    asked.current = q
    const n = ++seq.current
    setFound((f) => (f ? { ...f, busy: true } : { q, busy: true }))
    try {
      const hits = await api.search(pid, q)
      if (n === seq.current) setFound({ q, hits, busy: false })
    } catch (error) {
      if (n === seq.current) setFound({ q, error, busy: false })
    }
  }, [pid])
  /** Новый запрос; пустой закрывает найденное и возвращает список. */
  const changeQuery = useCallback((value: string) => {
    setQuery(value)
    if (value.trim()) return
    asked.current = ''
    seq.current++ // ответ на прежний запрос уже не нужен
    setFound(undefined)
  }, [])
  // Ищем на ходу, после короткой паузы в наборе; Enter — сразу
  useEffect(() => {
    const q = query.trim()
    if (!q) return
    const t = window.setTimeout(() => { if (asked.current !== q) void runSearch(q) }, 250)
    return () => window.clearTimeout(t)
  }, [query, runSearch])

  // «/» — к поиску, Esc — закрыть найденное (не когда фокус в поле)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return
      if (e.code === 'Slash' && !e.shiftKey) {
        e.preventDefault()
        searchRef.current?.focus()
        searchRef.current?.select()
      } else if (e.code === 'Escape') {
        changeQuery('')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [changeQuery])

  const fileSelect = (
    <label className="tr-file">
      Файл
      <Select value={assetId ?? ''} onChange={(e) => chooseFile(e.target.value)}>
        {spoken.map((a) => <option key={a.id} value={a.id}>{fileRef(a)}</option>)}
      </Select>
    </label>
  )
  const toProject = () => (
    <a href="#" onClick={(e) => { e.preventDefault(); onOpenTab?.('setup') }}>Проект</a>
  )
  // На пустом экране и при ошибке строка нужна, только чтобы сменить файл
  const fileBar = spoken.length > 1 && <div className="subbar tr-bar">{fileSelect}</div>

  if (!asset) {
    return (
      <section className="screen tr" aria-labelledby="transcript-title">
        <h2 id="transcript-title" className="sr-only">Расшифровка</h2>
        <StatePanel state="empty" title="Файлов ещё нет">
          <p>Подключите файлы записи в разделе {toProject()} и запустите у них «Громкость, паузы и расшифровка».</p>
        </StatePanel>
      </section>
    )
  }

  if (!fresh && data.error) {
    return (
      <section className="screen tr" aria-labelledby="transcript-title">
        <h2 id="transcript-title" className="sr-only">Расшифровка</h2>
        {fileBar}
        <StatePanel state="error" title="Не удалось открыть расшифровку">
          <pre className="error-box">{errorMessage(data.error)}</pre>
          <p>Пересчитайте «Громкость, паузы и расшифровка» для {name} в разделе {toProject()}.</p>
          <div className="tr-actions">
            <button type="button" className="btn" onClick={data.reload}>Повторить</button>
          </div>
        </StatePanel>
      </section>
    )
  }

  if (!data.data) {
    return (
      <section className="screen tr" data-state="loading" aria-busy="true" aria-labelledby="transcript-title">
        <h2 id="transcript-title" className="sr-only">Расшифровка</h2>
        <p className="tr-loading" role="status">Загружаем расшифровку {name}…</p>
        <SkeletonRows />
      </section>
    )
  }

  if (fresh && !fresh.transcript) {
    return (
      <section className="screen tr" aria-labelledby="transcript-title">
        <h2 id="transcript-title" className="sr-only">Расшифровка</h2>
        {fileBar}
        <StatePanel state="empty" title={`Расшифровки для ${name} ещё нет`}>
          <p>
            Запустите «Громкость, паузы и расшифровка» у этого файла в разделе {toProject()}. Ход задачи будет виден там
            и в Задачах.
          </p>
        </StatePanel>
      </section>
    )
  }

  const info = fresh?.transcript
  return (
    <section className="screen fill tr" aria-labelledby="transcript-title">
      <h2 id="transcript-title" className="sr-only">Расшифровка</h2>
      <div className="subbar tr-bar">
        {fileSelect}
        {info ? (
          <p className="tr-info">
            Фраз: <span className="mono">{info.segments}</span>, язык: {info.language}. Точность времени — {timingText(info)}.
          </p>
        ) : (
          <p className="tr-info" role="status">Загружаем расшифровку {name}…</p>
        )}
        <form className="tr-search" role="search" onSubmit={(e) => { e.preventDefault(); if (query.trim()) void runSearch(query.trim()) }}>
          <label>
            <span className="sr-only">Поиск по расшифровке</span>
            <input
              ref={searchRef}
              type="search"
              value={query}
              placeholder="Поиск по тексту"
              aria-keyshortcuts="/"
              onChange={(e) => changeQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key !== 'Escape') return
                e.preventDefault()
                if (query) changeQuery('')
                else e.currentTarget.blur()
              }}
            />
            <kbd aria-hidden="true">/</kbd>
          </label>
        </form>
      </div>

      <div className="tr-body">
        <section className="tr-list" aria-labelledby="tr-list-title">
          {found ? (
            <Found
              found={found}
              groups={groupHits((found.hits ?? []).map((s) => patched[s.id] ?? s), project.assets)}
              title={fileRef}
              currentId={currentId}
              onGo={go}
              onOpen={(s) => { go(s); changeQuery('') }}
              onClear={() => { changeQuery(''); searchRef.current?.focus() }}
            />
          ) : segments && info ? (
            <SegmentList
              segments={segments}
              total={info.segments}
              currentIndex={currentIndex}
              onGo={go}
              onFix={fix}
            />
          ) : (
            <>
              <h3 id="tr-list-title" className="sr-only">Фразы</h3>
              <SkeletonRows />
            </>
          )}
        </section>

        <aside className="tr-side" aria-labelledby="tr-side-title">
          <div className="player-head">
            <h3 id="tr-side-title">Лёгкая копия</h3>
            <span>{name}</span>
          </div>
          {proxy ? (
            <VideoPlayer
              key={assetId}
              src={api.proxyUrl(pid, asset.id)}
              label={`Лёгкая копия ${fileRef(asset)}`}
              startUs={jump}
              handle={player}
              onTime={onTime}
            />
          ) : (
            <div className="video-frame" data-empty="">{proxy === false && <span>лёгкой копии ещё нет</span>}</div>
          )}
          {proxy === false ? (
            <p className="tr-side-hint">
              Лёгкая копия делается в разделе {toProject()}: «Лёгкая копия для просмотра» у этого файла. Текст фразы
              правится прямо в строке и без неё, исходный текст распознавания сохраняется.
            </p>
          ) : (
            <p className="tr-side-hint">
              Нажмите на время, чтобы перейти. Текст фразы правится прямо в строке, исходный текст распознавания
              сохраняется.
            </p>
          )}
        </aside>
      </div>
    </section>
  )
}

function SkeletonRows() {
  return (
    <div className="tr-skel" aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <div key={i}>
          <span className="skeleton strong" />
          <span className="skeleton" />
        </div>
      ))}
    </div>
  )
}

function Flags({ flags }: { flags: string[] }) {
  if (!flags.length) return null
  return (
    <ul className="tr-flags" aria-label="Пометки">
      {flags.map((f) => <li key={f} className="chip sm" data-flag={f}>{FLAG_LABEL[f] ?? f}</li>)}
    </ul>
  )
}

/** Исправленная фраза: значок и то, что распознал Whisper. */
function Corrected({ s }: { s: Segment }) {
  if (!s.text_corrected) return null
  return (
    <p className="tr-orig">
      <span className="chip sm plain">исправлено</span>
      <span>распознано: «{s.text_raw}»</span>
    </p>
  )
}

function SegmentList({ segments, total, currentIndex, onGo, onFix }: {
  segments: Segment[]
  total: number
  currentIndex: number
  onGo: (s: Segment) => void
  onFix: (s: Segment, text: string) => Promise<void>
}) {
  const parent = useRef<HTMLDivElement>(null)
  const virtual = useVirtualizer({
    count: segments.length,
    getScrollElement: () => parent.current,
    estimateSize: () => 41,
    overscan: 10,
  })
  // Исправление живёт здесь, а не в строке: строка за краем экрана исчезает
  const [edit, setEdit] = useState<{ id: string; text: string }>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>()

  // Текущая фраза всегда на виду: при открытии списка — посередине, потом —
  // только если ушла за край (плеер играет дальше)
  const shown = useRef(false)
  useEffect(() => {
    if (currentIndex < 0) return
    virtual.scrollToIndex(currentIndex, { align: shown.current ? 'auto' : 'center' })
    shown.current = true
  }, [currentIndex, virtual])

  const start = (s: Segment) => {
    setEdit({ id: s.id, text: s.text })
    setError(undefined)
  }
  const cancel = () => {
    setEdit(undefined)
    setError(undefined)
  }
  const save = async (s: Segment) => {
    if (!edit) return
    if (edit.text.trim() === s.text) return cancel()
    setSaving(true)
    setError(undefined)
    try {
      await onFix(s, edit.text)
      setEdit(undefined)
    } catch (e) {
      setError(e)
    } finally {
      setSaving(false)
    }
  }

  const items = virtual.getVirtualItems()
  // Видимые сейчас, без запаса по краям
  const range = virtual.range
  return (
    <>
      <h3 id="tr-list-title" className="sr-only">Фразы</h3>
      <div ref={parent} className="tr-scroll">
        <ol className="tr-rows" aria-labelledby="tr-list-title" style={{ height: virtual.getTotalSize() }}>
          {items.map((row) => {
            const s = segments[row.index]
            const current = row.index === currentIndex
            const t = fmtUs(segmentStart(s), 1)
            return (
              <li
                key={s.id}
                ref={virtual.measureElement}
                data-index={row.index}
                className="tr-row"
                data-flags={s.flags.join(' ')}
                aria-current={current ? 'true' : undefined}
                style={{ transform: `translateY(${row.start}px)` }}
              >
                <button
                  type="button"
                  className="time-link"
                  aria-current={current ? 'true' : undefined}
                  aria-label={`Перейти к ${t}`}
                  onClick={() => onGo(s)}
                >
                  {t}
                </button>
                <div className="tr-phrase">
                  {edit?.id === s.id ? (
                    <EditBox
                      segment={s}
                      text={edit.text}
                      saving={saving}
                      error={error}
                      onText={(text) => setEdit({ id: s.id, text })}
                      onSave={() => void save(s)}
                      onCancel={cancel}
                    />
                  ) : (
                    <>
                      <button type="button" className="tr-text" aria-label={`Исправить: ${s.text}`} onClick={() => start(s)}>
                        {s.text}
                      </button>
                      <Corrected s={s} />
                    </>
                  )}
                </div>
                <Flags flags={s.flags} />
              </li>
            )
          })}
        </ol>
      </div>
      <p className="tr-range">
        {segments.length === 0
          ? 'фраз нет'
          : `фразы ${(range?.startIndex ?? 0) + 1}–${(range?.endIndex ?? 0) + 1} из ${total}`}
        {segments.length < total && ` · загружены первые ${Math.min(segments.length, LIST_LIMIT)}`}
      </p>
    </>
  )
}

function EditBox({ segment: s, text, saving, error, onText, onSave, onCancel }: {
  segment: Segment
  text: string
  saving: boolean
  error: unknown
  onText: (text: string) => void
  onSave: () => void
  onCancel: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  // Сразу в поле, курсор — в конце текста
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])
  return (
    <div className="tr-edit">
      <label>
        <span className="sr-only">Текст фразы</span>
        <textarea
          ref={ref}
          rows={1}
          value={text}
          readOnly={saving}
          aria-busy={saving}
          aria-invalid={error ? 'true' : undefined}
          onChange={(e) => onText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              onSave()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              e.stopPropagation()
              onCancel()
            }
          }}
        />
      </label>
      <p className="tr-edit-meta">
        <span>распознано: «{s.text_raw}»</span>
        <span className="tr-edit-keys"><kbd>↵</kbd>сохранить<kbd>Esc</kbd>отменить</span>
      </p>
      <ErrorText error={error} />
    </div>
  )
}

/** Найденное по всем файлам проекта: по файлам, с отмеченными словами. */
function Found({ found, groups, title, currentId, onGo, onOpen, onClear }: {
  found: { q: string; hits?: Segment[]; error?: unknown; busy: boolean }
  groups: ReturnType<typeof groupHits>
  title: (a: Asset) => string
  currentId: string | undefined
  onGo: (s: Segment) => void
  onOpen: (s: Segment) => void
  onClear: () => void
}) {
  const n = found.hits?.length ?? 0
  const capped = n >= SEARCH_LIMIT
  return (
    <>
      <div className="tr-found-head">
        <h3 id="tr-list-title">
          {found.hits ? <>Найдено: <span className="mono">{capped ? `${n}+` : n}</span></> : found.error ? 'Поиск не удался' : 'Ищем…'}
        </h3>
        <span className="faint">
          {capped ? `по всем файлам · показаны первые ${SEARCH_LIMIT}, уточните запрос` : 'по всем файлам проекта'}
        </span>
        <button type="button" className="btn btn-quiet" onClick={onClear}>
          Очистить поиск <kbd>Esc</kbd>
        </button>
      </div>
      <div className="tr-scroll tr-found" role="region" aria-label="Найдено" aria-busy={found.busy}>
        <ErrorText error={found.error} />
        {found.hits && n === 0 && (
          <p className="tr-found-empty">
            По запросу «{found.q}» ничего нет. Ищем по началу слов во всех файлах проекта, без учёта регистра.
          </p>
        )}
        {groups.map((g) => (
          <section key={g.asset.id} aria-label={title(g.asset)}>
            <h4 className="group-title tr-group">
              {title(g.asset)} <span className="count">{g.hits.length}</span>
            </h4>
            <ol>
              {g.hits.map((s) => {
                const current = s.id === currentId
                const t = fmtUs(segmentStart(s), 1)
                return (
                  <li key={s.id} className="tr-row" data-flags={s.flags.join(' ')} aria-current={current ? 'true' : undefined}>
                    <button
                      type="button"
                      className="time-link"
                      aria-current={current ? 'true' : undefined}
                      aria-label={`Перейти к ${t} в ${title(g.asset)}`}
                      onClick={() => onGo(s)}
                    >
                      {t}
                    </button>
                    <div className="tr-phrase">
                      <button type="button" className="tr-text" aria-label={`Показать в списке: ${s.text}`} onClick={() => onOpen(s)}>
                        {highlight(s.text, found.q).map((p, i) => (p.hit ? <mark key={i}>{p.text}</mark> : p.text))}
                      </button>
                      <Corrected s={s} />
                    </div>
                    <Flags flags={s.flags} />
                  </li>
                )
              })}
            </ol>
          </section>
        ))}
      </div>
    </>
  )
}

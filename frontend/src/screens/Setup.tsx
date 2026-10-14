// Проект: слева файлы и подготовка (лёгкая копия, громкость, расшифровка),
// первая версия, бриф, защищённые места; справа — как Claude вносит правки,
// хранилище и как запустить Claude. Оформление — styles/setup.css.
import { Fragment, useEffect, useState, type FormEvent } from 'react'
import { ErrorText, JobStatus, Select, StatePanel, errorMessage } from '../components/common'
import { api } from '../lib/api'
import { useAsync, useInterval } from '../lib/hooks'
import { isActiveJob } from '../lib/jobInfo'
import { syncSummary } from '../lib/camera'
import { assetRef } from '../lib/review'
import { fmtUs, parseTime } from '../lib/time'
import type { Asset, BriefData, Job, Project, Protection } from '../lib/types'
import { baseName, capitalize, fmtBytes, fmtClock, fmtFps, shortPath } from '../lib/units'

export function Setup({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const pid = project.id
  const jobs = useAsync(() => api.jobs(pid), [pid])
  const busy = (jobs.data ?? []).some(isActiveJob)
  useInterval(jobs.reload, 2000, busy)

  if (!jobs.data && jobs.error) {
    return (
      <StatePanel state="error" title="Не удалось открыть проект">
        <pre className="error-box">{errorMessage(jobs.error)}</pre>
        <p>Проверьте, что сервер Монтажа запущен, и повторите. Файлы видео не затронуты.</p>
        <div><button type="button" className="btn" onClick={jobs.reload}>Повторить</button></div>
      </StatePanel>
    )
  }
  if (!jobs.data) {
    return (
      <div className="setup-state" data-state="loading" aria-busy="true">
        <p role="status">Загружаем проект «{project.name}»…</p>
        <span className="skeleton strong setup-sk-title" aria-hidden="true" />
        <span className="setup-sk-field" aria-hidden="true" />
        <span className="setup-sk-row" aria-hidden="true" />
        <span className="setup-sk-row" aria-hidden="true" />
      </div>
    )
  }
  if (project.assets.length === 0) {
    return (
      <div className="setup-empty">
        <StatePanel state="empty" title="Файлов пока нет">
          <p>Подключите камеру и запись экрана по полному пути. Файлы остаются на месте, Монтаж их не копирует и никуда не загружает.</p>
          <AddAsset pid={pid} placeholder="/Users/me/Видео/новый-ролик/камера.mp4" onAdded={onChanged} />
        </StatePanel>
      </div>
    )
  }

  return (
    <div className="screen setup-screen">
      <div className="setup-col">
        <Files project={project} jobs={jobs.data} onChanged={() => { jobs.reload(); onChanged() }} />
        <BriefEditor
          key={`${project.brief.active?.version ?? 0}:${project.brief.draft?.version ?? 0}`}
          project={project}
          onChanged={onChanged}
        />
        <Protections project={project} />
      </div>
      <div className="setup-col">
        <AgentMode project={project} onChanged={onChanged} />
        {project.assets.some((a) => a.role === 'camera') && <DefaultLayout project={project} />}
        {project.assets.some((a) => a.role === 'camera' && a.sync_state && a.sync_state !== 'pending' && a.sync_state !== 'failed') && (
          <AudioSourceSetting project={project} onChanged={onChanged} />
        )}
        <LoudnessSetting project={project} onChanged={onChanged} />
        <GraphicsSetting project={project} />
        <Storage project={project} />
        <RunClaude pid={pid} />
      </div>
    </div>
  )
}

/** Подключить файл по полному пути: он только читается, копия не делается. */
function AddAsset({ pid, placeholder, onAdded, hasMain = true }: {
  pid: string
  placeholder: string
  onAdded: () => void
  hasMain?: boolean
}) {
  const [path, setPath] = useState('')
  const [role, setRole] = useState<Asset['role']>('main')
  const [error, setError] = useState<unknown>()
  const [sending, setSending] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!path.trim()) return
    setError(undefined)
    setSending(true)
    try {
      await api.addAsset(pid, path.trim(), role)
      setPath('')
      onAdded()
    } catch (err) {
      setError(err)
    } finally {
      setSending(false)
    }
  }
  return (
    <div className="setup-add-wrap">
      <form className="setup-add" onSubmit={submit}>
        <label className="field">
          <span className="field-label">Полный путь к файлу</span>
          <input
            type="text"
            className="mono"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder={placeholder}
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <label className="field setup-add-role">
          <span className="field-label">Что это</span>
          <Select value={role} onChange={(e) => setRole(e.target.value as Asset['role'])}>
            <option value="main">Экран с микрофоном</option>
            <option value="camera" disabled={!hasMain}>Камера (снята одновременно)</option>
          </Select>
        </label>
        <button type="submit" className="btn" disabled={!path.trim() || sending}>Подключить</button>
      </form>
      {role === 'camera' && (
        <p className="field-hint">
          Камера сводится с записью экрана по звуку сама; её звук нужен только для этого. Частота кадров и размер
          могут отличаться.
        </p>
      )}
      <ErrorText error={error} />
    </div>
  )
}

const PREP = [
  ['proxy', 'Лёгкая копия для просмотра'],
  ['transcribe', 'Громкость, паузы и расшифровка'],
] as const

function Files({ project, jobs, onChanged }: { project: Project; jobs: Job[]; onChanged: () => void }) {
  const pid = project.id
  const [error, setError] = useState<unknown>()
  const run = async (action: () => Promise<unknown>) => {
    setError(undefined)
    try {
      await action()
      onChanged()
    } catch (e) {
      setError(e)
    }
  }
  // Задачи приходят новыми вперёд: первая найденная — последняя по времени
  const latest = (assetId: string, type: Job['type']) => jobs.find((j) => j.asset_id === assetId && j.type === type)
  // Первая версия ждёт, пока считаются громкость, паузы и расшифровка
  const waiting = project.assets.filter((a) => a.role !== 'camera' &&
    (isActiveJob(latest(a.id, 'transcribe')) || isActiveJob(latest(a.id, 'analyze'))))
  const waitingNames = waiting.map((a) => baseName(a.path)).join(', ')

  return (
    <section className="setup-files" aria-labelledby="setup-files-title">
      <h2 id="setup-files-title" className="section-title">Файлы</h2>
      <AddAsset
        pid={pid}
        placeholder="/Users/me/Видео/как-я-монтирую/экран.mp4"
        onAdded={onChanged}
        hasMain={project.assets.some((a) => a.role === 'main')}
      />
      <table className="table setup-assets">
        <thead>
          <tr>
            <th scope="col">Файл</th>
            <th scope="col">Длительность</th>
            <th scope="col">Частота кадров</th>
            <th scope="col">Кадр</th>
            <th scope="col">Подготовка</th>
          </tr>
        </thead>
        <tbody>
          {project.assets.map((a) => (
            <tr key={a.id}>
              <td>
                <div className="setup-asset">
                  <span>
                    <span className="setup-asset-ref mono">{assetRef(project.assets, a.id)}</span>{baseName(a.path)}
                    <span className="chip sm plain setup-asset-role">{a.role === 'camera' ? 'камера' : 'экран и звук'}</span>
                  </span>
                  <span className="setup-asset-path mono" title={a.path}>{shortPath(a.path)}</span>
                  {a.role === 'camera' && <CameraSync project={project} asset={a} jobs={jobs} onChanged={onChanged} />}
                </div>
              </td>
              <td className="mono">{fmtClock(a.duration_us)}</td>
              <td className="mono">{fmtFps(a.fps)}</td>
              <td className="mono">{a.width}×{a.height}</td>
              <td>
                <div className="setup-prep">
                  {PREP.filter(([type]) => a.role !== 'camera' || type === 'proxy').map(([type, label]) => {
                    const job = latest(a.id, type)
                    return (
                      <Fragment key={type}>
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={isActiveJob(job)}
                          onClick={() => run(() => (type === 'proxy' ? api.makeProxy(pid, a.id) : api.transcribe(pid, a.id)))}
                        >
                          {label}
                        </button>
                        <span className="setup-prep-state"><JobStatus job={job} /></span>
                      </Fragment>
                    )
                  })}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorText error={error} />
      {!project.current_revision_id && (
        <div className="setup-first" data-state={waiting.length ? 'blocked' : undefined}>
          <button
            type="button"
            className={waiting.length ? 'btn' : 'btn btn-primary'}
            disabled={waiting.length > 0}
            aria-describedby="setup-first-note"
            onClick={() => run(() => api.initialRevision(pid))}
          >
            Создать первую версию
          </button>
          <p id="setup-first-note">
            {waiting.length
              ? `Станет доступна, когда у ${waitingNames} закончится «Громкость, паузы и расшифровка».`
              : 'Записи экрана целиком, по порядку подключения; камера идёт за ними по синхронизации. Дальше правит Claude, вы проверяете.'}
          </p>
        </div>
      )}
    </section>
  )
}

const EMPTY: BriefData = {
  audience: '', promise: '', intended_outcome: '', genre: '', language: 'ru', mode: 'conservative_cleanup',
  required_points: [], forbidden_changes: [],
}

// В форме списки — текстом, по строке на пункт: так можно набрать перенос строки
type ListKey = 'required_points' | 'forbidden_changes'
type TextKey = 'audience' | 'promise' | 'intended_outcome' | 'genre'
type BriefForm = Omit<BriefData, ListKey> & Record<ListKey, string>
const FIELDS = ['audience', 'promise', 'intended_outcome', 'genre', 'language', 'required_points', 'forbidden_changes'] as const

const lines = (text: string) => text.split('\n').map((s) => s.trim()).filter(Boolean)
const toForm = (d: BriefData): BriefForm => ({
  ...EMPTY,
  ...d,
  required_points: (d.required_points ?? []).join('\n'),
  forbidden_changes: (d.forbidden_changes ?? []).join('\n'),
})
const fromForm = (f: BriefForm): BriefData => ({
  ...f,
  required_points: lines(f.required_points),
  forbidden_changes: lines(f.forbidden_changes),
})
const same = (key: (typeof FIELDS)[number], a: BriefForm, b: BriefForm) =>
  key === 'required_points' || key === 'forbidden_changes'
    ? lines(a[key]).join('\n') === lines(b[key]).join('\n')
    : a[key] === b[key]

/** Синхронизация камеры: итог, ручной сдвиг на кадр камеры, пересчёт. */
function CameraSync({ project, asset, jobs, onChanged }: {
  project: Project
  asset: Asset
  jobs: Job[]
  onChanged: () => void
}) {
  const [error, setError] = useState<unknown>()
  const job = jobs.find((j) => j.asset_id === asset.id && j.type === 'sync')
  const [num, den] = asset.fps.split('/').map(Number)
  const frameUs = Math.round((1_000_000 * (den || 1)) / (num || 1))
  const act = async (action: () => Promise<unknown>) => {
    setError(undefined)
    try {
      await action()
      onChanged()
    } catch (e) {
      setError(e)
    }
  }
  const synced = asset.sync_offset_us !== null && asset.sync_to !== null
  return (
    <div className="setup-sync" data-state={asset.sync_state ?? 'pending'}>
      <span className="setup-sync-text">
        {isActiveJob(job) ? <JobStatus job={job} /> : syncSummary(asset, project.assets)}
      </span>
      <span className="setup-sync-actions">
        {/* Время камеры = смещение + время записи: больше смещение — берётся
            более поздний кадр камеры, и движение губ приходит раньше */}
        <button type="button" className="btn btn-quiet" disabled={!synced} title="Если губы отстают от голоса"
          onClick={() => act(() => api.nudgeSync(project.id, asset.id, frameUs))}>лицо на кадр раньше</button>
        <button type="button" className="btn btn-quiet" disabled={!synced} title="Если губы опережают голос"
          onClick={() => act(() => api.nudgeSync(project.id, asset.id, -frameUs))}>лицо на кадр позже</button>
        <button type="button" className="btn btn-quiet" disabled={isActiveJob(job)}
          onClick={() => act(() => api.resync(project.id, asset.id))}>пересчитать</button>
      </span>
      <ErrorText error={error} />
    </div>
  )
}

function BriefEditor({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const pid = project.id
  const { active, draft } = project.brief
  const draftForm = draft ? toForm(draft.data) : undefined
  // Черновик Claude сразу в полях: его поля с пунктиром, пока их не поменяли
  const [form, setForm] = useState<BriefForm>(draftForm ?? toForm(active?.data ?? EMPTY))
  const [error, setError] = useState<unknown>()
  const [saving, setSaving] = useState(false)
  const edited = draftForm ? FIELDS.some((k) => !same(k, form, draftForm)) : true
  const fromDraft = (k: (typeof FIELDS)[number]) => (draftForm && same(k, form, draftForm) ? 'draft' : undefined)

  const save = async (action: () => Promise<unknown>) => {
    setError(undefined)
    setSaving(true)
    try {
      await action()
      onChanged()
    } catch (e) {
      setError(e)
    } finally {
      setSaving(false)
    }
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    // Без правок — подтверждаем черновик Claude; с правками — сохраняем свой бриф (он сразу действует)
    void save(() => (draft && !edited ? api.confirmBrief(pid, draft.version) : api.saveBrief(pid, fromForm(form))))
  }
  const text = (key: TextKey, label: string, wide = false) => (
    <label className={wide ? 'field wide' : 'field'} data-state={fromDraft(key)}>
      <span className="field-label">{label}</span>
      {key === 'intended_outcome' ? (
        <textarea rows={2} value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
      ) : (
        <input type="text" value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
      )}
    </label>
  )
  const list = (key: ListKey, label: string) => (
    <label className="field" data-state={fromDraft(key)}>
      <span className="field-label">{label}</span>
      <textarea rows={3} value={form[key]} placeholder="по строке на пункт" onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </label>
  )

  return (
    <section className="setup-brief" aria-labelledby="setup-brief-title">
      <div className="setup-brief-head">
        <h2 id="setup-brief-title" className="section-title">Бриф</h2>
        {draft && (
          <span className="chip sm" data-state="draft">черновик от {draft.created_by === 'agent' ? 'Claude' : 'автора'}</span>
        )}
      </div>
      <p className="setup-note">
        {draft
          ? `Черновик не действует, пока вы его не подтвердите. Поля с пунктиром заполнил Claude.${active ? ` Сейчас действует версия ${active.version}.` : ''}`
          : active
            ? `Действует версия ${active.version}. Изменения сохраняются новой версией и действуют сразу.`
            : 'Бриф ещё не задан. Заполните поля и сохраните — или попросите Claude предложить черновик.'}
      </p>
      <form className="setup-brief-form" onSubmit={submit}>
        <div className="setup-brief-grid">
          {text('audience', 'Для кого')}
          {text('promise', 'Что обещает название')}
          {text('intended_outcome', 'Что зритель поймёт или сможет сделать', true)}
          {text('genre', 'Жанр')}
          <label className="field" data-state={fromDraft('language')}>
            <span className="field-label">Язык</span>
            <Select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value as BriefData['language'] })}>
              <option value="ru">русский</option>
              <option value="uk">украинский</option>
            </Select>
          </label>
          {list('required_points', 'Обязательно сохранить')}
          {list('forbidden_changes', 'Нельзя менять')}
        </div>
        <div className="setup-actions">
          <button type="submit" className="btn" disabled={saving}>
            {draft && !edited ? 'Подтвердить бриф' : 'Сохранить бриф'}
          </button>
          {draftForm && edited && (
            <button type="button" className="btn btn-quiet" title="Вернуть в поля черновик Claude" onClick={() => setForm(draftForm)}>
              Взять в форму
            </button>
          )}
        </div>
      </form>
      <ErrorText error={error} />
    </section>
  )
}

const WHO: Record<Protection['created_by'], string> = { user: 'вы', agent: 'Claude', system: 'код' }

function Protections({ project }: { project: Project }) {
  const pid = project.id
  const list = useAsync(() => api.protections(pid), [pid])
  const [asset, setAsset] = useState(project.assets[0]?.id ?? '')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [reason, setReason] = useState('')
  const [invalid, setInvalid] = useState(false)
  const [error, setError] = useState<unknown>()
  const assetId = project.assets.some((a) => a.id === asset) ? asset : project.assets[0]?.id ?? ''
  const fileName = (id: string) => {
    const a = project.assets.find((x) => x.id === id)
    return a ? baseName(a.path) : id
  }

  const add = async (e: FormEvent) => {
    e.preventDefault()
    setError(undefined)
    const a = parseTime(from)
    const b = parseTime(to)
    if (a === null || b === null || b <= a) {
      setInvalid(true)
      setError(new Error('время: мм:сс.с, конец позже начала'))
      return
    }
    setInvalid(false)
    try {
      await api.addProtection(pid, { asset_id: assetId, src_start_us: a, src_end_us: b, kind: 'hard', reason })
      setFrom('')
      setTo('')
      setReason('')
      list.reload()
    } catch (err) {
      setError(err)
    }
  }
  const remove = async (id: string) => {
    setError(undefined)
    try {
      await api.removeProtection(pid, id)
      list.reload()
    } catch (err) {
      setError(err)
    }
  }

  return (
    <section className="setup-prot" aria-labelledby="setup-prot-title">
      <div className="setup-head">
        <h2 id="setup-prot-title" className="section-title">Защищённые места</h2>
        <p>Жёсткую ставите вы, мягкую — код, на фразы с отрицаниями и числами.</p>
      </div>
      {list.data && list.data.length > 0 && (
        <table className="table setup-prot-table">
          <thead>
            <tr>
              <th scope="col">Вид</th>
              <th scope="col">Файл</th>
              <th scope="col">Диапазон</th>
              <th scope="col">Причина</th>
              <th scope="col">Кто поставил</th>
              <th scope="col"><span className="sr-only">Действие</span></th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((p) => (
              <tr key={p.id} data-kind={p.kind}>
                <td><span className="mark" data-kind={p.kind}>{p.kind === 'hard' ? 'жёсткая' : 'мягкая'}</span></td>
                <td>{fileName(p.asset_id)}</td>
                <td className="mono setup-range">{fmtUs(p.src_start_us, 1)}–{fmtUs(p.src_end_us, 1)}</td>
                <td>{p.reason}</td>
                <td className="setup-who">{WHO[p.created_by] ?? p.created_by}</td>
                <td><button type="button" className="btn btn-quiet" onClick={() => remove(p.id)}>снять</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {list.data?.length === 0 && <p className="setup-note">Защищённых мест пока нет.</p>}
      {list.loading && !list.data && <p className="loading-line" data-state="loading">Загружаем защищённые места…</p>}

      <form className="setup-prot-add" onSubmit={add} aria-labelledby="setup-prot-add-title">
        <h3 id="setup-prot-add-title" className="block-title">Поставить жёсткую защиту</h3>
        <div className="setup-prot-fields">
          <label className="field">
            <span className="field-label">Файл</span>
            <Select value={assetId} onChange={(e) => setAsset(e.target.value)}>
              {project.assets.map((a) => (
                <option key={a.id} value={a.id}>{assetRef(project.assets, a.id)} · {baseName(a.path)}</option>
              ))}
            </Select>
          </label>
          <label className="field">
            <span className="field-label">Начало</span>
            <input type="text" className="mono" value={from} onChange={(e) => { setFrom(e.target.value); setInvalid(false) }} placeholder="мм:сс.с" aria-invalid={invalid || undefined} />
          </label>
          <label className="field">
            <span className="field-label">Конец</span>
            <input type="text" className="mono" value={to} onChange={(e) => { setTo(e.target.value); setInvalid(false) }} placeholder="мм:сс.с" aria-invalid={invalid || undefined} />
          </label>
          <label className="field">
            <span className="field-label">Причина</span>
            <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="почему важно" />
          </label>
          <button type="submit" className="btn" disabled={!reason.trim()}>Защитить</button>
        </div>
        <p className="field-hint">Правка, которая задевает жёсткую защиту, не применяется сама: она ждёт вашего решения и причины.</p>
      </form>
      <ErrorText error={error ?? list.error} />
    </section>
  )
}

const MODES = [
  ['apply', 'Сразу, я проверяю после',
    'Claude применяет правки к версии. Вы оставляете или возвращаете каждую в Проверке. Правки, задевающие жёсткую защиту, ждут вас.'],
  ['propose', 'Только предлагает, я утверждаю',
    'Claude присылает предложение целиком. Версия меняется, только когда вы его утвердите.'],
] as const

function AgentMode({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const [error, setError] = useState<unknown>()
  const set = async (mode: Project['agent_mode']) => {
    setError(undefined)
    try {
      await api.setMode(project.id, mode)
      onChanged()
    } catch (e) {
      setError(e)
    }
  }
  return (
    <fieldset className="setup-mode" data-mode={project.agent_mode}>
      <legend className="section-title">Как Claude вносит правки</legend>
      {MODES.map(([mode, title, desc]) => (
        <label key={mode} className="setup-mode-card">
          <input type="radio" name="agent-mode" checked={project.agent_mode === mode} onChange={() => set(mode)} />
          <span className="setup-mode-text">
            <span className="setup-mode-title">{title}</span>
            <span className="setup-mode-desc">{desc}</span>
          </span>
        </label>
      ))}
      <ErrorText error={error} />
    </fieldset>
  )
}

const PIP_SIZES: [number, string][] = [[0.18, 'маленькое'], [0.22, 'среднее'], [0.3, 'крупное']]
const PIP_SLOTS: [string, string][] = [['br', 'справа внизу'], ['bl', 'слева внизу'], ['tr', 'справа вверху'], ['tl', 'слева вверху']]

/** Кадр вне участков, которые ставит Claude: экран в рамке с лицом сбоку, с лицом в углу или без. */
function DefaultLayout({ project }: { project: Project }) {
  const layer = useAsync(() => api.visual(project.id), [project.id])
  const [error, setError] = useState<unknown>()
  const d = layer.data?.default
  const save = async (layout: 'screen' | 'pip' | 'side', params: { slot?: string; size?: number } = {}) => {
    setError(undefined)
    try {
      await api.setLayout(project.id, layout, params as never)
      layer.reload()
    } catch (e) {
      setError(e)
    }
  }
  if (!d) return <ErrorText error={layer.error} />
  const slot = d.slot ?? 'br'
  const size = d.size ?? 0.22
  return (
    <fieldset className="setup-mode setup-layout" data-layout={d.layout}>
      <legend className="section-title">Кадр по умолчанию</legend>
      <label className="setup-mode-card">
        <input type="radio" name="layout" checked={d.layout === 'side'} onChange={() => save('side')} />
        <span className="setup-mode-text">
          <span className="setup-mode-title">Экран в рамке, лицо сбоку</span>
          <span className="setup-mode-desc">Экран чуть уменьшен на тёмном фоне, лицо квадратом справа внизу и не закрывает показ.</span>
        </span>
      </label>
      <label className="setup-mode-card">
        <input type="radio" name="layout" checked={d.layout === 'pip'} onChange={() => save('pip', { slot, size })} />
        <span className="setup-mode-text">
          <span className="setup-mode-title">Экран с лицом в углу</span>
          <span className="setup-mode-desc">Где Claude не поставил другое: экран целиком, лицо — небольшим окном.</span>
          {d.layout === 'pip' && (
            <span className="setup-layout-opts">
              <Select small value={slot} aria-label="Угол" onChange={(e) => save('pip', { slot: e.target.value, size })}>
                {PIP_SLOTS.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
              </Select>
              <Select small value={String(size)} aria-label="Размер лица" onChange={(e) => save('pip', { slot, size: Number(e.target.value) })}>
                {PIP_SIZES.map(([v, t]) => <option key={v} value={String(v)}>{t}</option>)}
                {!PIP_SIZES.some(([v]) => v === size) && <option value={String(size)}>{Math.round(size * 100)}%</option>}
              </Select>
            </span>
          )}
        </span>
      </label>
      <label className="setup-mode-card">
        <input type="radio" name="layout" checked={d.layout === 'screen'} onChange={() => save('screen')} />
        <span className="setup-mode-text">
          <span className="setup-mode-title">Экран без лица</span>
          <span className="setup-mode-desc">Лицо появляется только там, где его поставил Claude или вы.</span>
        </span>
      </label>
      <ErrorText error={error} />
    </fieldset>
  )
}

/** Пометка «ожидание сокращено» у сокращённых ожиданий (правка waiting). */
function GraphicsSetting({ project }: { project: Project }) {
  const state = useAsync(() => api.graphics(project.id), [project.id])
  const [error, setError] = useState<unknown>()
  const on = state.data?.mark_waiting
  const toggle = async (value: boolean) => {
    setError(undefined)
    try {
      await api.setMarkWaiting(project.id, value)
      state.reload()
    } catch (e) {
      setError(e)
    }
  }
  return (
    <section className="setup-loudness" aria-labelledby="graphics-title">
      <h2 id="graphics-title" className="section-title">Графика</h2>
      <label className="check">
        <input type="checkbox" checked={!!on} disabled={on === undefined} onChange={(e) => void toggle(e.target.checked)} />
        Помечать сокращённое ожидание
      </label>
      <p className="setup-mode-desc">
        Где Claude сократил ожидание результата, в углу на полторы секунды появляется «ожидание сокращено»: зритель не
        решит, что инструмент работает мгновенно. Остальную графику ставит Claude, вы проверяете её в очереди.
      </p>
      <ErrorText error={error ?? state.error} />
    </section>
  )
}

const LOUDNESS: [number | null, string][] = [
  [-14, '−14 LUFS — громче'],
  [-16, '−16 LUFS — стартовое значение'],
  [-18, '−18 LUFS — тише'],
  [null, 'не выравнивать'],
]

/** Громкость экспорта: одно общее усиление до цели, без компрессии. */
/** Чей звук идёт в ролик: микрофон записи экрана или звук сведённой камеры. */
function AudioSourceSetting({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const [error, setError] = useState<unknown>()
  const set = async (value: string) => {
    setError(undefined)
    try {
      await api.setAudioSource(project.id, value as 'screen' | 'camera')
      onChanged()
    } catch (e) {
      setError(e)
    }
  }
  return (
    <section className="setup-loudness" aria-labelledby="audio-source-title">
      <h2 id="audio-source-title" className="section-title">Звук ролика</h2>
      <label className="field">
        <span className="field-label">В ролик идёт</span>
        <Select value={project.audio_source} onChange={(e) => void set(e.target.value)}>
          <option value="screen">микрофон записи экрана</option>
          <option value="camera">звук камеры</option>
        </Select>
      </label>
      <p className="setup-mode-desc">
        Правки и расшифровка всегда по записи экрана; звук камеры встаёт на то же место по синхронизации.
      </p>
      <ErrorText error={error} />
    </section>
  )
}

function LoudnessSetting({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const [error, setError] = useState<unknown>()
  const value = project.loudness_lufs
  const known = LOUDNESS.some(([v]) => v === value)
  const set = async (raw: string) => {
    setError(undefined)
    try {
      await api.setLoudness(project.id, raw === 'off' ? null : Number(raw))
      onChanged()
    } catch (e) {
      setError(e)
    }
  }
  return (
    <section className="setup-loudness" aria-labelledby="loudness-title">
      <h2 id="loudness-title" className="section-title">Громкость ролика</h2>
      <label className="field">
        <span className="field-label">При экспорте выравнивать до</span>
        <Select value={value === null ? 'off' : String(value)} onChange={(e) => void set(e.target.value)}>
          {!known && value !== null && <option value={String(value)}>{value} LUFS</option>}
          {LOUDNESS.map(([v, label]) => <option key={label} value={v === null ? 'off' : String(v)}>{label}</option>)}
        </Select>
      </label>
      <p className="setup-mode-desc">
        Одно общее усиление на весь ролик: характер голоса и разница между тихими и громкими местами не меняются.
        Если до цели не дотянуть, не перегрузив пики, усиление остановится раньше — это будет видно в отчёте экспорта.
      </p>
      <ErrorText error={error} />
    </section>
  )
}

function Storage({ project }: { project: Project }) {
  const usage = useAsync(() => api.storage(project.id), [project.id])
  const [error, setError] = useState<unknown>()
  const clean = async (key: string) => {
    setError(undefined)
    try {
      await api.cleanStorage(project.id, key)
      usage.reload()
    } catch (e) {
      setError(e)
    }
  }
  const data = usage.data
  // Мало места: свободного меньше полутора запасов, ниже которых работа не начинается
  const low = !!data && data.free_bytes < data.reserve_bytes * 1.5
  const total = data?.categories.reduce((n, c) => n + c.bytes, 0) ?? 0

  return (
    <section className="setup-storage" data-state={low ? 'warning' : undefined} aria-labelledby="setup-storage-title">
      <div className="setup-head setup-head-split">
        <h2 id="setup-storage-title" className="section-title">Хранилище</h2>
        {data && <span className="setup-storage-total mono" title="Занимает проект">{fmtBytes(total)}</span>}
      </div>
      {data && (low ? (
        <p role="status" className="setup-storage-warn">
          <span>
            <strong>Мало места на диске:</strong> свободно {fmtBytes(data.free_bytes)}, а работа не начнётся, если после
            записи останется меньше {fmtBytes(data.reserve_bytes)}. Очистите то, что можно пересчитать.
          </span>
        </p>
      ) : (
        <p className="setup-note">
          Свободно на диске: <span className="mono">{fmtBytes(data.free_bytes)}</span>. Работа не начнётся, если после записи
          останется меньше {fmtBytes(data.reserve_bytes)}.
        </p>
      ))}
      {usage.loading && !data && <p className="loading-line" data-state="loading">Считаем место…</p>}
      {data && (
        <ul className="setup-storage-list">
          {data.categories.map((c) => (
            <li key={c.key} data-recoverable={c.recoverable ? 'yes' : 'no'}>
              <span className="setup-storage-name">
                <span>{capitalize(c.title)}</span>
                <span className="setup-storage-note">{c.recoverable ? 'можно пересчитать' : 'нельзя пересчитать'} · {c.note}</span>
              </span>
              <span className="setup-storage-size mono">{fmtBytes(c.bytes)}</span>
              <span className="setup-storage-act">
                {c.recoverable && (
                  <button type="button" className="btn btn-quiet" disabled={c.bytes === 0} onClick={() => clean(c.key)}>Очистить</button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <ErrorText error={error ?? usage.error} />
    </section>
  )
}

function RunClaude({ pid }: { pid: string }) {
  const command = `uv run longform-agent ${pid}`
  const [copied, setCopied] = useState<'yes' | 'no'>()
  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(undefined), 2000)
    return () => window.clearTimeout(id)
  }, [copied])
  const copy = () => {
    if (!navigator.clipboard) {
      setCopied('no')
      return
    }
    navigator.clipboard.writeText(command).then(() => setCopied('yes'), () => setCopied('no'))
  }
  return (
    <section className="setup-run" aria-labelledby="setup-run-title">
      <h2 id="setup-run-title" className="section-title">Как запустить Claude</h2>
      <p className="setup-note">В терминале, в папке backend редактора:</p>
      <div className="setup-command">
        <code>{command}</code>
        <button type="button" className="btn btn-quiet" onClick={copy}>
          {copied === 'yes' ? 'Скопировано' : copied === 'no' ? 'Не скопировалось' : 'Скопировать'}
        </button>
      </div>
      <p className="setup-note">
        Команда создаст рабочую папку Claude для этого проекта и напечатает путь к <code>start.sh</code> — запустите его.
        У Claude есть только инструменты редактора; утверждаете вы, во вкладке «Проверка».
      </p>
    </section>
  )
}

// Каркас приложения по дизайну автора (AppHeader из Claude Design): шапка с
// проектом и разделами, экран раздела, заглушка для узкого экрана.
import { useEffect, useState } from 'react'
import { ErrorText, Select, StatePanel } from './components/common'
import { api } from './lib/api'
import { useAsync, usePref } from './lib/hooks'
import { bootstrapSession, type SessionState } from './lib/session'
import type { ProposalSummary } from './lib/types'
import { Export } from './screens/Export'
import { Jobs } from './screens/Jobs'
import { Review } from './screens/Review'
import { Setup } from './screens/Setup'
import { Timeline } from './screens/Timeline'
import { Transcript } from './screens/Transcript'

const TABS = [
  ['review', 'Проверка'],
  ['transcript', 'Расшифровка'],
  ['timeline', 'Таймлайн'],
  ['jobs', 'Задачи'],
  ['export', 'Экспорт'],
  ['setup', 'Проект'],
] as const
export type Tab = (typeof TABS)[number][0]

/** Сколько правок ждёт автора: непроверенные и отложенные сделанные Claude,
 *  плюс правки открытых предложений. */
export function reviewCount(proposals: ProposalSummary[]): number {
  return proposals.reduce(
    (n, p) => n + (p.status === 'open' ? p.ops_count : p.auto_applied ? p.unreviewed + p.held : 0),
    0,
  )
}

function NarrowNote() {
  return (
    <div className="narrow-note">
      <StatePanel state="empty" title="Откройте на компьютере">
        <p>Для монтажа нужен экран шириной от 1280 px. Расширьте окно браузера или откройте Монтаж на компьютере.</p>
      </StatePanel>
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState<SessionState | 'loading'>('loading')
  const [error, setError] = useState<unknown>()
  useEffect(() => {
    bootstrapSession().then(setSession, setError)
  }, [])
  if (error || session !== 'ok') {
    return (
      <div className="app">
        <header className="app-header"><div className="app-brand"><h1>Монтаж</h1></div></header>
        <main className="app-main">
          {error ? (
            <StatePanel state="error" title="Не удалось подключиться к редактору">
              <pre className="error-box">{String(error)}</pre>
              <p>Проверьте, что сервер запущен (<code>uv run longform-server</code>), и обновите страницу.</p>
            </StatePanel>
          ) : session === 'loading' ? (
            <p className="loading-line" data-state="loading" style={{ padding: 'var(--sp-5) var(--sp-4)' }}>Подключаюсь…</p>
          ) : (
            <StatePanel state="empty" title="Нет доступа">
              <p>Откройте редактор командой <code>uv run longform-open</code> в папке backend.</p>
            </StatePanel>
          )}
          <NarrowNote />
        </main>
      </div>
    )
  }
  return <Workspace />
}

function Workspace() {
  const projects = useAsync(() => api.projects(), [])
  const [projectId, setProjectId] = usePref<string>('longform.project', '')
  const [tab, setTab] = usePref<Tab>('longform.tab', 'review')
  const [name, setName] = useState('')
  const [error, setError] = useState<unknown>()
  const id = projects.data?.some((p) => p.id === projectId) ? projectId : projects.data?.[0]?.id
  const project = useAsync(() => (id ? api.project(id) : Promise.resolve(undefined)), [id])
  // Значок у «Проверки»; обновляется вместе с проектом (новая версия, решение)
  const proposals = useAsync(
    () => (id ? api.proposals(id) : Promise.resolve([])),
    [id, project.data?.current_revision_id, project.data?.agent_mode],
  )
  const count = reviewCount(proposals.data ?? [])

  const create = async () => {
    try {
      const p = await api.createProject(name)
      setName('')
      setProjectId(p.id)
      setTab('setup')
      projects.reload()
    } catch (e) {
      setError(e)
    }
  }
  const changed = () => {
    project.reload()
    proposals.reload()
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-brand">
          <h1>Монтаж</h1>
          {(projects.data?.length ?? 0) > 0 && (
            <Select value={id ?? ''} onChange={(e) => setProjectId(e.target.value)} aria-label="Проект">
              {(projects.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          )}
        </div>
        {project.data && (
          <nav className="app-nav" aria-label="Разделы">
            {TABS.map(([key, label]) => (
              <button key={key} type="button" aria-current={tab === key ? 'page' : undefined} onClick={() => setTab(key)}>
                {label}
                {key === 'review' && count > 0 && <span className="count">{count}</span>}
              </button>
            ))}
          </nav>
        )}
        <form className="app-new" onSubmit={(e) => { e.preventDefault(); if (name.trim()) void create() }}>
          <label>
            <span className="sr-only">Название нового проекта</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Новый проект" />
          </label>
          <button type="submit" className="btn">Создать</button>
        </form>
      </header>
      <main className="app-main" data-screen={tab}>
        <ErrorText error={error ?? projects.error ?? project.error} />
        {projects.data?.length === 0 && (
          <StatePanel state="empty" title="Проектов ещё нет">
            <p>Введите название в поле «Новый проект» справа вверху и нажмите «Создать». Потом подключите файлы записи.</p>
          </StatePanel>
        )}
        {project.data && (
          <>
            {tab === 'review' && <Review project={project.data} onChanged={changed} onOpenTab={setTab} />}
            {tab === 'transcript' && <Transcript project={project.data} onOpenTab={setTab} />}
            {tab === 'timeline' && <Timeline project={project.data} onOpenTab={setTab} onChanged={changed} />}
            {tab === 'jobs' && <Jobs project={project.data} />}
            {tab === 'export' && <Export project={project.data} onOpenTab={setTab} />}
            {tab === 'setup' && <Setup project={project.data} onChanged={changed} />}
          </>
        )}
        <NarrowNote />
      </main>
    </div>
  )
}

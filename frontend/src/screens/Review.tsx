// Проверка — первый экран. Режим «сразу»: Claude уже правил, автор
// проверяет каждую правку после (оставить, вернуть, применить отложенное).
// Режим «предлагает»: автор решает по открытому предложению до применения.
// Если есть и то и другое (режим сменили после), открытые предложения —
// отдельной группой сверху очереди, по выбору открываются своим видом.
import { useState, type ReactNode } from 'react'
import { errorMessage, StatePanel } from '../components/common'
import type { Tab } from '../App'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { fromGraphic, fromShot } from '../lib/layer'
import { buildQueue } from '../lib/reviewQueue'
import type { Joint, Project } from '../lib/types'
import { ProposalView } from './review/ProposalView'
import { QueueView } from './review/QueueView'
import { Skeleton } from './review/Skeleton'
import { modeSentence, usePreviews } from './review/shared'
import { Subbar } from './review/Subbar'

export function Review({ project, onChanged, onOpenTab }: {
  project: Project
  onChanged: () => void
  onOpenTab?: (tab: Tab) => void
}) {
  const pid = project.id
  const rid = project.current_revision_id
  const rate = project.fps_num && project.fps_den ? { num: project.fps_num, den: project.fps_den } : undefined
  const summaries = useAsync(() => api.proposals(pid), [pid, rid, project.agent_mode])
  // Сделанное Claude в режиме «сразу» — все применённые предложения, и
  // проверенные тоже: они внизу очереди свёрнутой группой
  const appliedIds = (summaries.data ?? []).filter((p) => p.auto_applied).map((p) => p.id)
  const idsKey = appliedIds.join(',')
  const applied = useAsync(
    async () => ({ key: idsKey, list: await Promise.all(appliedIds.map((id) => api.proposal(pid, id))) }),
    [pid, rid, idsKey],
  )
  const revisions = useAsync(() => api.revisions(pid), [pid, rid])
  const joints = useAsync(() => (rid ? api.joints(pid, rid) : Promise.resolve<Joint[]>([])), [pid, rid])
  const jobs = useAsync(() => api.jobs(pid), [pid, rid])
  // Слой кадра: участки, которые Claude поставил (лицо, приближение экрана)
  const [layerTick, setLayerTick] = useState(0)
  const layer = useAsync(() => (rid ? api.visual(pid, rid) : Promise.resolve(undefined)), [pid, rid, layerTick])
  const drawn = useAsync(() => (rid ? api.graphics(pid, rid) : Promise.resolve(undefined)), [pid, rid, layerTick])
  const previews = usePreviews(pid, rate, jobs.data)
  // undefined — вид по режиму; 'queue' — очередь; иначе id открытого предложения
  const [view, setView] = useState<string>()

  const changed = () => {
    summaries.reload()
    applied.reload()
    setLayerTick((t) => t + 1)
    onChanged()
  }
  const retry = () => {
    summaries.reload()
    applied.reload()
  }
  const revisionNumber = revisions.data?.find((r) => r.id === rid)?.number
  const error = summaries.error ?? applied.error
  const loading = !error && (!summaries.data || applied.data?.key !== idsKey)

  const shell = (state: 'loading' | 'error' | 'empty' | undefined, body: ReactNode) => (
    <section
      className="screen fill rv"
      aria-label="Проверка"
      data-mode={project.agent_mode}
      data-state={state}
      aria-busy={state === 'loading' || undefined}
    >
      {body}
    </section>
  )

  if (error) {
    return shell('error', (
      <>
        <Subbar>{modeSentence(project.agent_mode)}</Subbar>
        <StatePanel state="error" title="Не удалось загрузить правки">
          <pre className="error-box">{errorMessage(error)}</pre>
          <p>
            Проверьте, что сервер Монтажа запущен, и повторите. Если не помогает, откройте{' '}
            <button type="button" className="rv-link" onClick={() => onOpenTab?.('jobs')}>Задачи</button>: там видно,
            отвечает ли исполнитель.
          </p>
          <div className="rv-panel-actions">
            <button type="button" className="btn" onClick={retry}>Повторить</button>
          </div>
        </StatePanel>
      </>
    ))
  }
  if (loading || !summaries.data || !applied.data) {
    return shell('loading', (
      <>
        <Subbar>{revisionNumber !== undefined ? `Загружаем правки версии ${revisionNumber}…` : 'Загружаем правки…'}</Subbar>
        <Skeleton />
      </>
    ))
  }

  const items = buildQueue(applied.data.list)
  const open = summaries.data.filter((p) => p.status === 'open')
  const auto = summaries.data.filter((p) => p.auto_applied)
  const unreviewed = auto.reduce((n, p) => n + p.unreviewed, 0)
  const held = auto.reduce((n, p) => n + p.held, 0)
  const activeCount = items.filter((i) => i.group !== 'done').length

  const shots = [...(layer.data?.segments ?? []).map(fromShot), ...(drawn.data?.graphics ?? []).map(fromGraphic)]
    .sort((a, b) => a.src_start_us - b.src_start_us)
  if ((items.length === 0 && open.length === 0 && shots.length === 0) || !rate) {
    return shell('empty', (
      <>
        <Subbar>{modeSentence(project.agent_mode)} Правок пока нет.</Subbar>
        <StatePanel state="empty" title="Правок пока нет">
          {rid ? (
            <>
              <p>
                Claude ещё не правил версию{revisionNumber !== undefined && ` ${revisionNumber}`}. Подготовьте его рабочую
                папку командой ниже, запустите в ней Claude и попросите сделать черновой монтаж.
              </p>
              <pre className="rv-command">uv run longform-agent {pid}</pre>
            </>
          ) : (
            <p>У проекта ещё нет версии монтажа: подключите файлы записи и создайте первую версию.</p>
          )}
          <p className="rv-panel-note">
            Команда и режим правок — в разделе{' '}
            <button type="button" className="rv-link" onClick={() => onOpenTab?.('setup')}>Проект</button>.
          </p>
        </StatePanel>
      </>
    ))
  }

  const openId = view && view !== 'queue' && open.some((p) => p.id === view) ? view : undefined
  const proposalId = openId ?? (
    view === 'queue' && items.length > 0 ? undefined
      : open.length > 0 && (project.agent_mode === 'propose' || activeCount === 0) ? open[0].id
      : undefined
  )

  return shell(undefined, proposalId ? (
    <ProposalView
      key={proposalId}
      project={project}
      proposalId={proposalId}
      rate={rate}
      open={open}
      unreviewed={unreviewed}
      held={held}
      previews={previews}
      onSwitch={setView}
      onBack={items.length > 0 ? () => setView('queue') : undefined}
      onDecided={() => {
        setView(undefined)
        changed()
      }}
    />
  ) : (
    <QueueView
      project={project}
      rate={rate}
      items={items}
      proposals={applied.data.list}
      open={open}
      unreviewed={unreviewed}
      held={held}
      joints={joints.data}
      revisionNumber={revisionNumber}
      previews={previews}
      onOpenProposal={setView}
      onChanged={changed}
      onOpenTab={onOpenTab}
      shots={shots}
    />
  ))
}

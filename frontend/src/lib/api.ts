// Клиент сервера редактора. Ключ живёт в HttpOnly cookie (см. session.ts),
// поэтому скрипты страницы его не видят, а <video> получает файлы сам.
import type {
  AgentMode, Asset, Brief, BriefData, Chapters, Comment, Job, Joint, Marker, Project, ProjectSummary, Proposal,
  ProposalSummary, Protection, QaReport, Revision, RevisionSummary, Segment, Silence, StorageUsage,
  GraphicItem, Thumbs, TranscriptInfo, VisualLayer, VisualSegment, Waveform, WorkerStatus,
} from './types'
import type { CutOp, MoveOp } from './edit'

export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.code = code
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    throw new ApiError(data?.code ?? `http_${res.status}`, data?.message ?? res.statusText, res.status)
  }
  return data as T
}

const p = (pid: string) => `/projects/${pid}`

export const api = {
  // Сессия
  exchangeKey: (key: string) =>
    fetch('/api/session/cookie', { method: 'POST', headers: { Authorization: `Bearer ${key}` } }),
  session: () => request<{ actor: string }>('GET', '/session'),
  worker: () => request<WorkerStatus>('GET', '/worker'),

  // Проекты и исходники
  projects: () => request<ProjectSummary[]>('GET', '/projects'),
  createProject: (name: string) => request<Project>('POST', '/projects', { name }),
  project: (pid: string) => request<Project>('GET', p(pid)),
  addAsset: (pid: string, path: string, role: Asset['role'] = 'main') =>
    request<Asset>('POST', `${p(pid)}/assets`, { path, role }),
  resync: (pid: string, aid: string) => request<Job>('POST', `${p(pid)}/assets/${aid}/sync`),
  nudgeSync: (pid: string, aid: string, delta_us: number) =>
    request<Asset>('POST', `${p(pid)}/assets/${aid}/sync/nudge`, { delta_us }),
  setMode: (pid: string, agent_mode: AgentMode) => request<Project>('POST', `${p(pid)}/settings`, { agent_mode }),
  setLoudness: (pid: string, loudness_lufs: number | null) =>
    request<Project>('POST', `${p(pid)}/settings`, { loudness_lufs }),
  setAudioSource: (pid: string, audio_source: 'screen' | 'camera') =>
    request<Project>('POST', `${p(pid)}/settings`, { audio_source }),
  storage: (pid: string) => request<StorageUsage>('GET', `${p(pid)}/storage`),
  cleanStorage: (pid: string, key: string) => request<StorageUsage>('DELETE', `${p(pid)}/storage/${key}`),
  makeProxy: (pid: string, aid: string) => request<Job>('POST', `${p(pid)}/assets/${aid}/proxy`),
  analyze: (pid: string, aid: string) => request<Job>('POST', `${p(pid)}/assets/${aid}/analyze`),
  transcribe: (pid: string, aid: string) => request<Job>('POST', `${p(pid)}/assets/${aid}/transcribe`, {}),
  proxyUrl: (pid: string, aid: string) => `/api${p(pid)}/assets/${aid}/proxy`,
  /** Полоса кадров для дорожки видео (собирается из лёгкой копии) */
  thumbs: (pid: string, aid: string) => request<Thumbs>('GET', `${p(pid)}/assets/${aid}/thumbs`),
  thumbsUrl: (pid: string, aid: string) => `/api${p(pid)}/assets/${aid}/thumbs.jpg`,

  // Версии
  initialRevision: (pid: string) => request<Revision>('POST', `${p(pid)}/revisions/initial`),
  revisions: (pid: string) => request<RevisionSummary[]>('GET', `${p(pid)}/revisions`),
  revision: (pid: string, rid: string) => request<Revision>('GET', `${p(pid)}/revisions/${rid}`),
  joints: (pid: string, rid: string) => request<Joint[]>('GET', `${p(pid)}/revisions/${rid}/joints`),
  revert: (pid: string, rid: string) => request<Revision>('POST', `${p(pid)}/revisions/${rid}/revert`),
  nudge: (pid: string, rid: string, joint: number, side: 'before' | 'after', frames: number) =>
    request<Proposal>('POST', `${p(pid)}/revisions/${rid}/nudge`, { joint, side, frames }),

  // Предложения
  proposals: (pid: string) => request<ProposalSummary[]>('GET', `${p(pid)}/proposals`),
  proposal: (pid: string, id: string) => request<Proposal>('GET', `${p(pid)}/proposals/${id}`),
  /** Правка от автора (ручной монтаж на таймлайне) — потом approve */
  createProposal: (pid: string, body: { base_revision_id: string; rationale: string; policy_version: string; ops: (CutOp | MoveOp)[] }) =>
    request<Proposal>('POST', `${p(pid)}/proposals`, body),
  approve: (pid: string, id: string, body: {
    exclude_op_ids: string[]; acknowledge_op_ids: string[]; overrides: Record<string, string>
  }) => request<Proposal>('POST', `${p(pid)}/proposals/${id}/approve`, body),
  reject: (pid: string, id: string) => request<Proposal>('POST', `${p(pid)}/proposals/${id}/reject`),
  /** Проверка после применения: оставить, вернуть, применить отложенную */
  reviewOp: (pid: string, id: string, opId: string, action: 'keep' | 'revert' | 'apply', reason = '') =>
    request<Proposal>('POST', `${p(pid)}/proposals/${id}/ops/${opId}/review`, { action, reason }),

  // Задачи
  jobs: (pid: string) => request<Job[]>('GET', `${p(pid)}/jobs`),
  job: (pid: string, jid: string) => request<Job>('GET', `${p(pid)}/jobs/${jid}`),
  cancelJob: (pid: string, jid: string) => request<Job>('POST', `${p(pid)}/jobs/${jid}/cancel`),
  jobOutputUrl: (pid: string, jid: string) => `/api${p(pid)}/jobs/${jid}/output`,
  preview: (pid: string, rid: string, out_start_frame: number, out_end_frame: number) =>
    request<Job>('POST', `${p(pid)}/revisions/${rid}/preview`, { out_start_frame, out_end_frame }),
  exportRevision: (pid: string, rid: string) => request<Job>('POST', `${p(pid)}/revisions/${rid}/export`),

  // Расшифровка и звук
  transcript: (pid: string, aid: string, fromUs = 0, toUs?: number, limit = 2000) =>
    request<{ transcript: TranscriptInfo | null; segments: Segment[] }>(
      'GET',
      `${p(pid)}/assets/${aid}/transcript?from_us=${fromUs}${toUs === undefined ? '' : `&to_us=${toUs}`}&limit=${limit}`,
    ),
  search: (pid: string, q: string) =>
    request<Segment[]>('GET', `${p(pid)}/transcript/search?q=${encodeURIComponent(q)}`),
  fixSegment: (pid: string, sid: string, text: string) =>
    request<Segment>('PATCH', `${p(pid)}/segments/${sid}`, { text }),
  silences: (pid: string, aid: string) => request<Silence[]>('GET', `${p(pid)}/assets/${aid}/silences`),
  waveform: (pid: string, aid: string, fromUs: number, toUs: number, points: number) =>
    request<Waveform>('GET', `${p(pid)}/assets/${aid}/waveform?from_us=${fromUs}&to_us=${toUs}&points=${points}`),

  // Бриф, защита, комментарии
  brief: (pid: string) => request<Brief>('GET', `${p(pid)}/brief`),
  saveBrief: (pid: string, data: Partial<BriefData>) => request<Brief>('POST', `${p(pid)}/brief`, data),
  confirmBrief: (pid: string, version: number) => request<Brief>('POST', `${p(pid)}/brief/${version}/confirm`),
  protections: (pid: string) => request<Protection[]>('GET', `${p(pid)}/protected`),
  addProtection: (pid: string, body: Omit<Protection, 'id' | 'created_by'>) =>
    request<Protection>('POST', `${p(pid)}/protected`, body),
  removeProtection: (pid: string, id: string) => request<void>('DELETE', `${p(pid)}/protected/${id}`),
  comments: (pid: string, rid: string) => request<Comment[]>('GET', `${p(pid)}/comments?revision_id=${rid}`),
  addComment: (pid: string, body: { text: string; revision_id?: string; edited_frame?: number }) =>
    request<Comment>('POST', `${p(pid)}/comments`, body),
  resolveComment: (pid: string, id: string) => request<Comment>('POST', `${p(pid)}/comments/${id}/resolve`),

  // Метки
  markers: (pid: string, rid?: string) => request<Marker[]>('GET', `${p(pid)}/markers${rid ? `?revision_id=${rid}` : ''}`),
  addMarker: (pid: string, body: { edited_frame: number; revision_id?: string; label?: string; kind?: Marker['kind'] }) =>
    request<Marker>('POST', `${p(pid)}/markers`, body),
  removeMarker: (pid: string, id: string) => request<void>('DELETE', `${p(pid)}/markers/${id}`),

  // Слой кадра: что видно в каждом месте ролика
  visual: (pid: string, rid?: string) =>
    request<VisualLayer>('GET', `${p(pid)}/visual${rid ? `?revision_id=${rid}` : ''}`),
  reviewVisual: (pid: string, vid: string, action: 'keep' | 'revert') =>
    request<VisualSegment>('POST', `${p(pid)}/visual/${vid}/review`, { action }),
  /** автор поправил участок на мониторе (окно лица, рамка экрана) */
  patchVisual: (pid: string, vid: string, params: VisualSegment['params']) =>
    request<VisualSegment>('PATCH', `${p(pid)}/visual/${vid}`, { params }),
  /** автор сдвинул графику на мониторе или поправил её текст */
  patchGraphic: (pid: string, gid: string, props: Record<string, unknown>) =>
    request<GraphicItem>('PATCH', `${p(pid)}/graphics/${gid}`, { props }),
  pages: (pid: string) => request<{ id: string; url: string; width: number; height: number; bg: string }[]>('GET', `${p(pid)}/pages`),
  brollUrl: (pid: string, name: string) => `/api${p(pid)}/broll/${encodeURIComponent(name)}/video`,
  backgroundUrl: (pid: string, name: string) => `/api${p(pid)}/backgrounds/${encodeURIComponent(name)}/video`,
  setLayout: (pid: string, layout: VisualSegment['layout'], params: VisualSegment['params'] = {}) =>
    request<VisualLayer['default']>('POST', `${p(pid)}/layout`, { layout, params }),

  // Моушн-графика поверх кадра
  graphics: (pid: string, rid?: string) =>
    request<{ mark_waiting: boolean; graphics: GraphicItem[] }>('GET', `${p(pid)}/graphics${rid ? `?revision_id=${rid}` : ''}`),
  reviewGraphic: (pid: string, gid: string, action: 'keep' | 'revert') =>
    request<GraphicItem>('POST', `${p(pid)}/graphics/${gid}/review`, { action }),
  setMarkWaiting: (pid: string, on: boolean) =>
    request<{ mark_waiting: boolean }>('POST', `${p(pid)}/graphics/mark-waiting`, { on }),

  // Субтитры и главы для YouTube
  captionsUrl: (pid: string, rid: string) => `/api${p(pid)}/revisions/${rid}/captions.srt`,
  chapters: (pid: string, rid: string) => request<Chapters>('GET', `${p(pid)}/revisions/${rid}/chapters`),

  // Отчёт качества
  qa: (pid: string) => request<QaReport[]>('GET', `${p(pid)}/qa`),
  qaHuman: (pid: string, id: string, status: 'approved' | 'changes_requested', note: string) =>
    request<QaReport>('POST', `${p(pid)}/qa/${id}/human`, { status, note }),
}

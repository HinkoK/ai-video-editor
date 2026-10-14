// Типы ответов сервера редактора (backend/src/longform/api.py).
// Время исходника — целые микросекунды, время монтажа — кадры.

export type Actor = 'user' | 'agent'

export type AssetRole = 'main' | 'camera'

export interface SyncReport {
  points: { main_us: number; offset_us: number; peak: number; used: boolean }[]
  residual_us: number | null
  coarse_peak: number
  measured: { offset_us: number | null; drift_ppm: number; state: string }
}

export interface Asset {
  id: string
  position: number
  path: string
  /** main — запись экрана с микрофоном; camera — камера, снятая одновременно */
  role: AssetRole
  /** Для камеры: с какой записью сведена; время камеры = offset + время записи × (1 + drift/1e6) */
  sync_to: string | null
  sync_offset_us: number | null
  sync_drift_ppm: number | null
  sync_state: 'pending' | 'ok' | 'weak' | 'failed' | 'manual' | null
  sync: SyncReport | null
  fps: string
  frame_count: number
  duration_us: number
  width: number
  height: number
}

export interface BriefData {
  audience: string
  promise: string
  intended_outcome: string
  genre: string
  language: 'ru' | 'uk'
  mode: 'conservative_cleanup'
  required_points: string[]
  forbidden_changes: string[]
}

export interface BriefVersion {
  version: number
  data: BriefData
  created_by: Actor
  created_at: string
  confirmed_at: string | null
}

export interface Brief {
  active: BriefVersion | null
  draft: BriefVersion | null
}

export interface ProjectSummary {
  id: string
  name: string
  created_at: string
  current_revision_id: string | null
}

export type AgentMode = 'apply' | 'propose'

export interface Project extends ProjectSummary {
  agent_mode: AgentMode
  /** Громкость экспорта в LUFS; null — не выравнивать */
  loudness_lufs: number | null
  /** Чей звук в ролике: микрофон записи экрана или звук камеры */
  audio_source: 'screen' | 'camera'
  fps_num: number | null
  fps_den: number | null
  sample_rate: number | null
  assets: Asset[]
  brief: Brief
}

export interface RevisionClip {
  id: string
  asset_id: string
  src_start_us: number
  src_end_us: number
  out_start_frame: number
  out_end_frame: number
}

/** Перестановка куска в версии (step-29): все оставленные кадры файла из
 *  [src_start_us, src_end_us) идут подряд после момента target_src_us (after)
 *  или перед ним (before); где кусок в ролике и номера трёх его стыков */
export interface RevisionMove {
  op_id: string
  asset_id: string
  src_start_us: number
  src_end_us: number
  target_asset_id: string
  target_src_us: number
  side: 'after' | 'before'
  out_start_frame: number | null
  out_end_frame: number | null
  joints: { in: number | null; out: number | null; from: number | null }
}

export interface Revision {
  id: string
  number: number
  parent_id: string | null
  state: 'candidate' | 'approved' | 'discarded'
  description: string
  created_by: Actor
  created_at: string
  fps: string
  total_frames: number
  duration_us: number
  clips: RevisionClip[]
  /** Слой перестановок версии (пустой — куски в порядке записи) */
  moves?: RevisionMove[]
}

export interface RevisionSummary {
  id: string
  number: number
  state: Revision['state']
  description: string
  created_by: Actor
  created_at: string
}

export interface Joint {
  out_frame: number
  before_asset: string
  before_src_end_us: number
  after_asset: string
  after_src_start_us: number
  /** cut — между кусками только вырезанное; move — стык перестановки (вход
   *  куска, выход или место, откуда он взят) */
  kind?: 'cut' | 'move'
}

export type RiskFlag =
  | 'protected' | 'high_impact' | 'reason_other' | 'long_cut' | 'cut_in_speech' | 'reorder' | 'splits_graphic'

export interface Op {
  id: string
  position: number
  /** move — перестановка куска, unmove — её снятие («вернуть как было») */
  kind: 'remove' | 'restore' | 'move' | 'unmove'
  asset_id: string
  src_start_us: number
  src_end_us: number
  requested_start_us: number | null
  requested_end_us: number | null
  adjust_note: string | null
  reason: string
  why: string
  kept_alternative: [string, number, number] | null
  segment_ids: string[]
  risk_flags: RiskFlag[]
  decision: 'pending' | 'accepted' | 'excluded'
  override_reason: string | null
  candidate_joint_frame: number | null
  /** Кадр стыка этой правки в текущей версии — для проверки после применения */
  current_joint_frame: number | null
  /** pending — автор ещё не смотрел, kept — оставил, reverted — вернул, held — ждёт его */
  review: 'pending' | 'kept' | 'reverted' | 'held'
  /** Перестановка: куда встаёт кусок — после момента исходника или перед ним */
  target?: { asset_id: string; src_us: number; side: 'after' | 'before' } | null
  /** Снятие перестановки: id правки move */
  undo_of?: string | null
}

export interface ProposalSummary {
  id: string
  number: number
  status: 'open' | 'approved' | 'rejected' | 'stale'
  base_revision_id: string
  candidate_revision_id: string
  result_revision_id: string | null
  rationale: string
  created_by: Actor
  created_at: string
  auto_applied: number
  unreviewed: number
  held: number
  /** Всего правок в предложении */
  ops_count: number
}

export interface Proposal extends ProposalSummary {
  pass: string
  policy_version: string
  notes: string[]
  ops: Op[]
}

export type JobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'

export interface Job {
  id: string
  type: 'proxy' | 'preview' | 'export' | 'analyze' | 'transcribe' | 'sync'
  asset_id: string | null
  revision_id: string | null
  revision_number?: number
  state: JobState
  attempts: number
  progress: number
  message: string
  error: string | null
  output_path: string | null
  created_by: Actor
  created_at: string
  draft?: boolean
  stale?: boolean
  params: Record<string, unknown>
}

export interface Segment {
  id: string
  idx: number
  asset_id: string
  src_start_us: number
  src_end_us: number
  speech_start_us: number | null
  speech_end_us: number | null
  text: string
  text_raw: string
  text_corrected: string | null
  avg_p: number | null
  flags: string[]
}

export interface TranscriptInfo {
  id: string
  language: string
  timing_level: 'segment' | 'word_verified'
  /** estimated — время слов по DTW, приблизительное */
  word_timing: 'estimated' | null
  segments: number
  model: string
}

export interface Silence {
  src_start_us: number
  src_end_us: number
  duration_us: number
  min_db: number | null
  kind: 'unknown' | 'no_audio'
}

export interface Waveform {
  from_us: number
  to_us: number
  points: number
  threshold_db: number
  peaks_db: number[]
}

export interface Comment {
  id: string
  revision_id: string
  asset_id: string
  src_us: number
  src_end_us: number | null
  text: string
  author: Actor
  status: 'open' | 'resolved'
  created_at: string
  edited_frame: number | null
  in_revision: boolean
}

export interface Protection {
  id: string
  asset_id: string
  src_start_us: number
  src_end_us: number
  kind: 'hard' | 'soft'
  reason: string
  created_by: Actor | 'system'
}

export interface QaCheck {
  ok?: boolean
  [key: string]: unknown
}

export interface QaReport {
  id: string
  revision_id: string
  job_id: string
  technical: Record<string, QaCheck>
  critical: boolean
  stale: boolean
  final: boolean
  model_notes: { text: string; out_start_frame: number; out_end_frame: number; author: Actor }[]
  human_status: 'pending' | 'approved' | 'changes_requested'
  human_note: string | null
  created_at: string
}

export interface WorkerStatus {
  alive: boolean
  pid: number | null
  heartbeat_age_s: number | null
}

export interface Marker {
  id: string
  number: number
  /** mark — обычная метка, chapter — начало главы для YouTube (label — название) */
  kind: 'mark' | 'chapter'
  asset_id: string
  src_us: number
  label: string
  created_by: Actor
  edited_frame: number | null
  in_revision: boolean
}

export interface StorageCategory {
  key: string
  title: string
  bytes: number
  recoverable: boolean
  note: string
}

export interface StorageUsage {
  categories: StorageCategory[]
  free_bytes: number
  reserve_bytes: number
}

/** Полоса кадров для дорожки видео: сетка cols × rows кадров tile_w × tile_h,
 *  кадр k — около k × step_us от начала файла (точность accuracy_us). */
export interface Thumbs {
  version: number
  step_us: number
  count: number
  cols: number
  rows: number
  tile_w: number
  tile_h: number
  accuracy_us: number
}

/** Главы версии для описания на YouTube и что в них не так по правилам YouTube */
export interface Chapters {
  items: { number: number; title: string; start_us: number; duration_us: number }[]
  text: string
  problems: string[]
  notes: string[]
  ok: boolean
}

/** Участок слоя кадра: что видно на отрезке записи экрана */
export interface VisualSegment {
  id: string
  asset_id: string
  src_start_us: number
  src_end_us: number
  layout: 'screen' | 'camera' | 'pip' | 'side' | 'broll'
  params: {
    zoom?: { x: number; y: number; w: number; h: number }
    /** side: какая часть экрана в рамке */
    region?: { x: number; y: number; w: number; h: number }
    face_cx?: number
    /** Переход на участок: whip — шторка */
    enter?: 'whip'
    /** Лицо крупнее; release — за столько секунд план вернётся к общему */
    punch?: { scale: number; cx: number; cy: number; release?: number; attack?: number }
    /** camera: медленный наезд на кусках между склейками (0.04 — на 4 %) */
    push?: number
    /** camera: смена плана на склейках — по очереди общий и крупнее на step */
    alternate?: { step: number; cx: number; cy: number }
    /** side: экран опущен на эту долю высоты кадра — сверху освобождается место */
    drop?: number
    slot?: 'br' | 'bl' | 'tr' | 'tl'
    size?: number
    /** Окно лица, выставленное мышью на мониторе (side, pip): левый верхний угол и ширина, доли кадра */
    face?: { x: number; y: number; size: number }
    /** Рамка экрана, выставленная мышью (side): левый верхний угол и ширина, доли кадра */
    card?: { x: number; y: number; w: number }
    /** broll: свой кадр — клип из папки broll и с какой секунды */
    clip?: string
    at?: number
    /** side: видео-фон за рамкой экрана из папки backgrounds */
    bg?: string
  }
  goal: string
  created_by: Actor
  review: 'pending' | 'kept' | 'reverted'
  created_at: string
  /** Кадры ролика, где участок виден в этой версии */
  edited_ranges: [number, number][]
  in_revision: boolean
}

export interface VisualLayer {
  default: { layout: VisualSegment['layout'] } & VisualSegment['params']
  segments: VisualSegment[]
}

/** Моушн-графика поверх кадра */
export interface GraphicItem {
  id: string
  asset_id: string
  src_start_us: number
  src_end_us: number
  template: string
  props: Record<string, unknown>
  goal: string
  created_by: Actor
  review: 'pending' | 'kept' | 'reverted'
  created_at: string
  edited_ranges: [number, number][]
  in_revision: boolean
}

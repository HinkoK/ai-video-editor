"""Локальный сервер редактора: HTTP поверх store.py.

Граница доверия проходит здесь. Сервер слушает только 127.0.0.1, но этого
мало: любой открытый в браузере сайт может послать запрос на localhost.
Поэтому:
- заголовок Host должен быть нашим (защита от подмены DNS);
- запрос с чужим Origin отклоняется (защита от запросов со сторонних сайтов);
- каждый запрос несёт ключ доступа. Ключ автора даёт всё, ключ Claude —
  только чтение, предложения правок, мягкую защиту, черновик брифа и
  комментарии. Утверждать он не может, и это проверяет ещё и store.py.
"""

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from secrets import compare_digest
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from longform import asset_data, jobs, qa, store
from longform.db import connect
from longform.edl import EdlError
from longform.ffmpeg import MediaToolError
from longform.ops import Op
from longform.probe import UnsupportedMedia


@dataclass(frozen=True)
class Settings:
    db_path: Path
    user_token: str
    agent_token: str
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    # Отдаём только файлы внутри рабочей папки (результаты задач)
    workspace_root: Path | None = None
    # Собранный интерфейс
    static_dir: Path | None = None


COOKIE = "longform_session"
# Пробный ролик — для проверки стыка, а не для просмотра всего монтажа
MAX_PREVIEW_SECONDS = 120


class Unauthorized(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectIn(Strict):
    name: str = Field(max_length=200)


class AssetIn(Strict):
    path: str = Field(max_length=4096)
    # main — запись экрана с микрофоном; camera — камера, снятая одновременно
    role: Literal["main", "camera"] = "main"


class VisualIn(Strict):
    asset_id: str
    src_start_us: int
    src_end_us: int
    layout: Literal["screen", "camera", "pip", "side", "broll"]
    params: dict = Field(default_factory=dict)
    goal: str = Field(min_length=1, max_length=500)


class VisualReviewIn(Strict):
    action: Literal["keep", "revert"]


class GraphicIn(Strict):
    asset_id: str
    src_start_us: int
    src_end_us: int
    template: str = Field(max_length=40)
    props: dict = Field(default_factory=dict)
    goal: str = Field(min_length=1, max_length=500)


class PageIn(Strict):
    url: str = Field(min_length=8, max_length=2000)
    width: int = Field(default=900, ge=600, le=1600)
    # Фраза, которую выделит свой шаблон-маркер: где её строки на снимке
    find: str | None = Field(default=None, min_length=1, max_length=300)


class PageImportIn(Strict):
    path: str = Field(min_length=1, max_length=4096)
    title: str = Field(default="", max_length=200)


class MarkWaitingIn(Strict):
    on: bool


class ParamsIn(Strict):
    params: dict


class PropsIn(Strict):
    props: dict


class LayoutIn(Strict):
    layout: Literal["screen", "camera", "pip", "side"]
    params: dict = Field(default_factory=dict)


class SyncNudgeIn(Strict):
    delta_us: int = Field(ge=-10_000_000, le=10_000_000)


class KeptAlternativeIn(Strict):
    """Оставленный дубль: диапазон исходника или номера фраз расшифровки."""
    asset_id: str
    src_start_us: int | None = None
    src_end_us: int | None = None
    segment_ids: list[str] = Field(default_factory=list, max_length=500)


class OpIn(Strict):
    """Правка: диапазон исходника (src_start_us, src_end_us) или номера фраз
    (segment_ids) — тогда диапазон считает сервер по речи этих фраз.

    Перестановка (kind move, причина reorder): тот же диапазон —
    какой кусок, и одно место — после или перед (target_side) фразой
    (target_segment_ids: после — конец её речи, перед — начало), моментом
    исходника (target_src_us) или кадром версии под курсором
    (target_edited_frame — таймлайн)."""
    kind: Literal["remove", "restore", "move"]
    asset_id: str
    src_start_us: int | None = None
    src_end_us: int | None = None
    reason: str
    why: str = Field(max_length=2000)
    kept_alternative: KeptAlternativeIn | None = None
    segment_ids: list[str] = Field(default_factory=list, max_length=500)
    # Точная правка по словам: границы в ближайший тихий момент, без запаса
    precise: bool = False
    target_segment_ids: list[str] = Field(default_factory=list, max_length=500)
    target_src_us: int | None = None
    target_edited_frame: int | None = None
    target_side: Literal["before", "after"] | None = None


class ProposalIn(Strict):
    base_revision_id: str
    pass_: str = Field("cleanup", alias="pass")
    rationale: str = Field(max_length=5000)
    policy_version: str = Field("нет", max_length=100)
    ops: list[OpIn] = Field(max_length=2000)


class ApproveIn(Strict):
    exclude_op_ids: list[str] = Field(default_factory=list)
    acknowledge_op_ids: list[str] = Field(default_factory=list)
    overrides: dict[str, str] = Field(default_factory=dict)


class BriefIn(Strict):
    audience: str = ""
    promise: str = ""
    intended_outcome: str = ""
    genre: str = ""
    language: Literal["ru", "uk"] = "ru"
    mode: Literal["conservative_cleanup"] = "conservative_cleanup"
    required_points: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)


class ProtectionIn(Strict):
    asset_id: str
    src_start_us: int
    src_end_us: int
    kind: Literal["hard", "soft"]
    reason: str = Field(max_length=1000)


class PointIn(Strict):
    """Момент файла (asset_id + src_us) или кадр версии (edited_frame [+ revision_id])."""
    asset_id: str | None = None
    src_us: int | None = None
    edited_frame: int | None = None
    revision_id: str | None = None


class FramesIn(Strict):
    points: list[PointIn] = Field(min_length=1, max_length=12)
    width: int = 480
    cols: int = 4


class MarkerIn(Strict):
    label: str = Field("", max_length=200)
    kind: Literal["mark", "chapter"] = "mark"
    revision_id: str | None = None
    edited_frame: int | None = None
    asset_id: str | None = None
    src_us: int | None = None


class SettingsIn(Strict):
    agent_mode: Literal["apply", "propose"] | None = None
    # Громкость экспорта в LUFS; явный null — не выравнивать
    loudness_lufs: float | None = Field(None, ge=-30, le=-8)
    audio_source: Literal["screen", "camera"] | None = None


class ReviewIn(Strict):
    action: Literal["keep", "revert", "apply"]
    reason: str = Field("", max_length=1000)


class NudgeIn(Strict):
    joint: int
    side: Literal["before", "after"]
    frames: int


class TranscribeIn(Strict):
    language: Literal["ru", "uk"] | None = None


class SegmentFixIn(Strict):
    text: str = Field(max_length=5000)


class PreviewIn(Strict):
    out_start_frame: int
    out_end_frame: int


class NoteIn(Strict):
    text: str = Field(max_length=5000)
    out_start_frame: int
    out_end_frame: int


class HumanReviewIn(Strict):
    status: Literal["approved", "changes_requested"]
    note: str = Field("", max_length=5000)


class CommentIn(Strict):
    text: str = Field(max_length=5000)
    revision_id: str | None = None
    edited_frame: int | None = None
    asset_id: str | None = None
    src_us: int | None = None
    src_end_us: int | None = None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def create_app(settings: Settings, on_shutdown: Callable[[], None] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        yield
        # Выполняется при корректной остановке сервера, до того как uvicorn
        # повторно пошлёт себе сигнал завершения
        if on_shutdown:
            on_shutdown()

    app = FastAPI(title="Longform AI Editor", docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in settings.allowed_hosts:
            return _error(400, "bad_host", "запрос не к этому серверу")
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            return _error(403, "bad_origin", "запросы с других сайтов запрещены")
        return await call_next(request)

    @app.exception_handler(Unauthorized)
    async def _unauthorized(_, exc):
        return _error(401, "unauthorized", "нужен ключ доступа")

    @app.exception_handler(store.Forbidden)
    async def _forbidden(_, exc):
        return _error(403, "forbidden", str(exc))

    @app.exception_handler(store.NotFound)
    async def _not_found(_, exc):
        return _error(404, "not_found", "не найдено")

    @app.exception_handler(store.Conflict)
    async def _conflict(_, exc):
        return _error(409, exc.code, exc.message)

    @app.exception_handler(EdlError)
    async def _invalid(_, exc):
        return _error(422, exc.code, exc.message)

    @app.exception_handler(UnsupportedMedia)
    async def _unsupported(_, exc):
        return _error(422, "unsupported_media", str(exc))

    @app.exception_handler(MediaToolError)
    async def _media_tool(_, exc):
        return _error(422, "unreadable_media", f"{exc.tool} не смог прочитать файл")

    def actor(authorization: str = Header(default=""),
              session_cookie: str | None = Cookie(default=None, alias=COOKIE)) -> str:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            given = token.encode()
            if compare_digest(given, settings.user_token.encode()):
                return "user"
            if compare_digest(given, settings.agent_token.encode()):
                return "agent"
        # Браузер: тег <video> не умеет посылать ключ в заголовке, поэтому
        # интерфейс один раз меняет ключ автора на cookie. Cookie только для
        # Автора, HttpOnly (недоступна скриптам) и SameSite=Strict (не уходит
        # с чужих сайтов)
        if session_cookie and compare_digest(session_cookie.encode(), settings.user_token.encode()):
            return "user"
        raise Unauthorized()

    def user(who: str = Depends(actor)) -> str:
        if who != "user":
            raise store.Forbidden("это действие доступно только автору")
        return who

    def db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/session")
    def session(who: str = Depends(actor)):
        return {"actor": who}

    @app.post("/api/session/cookie", status_code=204)
    def session_cookie(response: Response, _: str = Depends(user)):
        response.set_cookie(COOKIE, settings.user_token, httponly=True, samesite="strict", path="/api")

    @app.delete("/api/session/cookie", status_code=204)
    def drop_cookie(response: Response):
        response.delete_cookie(COOKIE, path="/api")

    def send_file(path, media_type: str = "video/mp4") -> FileResponse:
        if settings.workspace_root is None or not path.exists():
            raise store.NotFound("файла нет")
        root = settings.workspace_root.resolve()
        real = path.resolve()
        if not real.is_relative_to(root):
            raise store.Forbidden("файл вне рабочей папки")
        # FileResponse сам отвечает на запросы диапазона (Range): плеер
        # перематывает, не скачивая файл целиком, и сервер не читает его в память
        return FileResponse(real, media_type=media_type)

    @app.get("/api/worker")
    def worker(_: str = Depends(actor), conn=Depends(db)):
        return jobs.worker_status(conn)

    @app.get("/api/projects/{pid}/jobs")
    def list_jobs(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return jobs.list_jobs(conn, pid)

    @app.get("/api/projects/{pid}/jobs/{jid}")
    def get_job(pid: str, jid: str, _: str = Depends(actor), conn=Depends(db)):
        return jobs.get_job(conn, pid, jid)

    @app.post("/api/projects/{pid}/jobs/{jid}/cancel")
    def cancel_job(pid: str, jid: str, who: str = Depends(user), conn=Depends(db)):
        return jobs.request_cancel(conn, pid, jid, who)

    @app.get("/api/projects/{pid}/jobs/{jid}/output")
    def job_output(pid: str, jid: str, _: str = Depends(actor), conn=Depends(db)):
        return send_file(jobs.output_of(conn, pid, jid))

    @app.post("/api/projects/{pid}/assets/{aid}/proxy", status_code=201)
    def make_proxy(pid: str, aid: str, who: str = Depends(actor), conn=Depends(db)):
        store._one(conn, "SELECT id FROM assets WHERE id = ? AND project_id = ?", aid, pid)
        return jobs.enqueue(conn, pid, "proxy", actor=who, asset_id=aid)

    @app.get("/api/projects/{pid}/assets/{aid}/proxy")
    def get_proxy(pid: str, aid: str, _: str = Depends(actor), conn=Depends(db)):
        row = conn.execute("SELECT id FROM jobs WHERE project_id = ? AND asset_id = ? AND type = 'proxy' "
                           "AND state = 'completed' ORDER BY finished_at DESC LIMIT 1", (pid, aid)).fetchone()
        if row is None:
            raise store.NotFound("лёгкой копии ещё нет")
        return send_file(jobs.output_of(conn, pid, row["id"]))

    def latest_proxy(conn, pid: str, aid: str) -> Path:
        row = conn.execute("SELECT id FROM jobs WHERE project_id = ? AND asset_id = ? AND type = 'proxy' "
                           "AND state = 'completed' ORDER BY finished_at DESC LIMIT 1", (pid, aid)).fetchone()
        if row is None:
            raise store.Conflict("no_proxy", "лёгкой копии ещё нет: сделайте её в разделе «Проект»")
        path = jobs.output_of(conn, pid, row["id"])
        if not path.exists():
            raise store.Conflict("no_proxy", "лёгкая копия стёрта: соберите её заново в разделе «Проект»")
        return path

    @app.get("/api/projects/{pid}/assets/{aid}/thumbs")
    def get_thumbs(pid: str, aid: str, _: str = Depends(actor), conn=Depends(db)):
        """Полоса кадров для дорожки видео: описание сетки (шаг, размер кадра)."""
        from longform import thumbs

        store._one(conn, "SELECT id FROM assets WHERE id = ? AND project_id = ?", aid, pid)
        return thumbs.ensure(latest_proxy(conn, pid, aid))

    @app.get("/api/projects/{pid}/assets/{aid}/thumbs.jpg")
    def get_thumbs_image(pid: str, aid: str, _: str = Depends(actor), conn=Depends(db)):
        from longform import thumbs

        store._one(conn, "SELECT id FROM assets WHERE id = ? AND project_id = ?", aid, pid)
        proxy = latest_proxy(conn, pid, aid)
        thumbs.ensure(proxy)
        return send_file(thumbs.paths(proxy)[0], media_type="image/jpeg")

    def asset_row(conn, pid: str, aid: str):
        return store._one(conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", aid, pid)

    @app.post("/api/projects/{pid}/assets/{aid}/analyze", status_code=201)
    def analyze(pid: str, aid: str, who: str = Depends(actor), conn=Depends(db)):
        asset_row(conn, pid, aid)
        return jobs.enqueue(conn, pid, "analyze", actor=who, asset_id=aid)

    @app.post("/api/projects/{pid}/assets/{aid}/transcribe", status_code=201)
    def transcribe(pid: str, aid: str, body: TranscribeIn | None = None, who: str = Depends(actor),
                   conn=Depends(db)):
        from longform import transcribe as tr

        asset_row(conn, pid, aid)
        # Язык выбирает автор (или бриф). Повторная расшифровка снимает мягкую
        # защиту прошлой, поэтому агент не может перезапустить её на другом языке
        language = ((body.language if body and who == "user" else None)
                    or store._brief_language(conn, pid))
        params = {"language": language, "model": tr.MODEL.name, "dtw": tr.dtw_preset(tr.MODEL),
                  "chunk_ms": tr.CHUNK_MS, "overlap_ms": tr.OVERLAP_MS, "engine": tr.ENGINE_VERSION}
        return jobs.enqueue(conn, pid, "transcribe", actor=who, asset_id=aid, params=params)

    @app.get("/api/projects/{pid}/assets/{aid}/analysis")
    def analysis(pid: str, aid: str, _: str = Depends(actor), conn=Depends(db)):
        asset_row(conn, pid, aid)
        return asset_data.analysis_info(conn, aid)

    @app.get("/api/projects/{pid}/assets/{aid}/silences")
    def silences(pid: str, aid: str, from_us: int = 0, to_us: int | None = None, _: str = Depends(actor),
                 conn=Depends(db)):
        asset_row(conn, pid, aid)
        return asset_data.silences(conn, aid, from_us, to_us)

    @app.get("/api/projects/{pid}/assets/{aid}/waveform")
    def waveform(pid: str, aid: str, from_us: int = 0, to_us: int | None = None, points: int = 1000,
                 _: str = Depends(actor), conn=Depends(db)):
        from longform.analysis import peaks

        row = asset_row(conn, pid, aid)
        loud = asset_data.loudness(conn, aid)
        if loud is None:
            raise store.NotFound("громкость ещё не измерена")
        end = to_us if to_us is not None else row["frame_count"] * 1_000_000 * row["fps_den"] // row["fps_num"]
        points = max(1, min(points, 5000))
        return {"from_us": from_us, "to_us": end, "points": points, "threshold_db": loud.threshold_db,
                "peaks_db": peaks(loud.db, loud.idx(from_us), loud.idx(end), points)}

    @app.get("/api/projects/{pid}/assets/{aid}/transcript")
    def transcript(pid: str, aid: str, from_us: int = 0, to_us: int | None = None, limit: int = 500,
                   words: bool = False, _: str = Depends(actor), conn=Depends(db)):
        asset_row(conn, pid, aid)
        segs = asset_data.segments(conn, aid, from_us, to_us, min(limit, 2000))
        if words:
            by_seg = asset_data.words(conn, [s["id"] for s in segs])
            for s in segs:
                s["words"] = by_seg.get(s["id"], [])
        return {"transcript": asset_data.transcript_info(conn, aid), "segments": segs}

    @app.get("/api/projects/{pid}/transcript/search")
    def search(pid: str, q: str, limit: int = 50, _: str = Depends(actor), conn=Depends(db)):
        return asset_data.search(conn, pid, q, min(limit, 200))

    @app.patch("/api/projects/{pid}/segments/{sid}")
    def fix_segment(pid: str, sid: str, body: SegmentFixIn, who: str = Depends(user), conn=Depends(db)):
        return asset_data.correct_segment(conn, pid, sid, body.text, who)

    @app.post("/api/projects/{pid}/revisions/{rid}/preview", status_code=201)
    def make_preview(pid: str, rid: str, body: PreviewIn, who: str = Depends(actor), conn=Depends(db)):
        edl = store.compile_revision(conn, pid, rid)
        # «3 с вокруг стыка» у начала или конца ролика — обрезаем по ролику
        start, end = max(0, body.out_start_frame), min(edl.total_frames, body.out_end_frame)
        edl.slice(start, end)  # проверит, что диапазон не пустой
        if (end - start) / edl.profile.fps > MAX_PREVIEW_SECONDS:
            raise EdlError("preview_too_long", f"пробный ролик не длиннее {MAX_PREVIEW_SECONDS} с")
        return jobs.enqueue(conn, pid, "preview", actor=who, revision_id=rid,
                            params={"out_start_frame": start, "out_end_frame": end})

    @app.post("/api/projects/{pid}/revisions/{rid}/export", status_code=201)
    def make_export(pid: str, rid: str, who: str = Depends(user), conn=Depends(db)):
        return jobs.enqueue(conn, pid, "export", actor=who, revision_id=rid)

    @app.get("/api/projects/{pid}/qa")
    def qa_reports(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return qa.list_reports(conn, pid)

    @app.post("/api/projects/{pid}/qa/{qid}/notes")
    def qa_note(pid: str, qid: str, body: NoteIn, who: str = Depends(actor), conn=Depends(db)):
        return qa.add_model_note(conn, pid, qid, text=body.text, out_start_frame=body.out_start_frame,
                                 out_end_frame=body.out_end_frame, actor=who)

    @app.post("/api/projects/{pid}/qa/{qid}/human")
    def qa_human(pid: str, qid: str, body: HumanReviewIn, who: str = Depends(user), conn=Depends(db)):
        return qa.set_human_review(conn, pid, qid, status=body.status, note=body.note, actor=who)

    @app.get("/api/projects")
    def projects(_: str = Depends(actor), conn=Depends(db)):
        return store.list_projects(conn)

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn, who: str = Depends(user), conn=Depends(db)):
        return store.create_project(conn, body.name, who)

    @app.get("/api/projects/{pid}")
    def project(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.get_project(conn, pid)

    @app.post("/api/projects/{pid}/assets", status_code=201)
    def register_asset(pid: str, body: AssetIn, who: str = Depends(user), conn=Depends(db)):
        asset = store.register_asset(conn, pid, body.path, who, role=body.role)
        if body.role == "camera":
            # Камеру сразу сводим по звуку и делаем лёгкую копию для просмотра
            jobs.enqueue(conn, pid, "sync", actor=who, asset_id=asset["id"])
            jobs.enqueue(conn, pid, "proxy", actor=who, asset_id=asset["id"])
        return asset

    @app.get("/api/projects/{pid}/visual")
    def visual_list(pid: str, revision_id: str | None = None, _: str = Depends(actor), conn=Depends(db)):
        """Участки слоя кадра; с revision_id — где каждый виден в этой версии."""
        from longform import visual

        rid = revision_id or store._one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        edl = store.compile_revision(conn, pid, rid) if rid else None
        return {"default": visual.project_layout(conn, pid), "segments": visual.list_segments(conn, pid, edl)}

    @app.post("/api/projects/{pid}/visual", status_code=201)
    def visual_add(pid: str, body: VisualIn, who: str = Depends(actor), conn=Depends(db)):
        from longform import visual

        clips_dir = workspace().broll if settings.workspace_root is not None else None
        return visual.add_segment(conn, pid, actor=who, asset_id=body.asset_id, src_start_us=body.src_start_us,
                                  src_end_us=body.src_end_us, layout=body.layout, params=body.params, goal=body.goal,
                                  clips_dir=clips_dir)

    @app.get("/api/projects/{pid}/broll")
    def broll_list(pid: str, _: str = Depends(actor), conn=Depends(db)):
        """Свои кадры для вставок: имя клипа, длительность, описание."""
        from longform import visual

        store._one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        return visual.list_broll(workspace().broll if settings.workspace_root is not None else None)

    @app.post("/api/projects/{pid}/visual/{vid}/review")
    def visual_review(pid: str, vid: str, body: VisualReviewIn, who: str = Depends(user), conn=Depends(db)):
        from longform import visual

        return visual.review_segment(conn, pid, vid, body.action, who)

    @app.get("/api/projects/{pid}/graphics")
    def graphics_list(pid: str, revision_id: str | None = None, _: str = Depends(actor), conn=Depends(db)):
        """Графика поверх кадра; с revision_id — где она видна в этой версии."""
        from longform import graphics

        rid = revision_id or store._one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        edl = store.compile_revision(conn, pid, rid) if rid else None
        mark = store._one(conn, "SELECT mark_waiting FROM projects WHERE id = ?", pid)["mark_waiting"]
        return {"mark_waiting": bool(mark), "graphics": graphics.list_graphics(conn, pid, edl)}

    @app.post("/api/projects/{pid}/graphics", status_code=201)
    def graphics_add(pid: str, body: GraphicIn, who: str = Depends(actor), conn=Depends(db)):
        from longform import graphics

        return graphics.add_graphic(conn, pid, actor=who, asset_id=body.asset_id, src_start_us=body.src_start_us,
                                    src_end_us=body.src_end_us, template=body.template, props=body.props,
                                    goal=body.goal)

    @app.post("/api/projects/{pid}/graphics/{gid}/review")
    def graphics_review(pid: str, gid: str, body: VisualReviewIn, who: str = Depends(user), conn=Depends(db)):
        from longform import graphics

        return graphics.review_graphic(conn, pid, gid, body.action, who)

    @app.post("/api/projects/{pid}/assets/{aid}/noises", status_code=201)
    def noises_find(pid: str, aid: str, who: str = Depends(actor), conn=Depends(db)):
        """Найти кашель и «кхм» в том, что осталось от записи в текущей версии и
        ещё не проверено (задача исполнителю). Нечего проверять — job: null."""
        from longform import asset_data, noises

        store._one(conn, "SELECT id FROM assets WHERE id = ? AND project_id = ?", aid, pid)
        t = asset_data.current_transcript(conn, aid)
        if t is None:
            raise EdlError("no_transcript", "сначала расшифровка: кашель ищется по ней")
        rid = store._one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        if not rid:
            raise EdlError("no_revision", "сначала версия: кашель ищется в том, что в ней осталось")
        ranges = noises.to_scan(conn, aid, store.compile_revision(conn, pid, rid), t["id"])
        if not ranges:
            return {"job": None, "ranges": []}
        job = jobs.enqueue(conn, pid, "noises", actor=who, asset_id=aid,
                           params={"transcript": t["id"], "version": noises.VERSION, "ranges": [list(r) for r in ranges]})
        return {"job": job, "ranges": ranges}

    @app.get("/api/projects/{pid}/assets/{aid}/noises")
    def noises_list(pid: str, aid: str, _: str = Depends(actor), conn=Depends(db)):
        """Кашель и «кхм»: сам звук, что вырезать, что услышал Whisper, есть ли он
        в текущей версии; unscanned_us — сколько оставленного в версии ещё не проверено."""
        from longform import asset_data, noises
        from longform.timebase import frame_at

        store._one(conn, "SELECT id FROM assets WHERE id = ? AND project_id = ?", aid, pid)
        rid = store._one(conn, "SELECT current_revision_id FROM projects WHERE id = ?", pid)[0]
        edl = store.compile_revision(conn, pid, rid) if rid else None
        out = []
        for n in noises.list_noises(conn, aid):
            if edl is not None and aid in edl.sources:
                fps = edl.sources[aid].fps
                n["in_revision"] = edl.source_to_edited(aid, frame_at((n["start_us"] + n["end_us"]) // 2, fps)) is not None
            out.append(n)
        t = asset_data.current_transcript(conn, aid)
        left = noises.to_scan(conn, aid, edl, t["id"]) if edl is not None and t is not None else []
        return {"scan": noises.scanned(conn, aid), "noises": out, "unscanned_us": sum(b - a for a, b in left)}

    @app.get("/api/projects/{pid}/pages")
    def pages_list(pid: str, _: str = Depends(actor), conn=Depends(db)):
        """Снимки страниц для вставок."""
        from longform import pages

        return pages.list_pages(conn, pid)

    @app.post("/api/projects/{pid}/pages", status_code=201)
    def pages_capture(pid: str, body: PageIn, who: str = Depends(actor), conn=Depends(db)):
        """Снять страницу по ссылке (Chrome без окна; интернет только для снимка)."""
        from longform import pages

        store._one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        return pages.capture(conn, workspace(), pid, body.url, who, body.width, find=body.find)

    @app.post("/api/projects/{pid}/pages/import", status_code=201)
    def pages_import(pid: str, body: PageImportIn, who: str = Depends(user), conn=Depends(db)):
        """Свой снимок автора — например страница, закрытая входом."""
        from longform import pages

        store._one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        return pages.import_file(conn, workspace(), pid, body.path, body.title, who)

    @app.get("/api/projects/{pid}/pages/{page_id}/preview")
    def pages_preview(pid: str, page_id: str, width: int = 800, _: str = Depends(actor), conn=Depends(db)):
        """Верх снимка уменьшенным — посмотреть, что снято."""
        from longform import pages

        pages.get_page(conn, pid, page_id)
        png = pages.preview(workspace(), pid, page_id, min(max(width, 200), 1600))
        return Response(content=png, media_type="image/png")

    @app.post("/api/projects/{pid}/graphics/mark-waiting")
    def graphics_mark_waiting(pid: str, body: MarkWaitingIn, who: str = Depends(user), conn=Depends(db)):
        """Ставить ли пометку «ожидание сокращено» у сокращённых ожиданий."""
        store._only_user(who, "менять пометку ожидания")
        with store.write(conn):
            store._one(conn, "SELECT id FROM projects WHERE id = ?", pid)
            conn.execute("UPDATE projects SET mark_waiting = ? WHERE id = ?", (int(body.on), pid))
        return {"mark_waiting": body.on}

    @app.post("/api/projects/{pid}/layout")
    def layout_default(pid: str, body: LayoutIn, who: str = Depends(user), conn=Depends(db)):
        from longform import visual

        return visual.set_project_layout(conn, pid, body.layout, body.params, who)

    @app.post("/api/projects/{pid}/assets/{aid}/sync", status_code=201)
    def resync(pid: str, aid: str, who: str = Depends(user), conn=Depends(db)):
        """Пересчитать синхронизацию камеры (в том числе после ручной подстройки)."""
        return jobs.enqueue(conn, pid, "sync", actor=who, asset_id=aid, params={"requested_at": store._now()})

    @app.post("/api/projects/{pid}/assets/{aid}/sync/nudge")
    def sync_nudge(pid: str, aid: str, body: SyncNudgeIn, who: str = Depends(user), conn=Depends(db)):
        return store.nudge_sync(conn, pid, aid, body.delta_us, who)

    @app.get("/api/projects/{pid}/revisions")
    def revisions(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.list_revisions(conn, pid)

    @app.post("/api/projects/{pid}/revisions/initial", status_code=201)
    def initial_revision(pid: str, who: str = Depends(user), conn=Depends(db)):
        return store.create_initial_revision(conn, pid, who)

    @app.get("/api/projects/{pid}/revisions/{rid}")
    def revision(pid: str, rid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.get_revision(conn, pid, rid)

    @app.post("/api/projects/{pid}/revisions/{rid}/revert", status_code=201)
    def revert(pid: str, rid: str, who: str = Depends(user), conn=Depends(db)):
        return store.revert(conn, pid, rid, who)

    @app.get("/api/projects/{pid}/proposals")
    def proposals(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.list_proposals(conn, pid)

    def resolve_range(conn, pid, asset_id, start, end, segment_ids, what):
        if start is not None and end is not None:
            return start, end
        if segment_ids:
            return asset_data.range_for_segments(conn, pid, asset_id, segment_ids)
        raise EdlError("no_range", f"{what}: нужен диапазон исходника или номера фраз")

    def resolve_target(conn, pid: str, base_rid: str, o: OpIn, what: str) -> tuple[str, int, str] | None:
        """Место перестановки -> (исходник, время исходника, сторона)."""
        given = [bool(o.target_segment_ids), o.target_src_us is not None, o.target_edited_frame is not None]
        if o.kind != "move":
            if any(given) or o.target_side is not None:
                raise EdlError("bad_target", f"{what}: место указывают только у перестановки (kind move)")
            return None
        if sum(given) != 1:
            raise EdlError("no_target", f"{what}: у перестановки одно место — target_segment_ids, "
                                        "target_src_us или target_edited_frame")
        side = o.target_side
        if side is None:
            raise EdlError("no_side", f"{what}: укажите target_side — after (после) или before (перед)")
        if o.target_segment_ids:
            a, b = asset_data.range_for_segments(conn, pid, o.asset_id, o.target_segment_ids)
            return o.asset_id, (b if side == "after" else a), side
        if o.target_src_us is not None:
            return o.asset_id, o.target_src_us, side
        # Кадр версии под курсором: «перед» — перед этим кадром, «после» — после него
        from longform.timebase import frame_to_us

        edl = store.compile_revision(conn, pid, base_rid)
        f = o.target_edited_frame
        if f == edl.total_frames:  # курсор за последним кадром — в конец ролика
            last = edl.clips[-1]
            return last.asset_id, frame_to_us(last.src_end_frame, edl.sources[last.asset_id].fps), "after"
        try:
            _, asset, src_frame = edl.edited_to_source(f)
        except IndexError:
            raise EdlError("out_of_bounds", f"{what}: кадра {f} нет в версии") from None
        fps = edl.sources[asset].fps
        return asset, frame_to_us(src_frame + (1 if side == "after" else 0), fps), side

    @app.post("/api/projects/{pid}/proposals", status_code=201)
    def create_proposal(pid: str, body: ProposalIn, who: str = Depends(actor), conn=Depends(db)):
        ops = []
        for i, o in enumerate(body.ops):
            start, end = resolve_range(conn, pid, o.asset_id, o.src_start_us, o.src_end_us, o.segment_ids,
                                       f"правка {i}")
            alt = None
            if o.kept_alternative:
                k = o.kept_alternative
                alt = (k.asset_id, *resolve_range(conn, pid, k.asset_id, k.src_start_us, k.src_end_us,
                                                  k.segment_ids, f"правка {i}, оставленный дубль"))
            target = resolve_target(conn, pid, body.base_revision_id, o, f"правка {i}")
            ops.append(Op(o.kind, o.asset_id, start, end, o.reason, o.why, alt, tuple(o.segment_ids), o.precise,
                          target=target))
        return store.create_proposal(conn, pid, base_revision_id=body.base_revision_id, pass_=body.pass_,
                                     rationale=body.rationale, policy_version=body.policy_version,
                                     ops=ops, actor=who)

    @app.get("/api/projects/{pid}/proposals/{prop_id}")
    def proposal(pid: str, prop_id: str, _: str = Depends(actor), conn=Depends(db)):
        return store.get_proposal(conn, pid, prop_id)

    @app.post("/api/projects/{pid}/proposals/{prop_id}/approve")
    def approve(pid: str, prop_id: str, body: ApproveIn, who: str = Depends(user), conn=Depends(db)):
        return store.approve(conn, pid, prop_id, actor=who, exclude=body.exclude_op_ids,
                             acknowledge=body.acknowledge_op_ids, overrides=body.overrides)

    @app.post("/api/projects/{pid}/frames")
    def frames(pid: str, body: FramesIn, _: str = Depends(actor), conn=Depends(db)):
        """Лист кадров оригинала в заданные моменты — «глаза» Claude."""
        from longform.frames import contact_sheet
        from longform.probe import probe_cached
        from longform.timebase import frame_to_us
        from longform.worker import check_source

        points = []
        for p in body.points:
            if p.edited_frame is not None:
                rid = p.revision_id or store._one(conn, "SELECT current_revision_id FROM projects WHERE id = ?",
                                                  pid)[0]
                edl = store.compile_revision(conn, pid, rid)
                try:
                    _, asset_id, src_frame = edl.edited_to_source(p.edited_frame)
                except IndexError:
                    raise EdlError("out_of_bounds", f"кадра {p.edited_frame} нет в версии") from None
                fps = edl.sources[asset_id].fps
                src_us = (frame_to_us(src_frame, fps) + frame_to_us(src_frame + 1, fps)) // 2
            elif p.asset_id is not None and p.src_us is not None:
                asset_id, src_us = p.asset_id, p.src_us
            else:
                raise EdlError("bad_point", "точка: asset_id и src_us или edited_frame")
            row = store._one(conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", asset_id, pid)
            points.append((probe_cached(check_source(row)), src_us))
        png = contact_sheet(points, body.width, body.cols)
        return Response(content=png, media_type="image/png")

    def workspace():
        from longform.workspace import Workspace

        if settings.workspace_root is None:
            raise store.NotFound("рабочая папка не задана")
        return Workspace(settings.workspace_root)

    @app.get("/api/projects/{pid}/storage")
    def storage_usage(pid: str, _: str = Depends(actor), conn=Depends(db)):
        from longform import storage

        return storage.usage(conn, workspace(), pid)

    @app.delete("/api/projects/{pid}/storage/{key}")
    def storage_clean(pid: str, key: str, who: str = Depends(user), conn=Depends(db)):
        from longform import storage

        return storage.clean(conn, workspace(), pid, key, who)

    @app.get("/api/projects/{pid}/markers")
    def markers(pid: str, revision_id: str | None = None, _: str = Depends(actor), conn=Depends(db)):
        return store.list_markers(conn, pid, revision_id)

    @app.post("/api/projects/{pid}/markers", status_code=201)
    def add_marker(pid: str, body: MarkerIn, who: str = Depends(actor), conn=Depends(db)):
        return store.add_marker(conn, pid, actor=who, label=body.label, revision_id=body.revision_id,
                                edited_frame=body.edited_frame, asset_id=body.asset_id, src_us=body.src_us,
                                kind=body.kind)

    @app.delete("/api/projects/{pid}/markers/{mid}", status_code=204)
    def remove_marker(pid: str, mid: str, who: str = Depends(user), conn=Depends(db)):
        store.remove_marker(conn, pid, mid, who)

    @app.post("/api/projects/{pid}/settings")
    def settings_(pid: str, body: SettingsIn, who: str = Depends(user), conn=Depends(db)):
        result = None
        if body.agent_mode is not None:
            result = store.set_agent_mode(conn, pid, body.agent_mode, who)
        if "loudness_lufs" in body.model_fields_set:
            result = store.set_loudness(conn, pid, body.loudness_lufs, who)
        if body.audio_source is not None:
            result = store.set_audio_source(conn, pid, body.audio_source, who)
        if result is None:
            raise EdlError("no_settings", "нечего менять")
        return result

    @app.post("/api/projects/{pid}/proposals/{prop_id}/ops/{op_id}/review")
    def review(pid: str, prop_id: str, op_id: str, body: ReviewIn, who: str = Depends(user), conn=Depends(db)):
        return store.review_op(conn, pid, prop_id, op_id, action=body.action, actor=who, reason=body.reason)

    @app.get("/api/projects/{pid}/revisions/{rid}/joints")
    def revision_joints(pid: str, rid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.joints(store.compile_revision(conn, pid, rid))

    @app.get("/api/projects/{pid}/revisions/{rid}/captions.srt")
    def revision_captions(pid: str, rid: str, _: str = Depends(actor), conn=Depends(db)):
        """Субтитры версии файлом .srt — из исправленной расшифровки."""
        from longform import captions

        number = store._one(conn, "SELECT number FROM revisions WHERE id = ? AND project_id = ?", rid, pid)[0]
        body = captions.to_srt(captions.revision_cues(conn, pid, rid))
        return Response(body.encode(), media_type="application/x-subrip; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="version-{number}.srt"'})

    @app.get("/api/projects/{pid}/revisions/{rid}/chapters")
    def revision_chapters(pid: str, rid: str, _: str = Depends(actor), conn=Depends(db)):
        """Главы для описания на YouTube и что в них не так по правилам YouTube."""
        from longform import captions

        return captions.revision_chapters(conn, pid, rid)

    @app.post("/api/projects/{pid}/revisions/{rid}/nudge")
    def nudge(pid: str, rid: str, body: NudgeIn, who: str = Depends(user), conn=Depends(db)):
        return store.nudge_joint(conn, pid, revision_id=rid, joint=body.joint, side=body.side,
                                 frames=body.frames, actor=who)

    @app.post("/api/projects/{pid}/proposals/{prop_id}/reject")
    def reject(pid: str, prop_id: str, who: str = Depends(user), conn=Depends(db)):
        return store.reject(conn, pid, prop_id, who)

    @app.get("/api/projects/{pid}/brief")
    def brief(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.get_brief(conn, pid)

    @app.post("/api/projects/{pid}/brief", status_code=201)
    def save_brief(pid: str, body: BriefIn, who: str = Depends(actor), conn=Depends(db)):
        return store.save_brief(conn, pid, body.model_dump(), who)

    @app.post("/api/projects/{pid}/brief/{version}/confirm")
    def confirm_brief(pid: str, version: int, who: str = Depends(user), conn=Depends(db)):
        return store.confirm_brief(conn, pid, version, who)

    @app.get("/api/projects/{pid}/protected")
    def protections(pid: str, _: str = Depends(actor), conn=Depends(db)):
        return store.list_protections(conn, pid)

    @app.post("/api/projects/{pid}/protected", status_code=201)
    def add_protection(pid: str, body: ProtectionIn, who: str = Depends(actor), conn=Depends(db)):
        return store.add_protection(conn, pid, asset_id=body.asset_id, src_start_us=body.src_start_us,
                                    src_end_us=body.src_end_us, kind=body.kind, reason=body.reason, actor=who)

    @app.delete("/api/projects/{pid}/protected/{prt_id}", status_code=204)
    def remove_protection(pid: str, prt_id: str, who: str = Depends(user), conn=Depends(db)):
        store.remove_protection(conn, pid, prt_id, who)

    @app.get("/api/projects/{pid}/comments")
    def comments(pid: str, revision_id: str, _: str = Depends(actor), conn=Depends(db)):
        return store.list_comments(conn, pid, revision_id)

    @app.post("/api/projects/{pid}/comments", status_code=201)
    def add_comment(pid: str, body: CommentIn, who: str = Depends(actor), conn=Depends(db)):
        return store.add_comment(conn, pid, text=body.text, actor=who, revision_id=body.revision_id,
                                 edited_frame=body.edited_frame, asset_id=body.asset_id, src_us=body.src_us,
                                 src_end_us=body.src_end_us)

    @app.post("/api/projects/{pid}/comments/{cid}/resolve")
    def resolve_comment(pid: str, cid: str, who: str = Depends(user), conn=Depends(db)):
        return store.resolve_comment(conn, pid, cid, who)

    # ---------- монитор программы (как в Premiere): правки автора мышью ----------

    @app.patch("/api/projects/{pid}/visual/{vid}")
    def visual_patch(pid: str, vid: str, body: ParamsIn, who: str = Depends(user), conn=Depends(db)):
        """Автор сдвинул окно лица или рамку экрана на мониторе."""
        from longform import visual

        return visual.update_params(conn, pid, vid, body.params, who)

    @app.patch("/api/projects/{pid}/graphics/{gid}")
    def graphic_patch(pid: str, gid: str, body: PropsIn, who: str = Depends(user), conn=Depends(db)):
        """Автор сдвинул графику на мониторе или поправил её текст."""
        from longform import graphics

        return graphics.update_props(conn, pid, gid, body.props, who)

    @app.get("/api/projects/{pid}/broll/{name}/video")
    def broll_video(pid: str, name: str, _: str = Depends(actor), conn=Depends(db)):
        """Свой кадр — для монитора программы."""
        from longform.visual import CLIP_NAME

        store._one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        if not CLIP_NAME.match(name):
            raise store.NotFound("нет такого клипа")
        return send_file(workspace().broll / name)

    @app.get("/api/projects/{pid}/backgrounds/{name}/video")
    def background_video(pid: str, name: str, _: str = Depends(actor), conn=Depends(db)):
        """Видео-фон за рамкой экрана — для монитора программы."""
        from longform.visual import CLIP_NAME

        store._one(conn, "SELECT id FROM projects WHERE id = ?", pid)
        if not CLIP_NAME.match(name):
            raise store.NotFound("нет такого фона")
        return send_file(workspace().backgrounds / name)

    # Шаблоны графики для монитора программы: окно graphics/preview.html собирает
    # ту же страницу, что render.mjs для кадров ролика. Отдаются только шаблоны,
    # тема, это окно и шрифты — остальное в graphics/ (node_modules) закрыто
    graphics_file = re.compile(r"(templates[\w-]*\.mjs|theme\.css|preview\.html|"
                               r"node_modules/@fontsource/[\w-]+/(files/)?[\w.-]+\.(css|woff2?))")
    graphics_types = {".mjs": "text/javascript", ".css": "text/css", ".html": "text/html",
                      ".woff2": "font/woff2", ".woff": "font/woff", ".json": "application/json", ".js": "text/javascript"}

    @app.get("/graphics/{path:path}")
    def graphics_static(path: str):
        from longform.graphics import GRAPHICS_DIR

        root = GRAPHICS_DIR.resolve()
        real = (GRAPHICS_DIR / path).resolve()
        if not graphics_file.fullmatch(path) or not real.is_relative_to(root) or not real.is_file():
            raise store.NotFound("нет такого файла графики")
        return FileResponse(real, media_type=graphics_types[real.suffix])

    # Собранный интерфейс (frontend/dist), если он есть. Данных в нём нет,
    # всё приходит через /api с проверкой ключа
    if settings.static_dir is not None and settings.static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")
    return app

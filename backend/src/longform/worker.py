"""Фоновый исполнитель: uv run python -m longform.worker

Один процесс, одна задача за раз. Обычно его запускает и останавливает
сервер (server.py), но можно запустить и отдельно.

Жизненный цикл:
- второй исполнитель не запустится: замок .run/worker.lock;
- при запуске всё, что «шло», возвращается в очередь (jobs.recover);
- SIGTERM или Ctrl+C: текущий ffmpeg убивается вместе с группой, задача
  помечается «прервана» и продолжится при следующем запуске;
- если умер сервер, который запустил исполнителя, исполнитель тоже
  останавливается: сирот не остаётся;
- результат пишется в «.part» и переименовывается только при успехе.
"""

import fcntl
import logging
import os
import signal
import sqlite3
import sys
import time
import traceback
from dataclasses import replace
from collections.abc import Callable
from pathlib import Path

from longform import jobs, qa, store
from longform.db import connect, migrate
from longform.edl import Clip, CompiledEdl, EdlError, OutputProfile, compile_edl
from longform.ffmpeg import Cancelled, DiskSpaceError, MediaToolError
from longform.probe import ProbeResult, UnsupportedMedia, probe
from longform.render.select import FINAL, PREVIEW, PROXY, RenderCheckError, RenderResult, render_select
from longform.timebase import frame_to_us
from longform.verify import verify_output
from longform.workspace import Workspace

log = logging.getLogger("longform.worker")


class SourceChanged(RuntimeError):
    pass


class Context:
    """Всё, что нужно выполняющейся задаче: база, папки, отмена, ход работы."""

    def __init__(self, conn: sqlite3.Connection, ws: Workspace, job: sqlite3.Row, stop: dict):
        self.conn, self.ws, self.job, self._stop = conn, ws, job, stop
        self._last_check = 0.0
        self._last_beat = time.monotonic()
        self._last_progress = 0.0
        self._cancel = False

    def cancel(self) -> str | None:
        """Вызывается каждые полсекунды, пока идёт работа. Заодно: сигнал
        «исполнитель жив» (раз в 3 с, иначе долгий экспорт выглядел бы
        зависанием) и проверка, жив ли сервер, запустивший исполнителя."""
        if self._stop.get("reason"):
            return "shutdown"
        now = time.monotonic()
        if now - self._last_check > 0.5:
            self._last_check = now
            self._cancel = jobs.cancel_requested(self.conn, self.job["id"])
            parent = self._stop.get("parent_pid")
            if parent is not None and os.getppid() != parent:
                log.info("сервер завершился посреди задачи — останавливаюсь")
                self._stop["reason"] = "shutdown"
                return "shutdown"
        if now - self._last_beat > 3:
            self._last_beat = now
            jobs.heartbeat(self.conn, os.getpid(), self._stop.get("started", store._now()))
        return "user" if self._cancel else None

    def progress(self, value: float, message: str | None = None) -> None:
        now = time.monotonic()
        if message is not None or now - self._last_progress > 1:
            self._last_progress = now
            jobs.progress(self.conn, self.job["id"], round(value, 4), message)

    @property
    def pid(self) -> str:
        return self.job["project_id"]


def check_source(row: sqlite3.Row) -> Path:
    """Исходник на месте и не менялся с подключения."""
    path = Path(row["path"])
    if not path.is_file():
        raise SourceChanged(f"исходник не найден: {path}")
    st = path.stat()
    if (st.st_size, st.st_mtime_ns) != (row["size_bytes"], row["mtime_ns"]):
        if store.quick_fingerprint(path) != row["fingerprint"]:
            raise SourceChanged(f"исходник изменился после подключения: {path.name}")
    return path


def media_for(ctx: Context, asset_ids) -> dict[str, ProbeResult]:
    media = {}
    for a in asset_ids:
        row = store._one(ctx.conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", a, ctx.pid)
        media[a] = probe(check_source(row))
        if media[a].video.frame_count != row["frame_count"]:
            raise SourceChanged(f"в исходнике {media[a].path.name} стало другое число кадров")
    return media


def _part(dst: Path) -> Path:
    return dst.with_name(dst.stem + ".part" + dst.suffix)


def _render(ctx: Context, edl: CompiledEdl, dst: Path, settings, span: tuple[float, float] = (0, 1),
            finalize: bool = True, plan=None, overlays=None) -> RenderResult:
    """Сборка в «.part»; при finalize — переименование при успехе. В
    результате path — где лежит ролик сейчас (.part, если не finalize).
    plan — слой кадра (visual.build_plan), камеры читаются вместе с экраном."""
    part = _part(dst)
    part.unlink(missing_ok=True)
    ids = {c.asset_id for c in edl.clips}
    if plan is not None:
        ids |= {cam.asset_id for cam in plan.cameras.values()}
    # Лёгкая копия — всегда со своим звуком; ролик — со звуком, который выбрал автор
    sound = {} if settings is PROXY else {a: v for a, v in store.audio_from(ctx.conn, ctx.pid).items() if a in ids}
    ids |= {cam for cam, _, _ in sound.values()}
    media = media_for(ctx, ids)
    a, b = span
    drawn = _draw_overlays(ctx, edl, overlays) if overlays else None
    result = render_select(edl, media, part, ctx.ws.project(ctx.pid, "cache"), settings,
                           cancel=ctx.cancel, on_progress=lambda f: ctx.progress(a + (b - a) * f), visual=plan,
                           overlays=drawn, audio_from=sound or None)
    if finalize:
        part.rename(dst)
        return replace(result, path=dst)
    return result


def run_proxy(ctx: Context) -> Path:
    asset = ctx.job["asset_id"]
    sources, _, _ = store.load_sources(ctx.conn, ctx.pid)
    src = sources[asset]
    # Лёгкая копия — в формате самого файла: у камеры частота кадров своя
    profile = OutputProfile(src.fps, src.sample_rate)
    # Лёгкая копия — это «монтаж» из одного куска на весь файл. Так её время
    # совпадает с временем исходника кадр в кадр: 0 — первый кадр видео
    edl = compile_edl([Clip("full", asset, 0, frame_to_us(src.frame_count, src.fps))], {asset: src}, profile)
    dst = ctx.ws.project(ctx.pid, "proxies") / f"{asset}-{ctx.job['params_hash'][:8]}.mp4"
    ctx.progress(0, "лёгкая копия")
    _render(ctx, edl, dst, PROXY)
    return dst


def run_preview(ctx: Context) -> Path:
    import json

    params = json.loads(ctx.job["params_json"])
    edl = store.compile_revision(ctx.conn, ctx.pid, ctx.job["revision_id"])
    a, b = params["out_start_frame"], params["out_end_frame"]
    part = edl.slice(a, b)
    dst = ctx.ws.project(ctx.pid, "previews") / f"{ctx.job['params_hash']}.mp4"
    ctx.progress(0, "пробный ролик")
    # План — по всему ролику, потом его часть: смена плана на склейках и
    # укрупнение с возвратом идут так же, как в готовом ролике
    plan = _plan(ctx, edl)
    plan = plan.slice(a, b) if plan else None
    if plan and all(s.layout == "screen" and s.zoom is None and not (s.whip_in or s.whip_out) for s in plan.shots):
        plan = None  # в этой части только экран как есть
    _render(ctx, part, dst, PREVIEW, plan=plan, overlays=_overlays(ctx, part))
    return dst


def _draw_overlays(ctx: Context, edl: CompiledEdl, overlays) -> list:
    """Нарисовать графику (или взять из кеша): папки кадров для сборки."""
    from longform import graphics

    p = store._one(ctx.conn, "SELECT width, height FROM projects WHERE id = ?", ctx.pid)
    cache = ctx.ws.project(ctx.pid, "cache") / "graphics"
    out = []
    for i, o in enumerate(overlays):
        ctx.progress(0, f"графика {i + 1} из {len(overlays)}")
        if graphics.page_refs(o.template, o.props):
            from longform import pages

            # Вставке нужен сам снимок: путь и размер берутся из записи страницы
            def page(ref: str) -> dict:
                pg = pages.get_page(ctx.conn, ctx.pid, ref)
                return {"src": str(pages.path(ctx.ws, ctx.pid, pg["id"])), "w": pg["width"], "h": pg["height"], "bg": pg["bg"]}

            o = replace(o, props=graphics.with_pages(o.template, o.props, page))
        frames_dir = graphics.render_frames(o, edl.profile.fps, p["width"], p["height"], cache, ctx.cancel)
        # Переход PAN и удержание последнего кадра — делает сборка (render/select.py)
        out.append((frames_dir, o.start, o.frames + o.extend, 0, {"pan": o.pan, "drawn": o.frames}))
    return out


def _overlays(ctx: Context, edl: CompiledEdl):
    from longform import graphics

    return graphics.overlays(ctx.conn, ctx.pid, edl)


def _plan(ctx: Context, edl: CompiledEdl):
    """Слой кадра для ролика: раскладка в размере записи экрана."""
    from longform import visual

    p = store._one(ctx.conn, "SELECT width, height FROM projects WHERE id = ?", ctx.pid)
    return visual.build_plan(ctx.conn, ctx.pid, edl, p["width"], p["height"], clips_dir=ctx.ws.broll,
                             bg_dir=ctx.ws.backgrounds)


def run_export(ctx: Context) -> Path:
    from datetime import datetime

    rid = ctx.job["revision_id"]
    rev = store._one(ctx.conn, "SELECT * FROM revisions WHERE id = ?", rid)
    if rev["state"] != "approved":
        raise EdlError("not_approved", "финальный экспорт — только из утверждённой версии")
    edl = store.compile_revision(ctx.conn, ctx.pid, rid)
    # Проверки до сборки: критическая ошибка не даёт начать
    cut = qa.unapproved_protected_cuts(ctx.conn, ctx.pid, rid, edl)
    if cut:
        raise EdlError("protected_cut", f"версия вырезает защищённое без подтверждённой причины: {cut}")
    name = store._one(ctx.conn, "SELECT name FROM projects WHERE id = ?", ctx.pid)["name"]
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip() or "ролик"
    dst = ctx.ws.project(ctx.pid, "exports") / f"{safe} v{rev['number']} {datetime.now():%Y-%m-%d %H%M%S}.mp4"
    ctx.progress(0, "экспорт")
    # Финальное имя ролик получает только после проверки и отчёта: до этого
    # он лежит как «.part», и при сбое проверки удаляется
    lufs = store._one(ctx.conn, "SELECT loudness_lufs FROM projects WHERE id = ?", ctx.pid)["loudness_lufs"]
    settings = replace(FINAL, loudness_lufs=lufs)
    plan = _plan(ctx, edl)
    overlays = _overlays(ctx, edl)
    result = _render(ctx, edl, dst, settings, span=(0, 0.8), finalize=False, plan=plan, overlays=overlays)
    part = result.path
    covered = [(o.start, o.start + o.frames) for o in overlays]

    def plain(frame: int) -> bool:
        # Экран как есть: без лица, приближения и графики поверх
        return (plan is None or plan.is_plain(frame)) and not any(a <= frame < b for a, b in covered)
    try:
        return _check_export(ctx, edl, rid, rev, part, dst, result.loudness, plain if (plan or covered) else None)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _check_export(ctx: Context, edl: CompiledEdl, rid: str, rev, part: Path, dst: Path,
                  loudness: dict | None = None, plain=None) -> Path:
    """Проверки после сборки — по раскодированному результату."""
    ctx.progress(0.8, "проверка результата")
    sound = store.audio_from(ctx.conn, ctx.pid)
    used = {c.asset_id for c in edl.clips}
    media = media_for(ctx, used | {cam for a, (cam, _, _) in sound.items() if a in used})
    out = probe(part)
    track_samples = _audio_track_samples(part)
    # Звук сверяем с тем, что в ролик и шло: со звуком камеры, если выбран он
    from longform.render.audio import camera_audio

    cache = ctx.ws.project(ctx.pid, "cache")
    heard = {a: camera_audio(media[a], media[cam], off, drift, edl.profile.sample_rate, cache, ctx.cancel)
             for a, (cam, off, drift) in sound.items() if a in used}
    # Стыки перестановок — всегда в выборке сверки: там сборка прыгает по исходнику
    moved = [j["out_frame"] for j in store.joints(edl) if j["kind"] == "move"]
    report = verify_output(edl, media, part, cancel=ctx.cancel, plain=plain, audio=heard or None, must=moved)
    technical = {
        "frames": {"ok": out.video.frame_count == edl.total_frames,
                   "expected": edl.total_frames, "actual": out.video.frame_count},
        "constant_frame_rate": {"ok": out.video.is_cfr},
        "audio_samples": {"ok": track_samples == edl.total_samples,
                          "expected": edl.total_samples, "actual": track_samples},
        "matches_revision": {**report.as_dict(), "move_joints": len(moved)},
        "protected_ranges": {"ok": True, "cut_without_reason": []},
        "revision": {"ok": True, "id": rid, "number": rev["number"]},
        # Не ошибка, а напоминание: что из сделанного Claude автор ещё не смотрел
        "agent_review": {"ok": True, **store.agent_review_counts(ctx.conn, ctx.pid, rid)},
    }
    if loudness is not None and out.audio:
        # Громкость готового ролика меряем заново — по раскодированному звуку
        from longform.render.loudness import measure

        ctx.progress(0.95, "замер громкости")
        after = measure(part, ctx.cancel)
        technical["loudness"] = {"ok": True, **loudness, "after_lufs": round(after.integrated, 1),
                                 "after_true_peak": round(after.true_peak, 1)}
    part.rename(dst)
    qa.create_report(ctx.conn, ctx.pid, rid, ctx.job["id"], technical)
    _write_captions(ctx, rid, dst)
    return dst


def _write_captions(ctx: Context, rid: str, dst: Path) -> None:
    """Субтитры и главы рядом с готовым роликом. Ролик уже проверен и готов,
    поэтому сбой здесь не портит экспорт: файлы можно скачать и потом."""
    from longform import captions

    try:
        cues = captions.revision_cues(ctx.conn, ctx.pid, rid)
        if cues:
            dst.with_suffix(".srt").write_text(captions.to_srt(cues), encoding="utf-8")
        chapters = captions.revision_chapters(ctx.conn, ctx.pid, rid)
        if chapters["items"]:
            dst.with_suffix(".chapters.txt").write_text(chapters["text"] + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — экспорт уже удался, файлы рядом — удобство
        log.exception("субтитры и главы рядом с экспортом не записаны")


def _audio_track_samples(path: Path) -> int | None:
    import json

    from longform.ffmpeg import run

    data = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=duration_ts", "-of", "json", str(path)]))
    streams = data.get("streams") or []
    return int(streams[0]["duration_ts"]) if streams and "duration_ts" in streams[0] else None


def _audio_offset_us(row) -> int:
    from fractions import Fraction

    return round(Fraction(row["audio_offset_num"], row["audio_offset_den"]) * 1_000_000)


def _source_wav(ctx: Context, row, media: ProbeResult) -> Path:
    """WAV исходника в кеше проекта; тот же файл берёт и склейка."""
    from longform.render.audio import extract_wav

    wav = ctx.ws.project(ctx.pid, "cache") / f"{row['id']}.wav"
    if not wav.exists():
        extract_wav(media.path, wav, media.audio.index, cancel=ctx.cancel)
    return wav


def _wav16(ctx: Context, asset_id: str, media: ProbeResult) -> Path:
    """16 кГц моно для детектора речи и Whisper; общий кеш."""
    from longform import transcribe

    wav16 = ctx.ws.project(ctx.pid, "cache") / f"{asset_id}-16k.wav"
    if not wav16.exists():
        transcribe.make_16k(media.path, wav16, media.audio.index, cancel=ctx.cancel)
    return wav16


def _analyze(ctx: Context, asset_id: str, span: tuple[float, float]) -> Path:
    from longform import asset_data
    from longform.analysis import measure, run_vad

    row = store._one(ctx.conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", asset_id, ctx.pid)
    media = media_for(ctx, [asset_id])[asset_id]
    a, b = span
    ctx.progress(a, "звук исходника")
    wav = _source_wav(ctx, row, media)
    ctx.progress(a + (b - a) / 3, "громкость")
    db = measure(wav, cancel=ctx.cancel)
    ctx.progress(a + 2 * (b - a) / 3, "детектор речи")
    vad = run_vad(_wav16(ctx, asset_id, media), cancel=ctx.cancel)
    path = ctx.ws.project(ctx.pid, "analysis") / f"{asset_id}-{row['fingerprint'][:8]}.npy"
    asset_data.save_analysis(ctx.conn, asset_id, row["fingerprint"], db, path, _audio_offset_us(row), vad)
    return path


def run_analyze(ctx: Context) -> Path:
    return _analyze(ctx, ctx.job["asset_id"], (0, 1))


def run_transcribe(ctx: Context) -> Path:
    import json

    from longform import asset_data, transcribe

    asset_id = ctx.job["asset_id"]
    params = {"language": "ru", "model": transcribe.MODEL.name, **json.loads(ctx.job["params_json"])}
    if asset_data.loudness(ctx.conn, asset_id) is None:
        _analyze(ctx, asset_id, (0, 0.1))  # без громкости не понять, где в сегменте речь
    row = store._one(ctx.conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", asset_id, ctx.pid)
    existing = asset_data.current_transcript(ctx.conn, asset_id)
    out_dir = ctx.ws.project(ctx.pid, "transcripts") / asset_id / ctx.job["params_hash"][:16]
    if existing is not None and existing["params_hash"] == ctx.job["params_hash"]:
        return out_dir
    media = media_for(ctx, [asset_id])[asset_id]
    ctx.progress(0.1, "звук для расшифровки")
    wav16 = _wav16(ctx, asset_id, media)
    import wave

    with wave.open(str(wav16)) as w:
        duration_ms = w.getnframes() * 1000 // w.getframerate()
    plan = transcribe.chunk_plan(duration_ms)
    out_dir.mkdir(parents=True, exist_ok=True)
    parsed = []
    for i, part in enumerate(plan):
        start, length, _, _ = part
        chunk = out_dir / f"chunk-{start:010d}.json"
        if not chunk.exists():  # готовые куски после сбоя не считаем заново
            ctx.progress(0.15 + 0.8 * i / len(plan), f"расшифровка: кусок {i + 1} из {len(plan)}")
            transcribe.run_chunk(wav16, chunk, start, length, params["language"], cancel=ctx.cancel)
        parsed.append((part, transcribe.parse_chunk(chunk)))
    segments = transcribe.merge(parsed)
    # Перечитать места, где речь есть, а текста нет или там выдумка Whisper
    loud = asset_data.loudness(ctx.conn, asset_id)
    if loud is not None:
        offset_us = _audio_offset_us(row)
        speech = [((a - offset_us) // 1000, (b - offset_us) // 1000)
                  for a, b in loud.speech_runs(offset_us, offset_us + duration_ms * 1000)]
        windows = transcribe.repair_windows(segments, speech, duration_ms)
        repaired = []
        for i, (a, b) in enumerate(windows):
            ctx.progress(0.95, f"перечитываю пропуски: {i + 1} из {len(windows)}")
            out = out_dir / f"repair-{a:010d}-{b - a:06d}.json"
            if not out.exists():
                transcribe.run_chunk(wav16, out, a, b - a, params["language"], cancel=ctx.cancel)
            repaired.append(((a, b), transcribe.parse_chunk(out)))
        segments = transcribe.apply_repair(segments, repaired)
    ctx.progress(0.97, "сохраняю расшифровку")
    asset_data.save_transcript(ctx.conn, asset_id, ctx.pid, params_hash=ctx.job["params_hash"],
                               model=params["model"], language=params["language"], segments=segments,
                               audio_offset_us=_audio_offset_us(row))
    # Кашель здесь не ищется: только в том, что останется после чистки (list_noises у Claude)
    return out_dir


def _find_noises(ctx: Context, asset_id: str, wav16: Path, row, ranges=None, language: str = "ru") -> int:
    """Кашель и «кхм» по записи экрана (longform.noises): в отрезках ranges
    (время записи) или во всей записи."""
    import wave

    from longform import asset_data, noises, transcribe

    loud = asset_data.loudness(ctx.conn, asset_id)
    t = asset_data.current_transcript(ctx.conn, asset_id)
    if loud is None or t is None:
        return 0
    offset = _audio_offset_us(row)
    with wave.open(str(wav16)) as w:
        duration_us = w.getnframes() * 1_000_000 // w.getframerate()
    hear = noises.hear_with_whisper(wav16, offset, transcribe.MODEL, language)
    where = noises.merge(ranges) if ranges else [(offset, offset + duration_us)]
    found = noises.detect_in(loud, noises.word_mids(ctx.conn, asset_id), where, hear, ctx.cancel,
                             lambda k, n: ctx.progress(k / max(n, 1), f"кашель и «кхм»: проверяю {k + 1} из {n}"))
    noises.save(ctx.conn, asset_id, found, t["id"], where if ranges else None)
    return len(found)


def run_noises(ctx: Context) -> Path:
    """Поиск кашля и «кхм» у уже расшифрованной записи — в отрезках из
    параметров (оставленное в версии) или во всей записи."""
    import json

    from longform import asset_data

    asset_id = ctx.job["asset_id"]
    row = store._one(ctx.conn, "SELECT * FROM assets WHERE id = ? AND project_id = ?", asset_id, ctx.pid)
    if asset_data.current_transcript(ctx.conn, asset_id) is None:
        raise EdlError("no_transcript", "сначала расшифровка: кашель ищется по ней")
    media = media_for(ctx, [asset_id])[asset_id]
    wav16 = _wav16(ctx, asset_id, media)
    _find_noises(ctx, asset_id, wav16, row, json.loads(ctx.job["params_json"]).get("ranges"))
    return wav16


def run_sync(ctx: Context) -> Path:
    """Свести камеру с записью экрана по звуку (longform.sync)."""
    import json

    from longform import sync

    cam_id = ctx.job["asset_id"]
    cam_row = store._one(ctx.conn, "SELECT * FROM assets WHERE id = ?", cam_id)
    mains = ctx.conn.execute("SELECT * FROM assets WHERE project_id = ? AND role = 'main' ORDER BY position",
                             (ctx.pid,)).fetchall()
    if not mains:
        raise EdlError("no_main", "нет записи экрана с микрофоном, с которой сводить камеру")
    media = media_for(ctx, [cam_id, *(m["id"] for m in mains)])
    ctx.progress(0.05, "звук камеры")
    cam_audio = sync.decode(media[cam_id].path, media[cam_id].audio.index, ctx.cancel)
    cam_offset = media[cam_id].audio.start - media[cam_id].video.start
    best = None
    for i, m in enumerate(mains):
        ctx.progress(0.1 + 0.8 * i / len(mains), f"сведение с {Path(m['path']).name}")
        main_audio = sync.decode(media[m["id"]].path, media[m["id"]].audio.index, ctx.cancel)
        r = sync.synchronize(main_audio, cam_audio, media[m["id"]].audio.start - media[m["id"]].video.start, cam_offset)
        rank = ({"ok": 2, "weak": 1}.get(r.state, 0), r.coarse_peak)
        if best is None or rank > best[0]:
            best = (rank, m["id"], r)
    _, main_id, r = best
    report = {"points": r.points, "residual_us": r.residual_us, "coarse_peak": r.coarse_peak,
              "measured": {"offset_us": r.offset_us, "drift_ppm": r.drift_ppm, "state": r.state}}
    store.set_sync(ctx.conn, ctx.pid, cam_id, main_id if r.state != "failed" else None, r.state,
                   r.offset_us, r.drift_ppm, report)
    out = ctx.ws.project(ctx.pid, "analysis") / cam_id / "sync.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"main": main_id, **report}, ensure_ascii=False))
    if r.state == "failed":
        raise EdlError("sync_failed", f"{Path(cam_row['path']).name}: звук камеры не совпал ни с одной записью экрана")
    return out


RUNNERS: dict[str, Callable[[Context], Path]] = {
    "proxy": run_proxy,
    "sync": run_sync,
    "preview": run_preview,
    "export": run_export,
    "analyze": run_analyze,
    "transcribe": run_transcribe,
    "noises": run_noises,
}

READABLE = (MediaToolError, EdlError, UnsupportedMedia, SourceChanged, DiskSpaceError, RenderCheckError,
            FileExistsError)


def execute(conn: sqlite3.Connection, ws: Workspace, job: sqlite3.Row, stop: dict) -> str:
    """Выполнить одну задачу и записать итог. Возвращает итоговое состояние."""
    ctx = Context(conn, ws, job, stop)
    started = time.monotonic()
    runner = RUNNERS.get(job["type"])
    try:
        if runner is None:
            raise EdlError("no_runner", f"задачи типа {job['type']} этот исполнитель ещё не умеет")
        output = runner(ctx)
        jobs.finish(conn, job["id"], "completed", output_path=output, message="готово")
        state = "completed"
    except Cancelled as c:
        state = "cancelled" if c.reason == "user" else "interrupted"
        jobs.finish(conn, job["id"], state,
                    message="отменена" if state == "cancelled" else "прервана, продолжится после перезапуска")
    except READABLE as e:
        state = "failed"
        jobs.finish(conn, job["id"], state, error=str(e)[:2000], message="ошибка")
    except Exception:
        state = "failed"
        log.error("задача %s: внутренняя ошибка\n%s", job["id"], traceback.format_exc())
        jobs.finish(conn, job["id"], state, error="внутренняя ошибка, подробности в logs/worker.log",
                    message="ошибка")
    log.info("задача %s (%s): %s за %.1f с", job["id"], job["type"], state, time.monotonic() - started)
    return state


CHILD_FILE = "worker-child.json"
OUR_TOOLS = ("ffmpeg", "ffprobe", "whisper-cli")


def track_children(ws: Workspace) -> None:
    """Записывать, какой ffmpeg/whisper сейчас запущен (см. ffmpeg.child_hook)."""
    import json

    from longform import ffmpeg

    path = ws.run_dir / CHILD_FILE

    def hook(pid: int | None) -> None:
        if pid is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"pid": pid}))
    ffmpeg.child_hook = hook


def kill_orphan(ws: Workspace) -> int | None:
    """Добить ffmpeg/whisper, оставшийся от жёстко убитого исполнителя.

    Перед убийством проверяем, что под этим номером процесса всё ещё наш
    инструмент: номер мог достаться другой программе. Возвращает убитый pid.
    """
    import json
    import subprocess

    path = ws.run_dir / CHILD_FILE
    try:
        pid = int(json.loads(path.read_text())["pid"])
    except (OSError, ValueError, KeyError):
        return None
    path.unlink(missing_ok=True)
    name = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    if not name or Path(name).name not in OUR_TOOLS:
        return None
    try:
        os.killpg(pid, signal.SIGKILL)  # свой сеанс: pid процесса — номер его группы
    except ProcessLookupError:
        return None
    log.info("добил осиротевший %s (pid %s)", Path(name).name, pid)
    return pid


def acquire_lock(ws: Workspace):
    handle = open(ws.run_dir / "worker.lock", "w")  # noqa: SIM115 — держим открытым всё время работы
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def serve(ws: Workspace, stop: dict, parent_pid: int | None = None, idle_sleep: float = 0.5,
          once: bool = False) -> None:
    conn = connect(ws.db)
    migrate(conn)
    kill_orphan(ws)
    track_children(ws)
    requeued = jobs.recover(conn)
    if requeued:
        log.info("вернул в очередь прерванные задачи: %s", requeued)
    started = store._now()
    stop.setdefault("started", started)
    if parent_pid is not None:
        stop.setdefault("parent_pid", parent_pid)
    while not stop.get("reason"):
        if parent_pid is not None and os.getppid() != parent_pid:
            log.info("сервер, запустивший исполнителя, завершился — останавливаюсь")
            break
        jobs.heartbeat(conn, os.getpid(), started)
        job = jobs.claim(conn, os.getpid())
        if job is None:
            if once:
                break
            time.sleep(idle_sleep)
            continue
        execute(conn, ws, job, stop)
    conn.close()


def main() -> None:
    ws = Workspace.from_env()
    logging.basicConfig(filename=ws.logs / "worker.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    lock = acquire_lock(ws)
    if lock is None:
        print("исполнитель уже запущен", file=sys.stderr)
        sys.exit(1)
    stop: dict = {}

    def handle(signum, _frame):
        stop["reason"] = "shutdown"

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGHUP, handle)  # закрыли терминал — остановиться так же аккуратно
    parent = os.environ.get("LONGFORM_WORKER_PARENT")
    log.info("исполнитель запущен, pid %s", os.getpid())
    serve(ws, stop, parent_pid=int(parent) if parent else None)
    log.info("исполнитель остановлен")


if __name__ == "__main__":
    main()

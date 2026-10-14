"""Моушн-графика: поля шаблонов, место в ролике, пометка сокращённого
ожидания, наложение при сборке."""

import numpy as np
import pytest

from longform import graphics, jobs, qa, store, worker
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.ops import Op
from longform.timebase import frame_to_us
from longform.workspace import Workspace
from media_fixtures import iter_frames, read_frame_number

S = 1_000_000


def us(frame):
    return frame_to_us(frame, 25)


def test_fields_are_checked_per_template():
    assert graphics.validate("steps", {"steps": ["читает", "делает"], "active": 1}) == \
        {"steps": ["читает", "делает"], "active": 1}
    assert graphics.validate("command", {"text": "uv run  longform-agent prj_1"})["text"] == "uv run  longform-agent prj_1"
    assert graphics.validate("checklist", {"items": [{"text": "установили", "state": "done"}]})["items"][0]["state"] == "done"
    for template, props, words in [
        ("steps", {"steps": ["один"]}, "от 2"),
        ("steps", {"steps": ["а", "б"], "active": 5}, "active"),
        ("term", {"term": "skill"}, "definition"),
        ("keyword", {"text": "х" * 200}, "до 90"),
        ("checklist", {"items": [{"text": "а", "state": "почти"}]}, "state"),
        ("rocket", {}, "шаблон"),
    ]:
        with pytest.raises(EdlError, match=words):
            graphics.validate(template, props)



@pytest.fixture
def project(tmp_path, fx_plain):
    ws = Workspace(tmp_path / "рабочая")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "графика", "user")
    a = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    yield ws, conn, p["id"], a["id"], rev["id"]
    conn.close()


def test_place_in_the_cut_and_waiting_badge(project):
    ws, conn, pid, aid, rev = project
    g = graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=8 * S, src_end_us=14 * S,
                             template="keyword", props={"text": "Нужна проверка человеком"}, goal="главный вывод")
    assert g["review"] == "pending"
    with pytest.raises(Exception, match="пересекается"):
        graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=12 * S, src_end_us=16 * S,
                             template="keyword", props={"text": "x"}, goal="g")
    with pytest.raises(EdlError, match="1,5"):
        graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=30 * S, src_end_us=31 * S,
                             template="keyword", props={"text": "x"}, goal="g")
    # Claude сокращает ожидание 10–12 с внутри графики и тишину 40–44 с
    store.create_proposal(conn, pid, base_revision_id=rev, pass_="cleanup", rationale="r", policy_version="t",
                          actor="agent", align=False,
                          ops=[Op("remove", aid, 10 * S, 12 * S, "waiting", "модель думает"),
                               Op("remove", aid, 40 * S, 44 * S, "technical_dead_air", "тишина")])
    edl = store.compile_revision(conn, pid, store.get_project(conn, pid)["current_revision_id"])
    out = graphics.overlays(conn, pid, edl)
    # Графика 8–14 с разрезана вырезом 10–12: куски по 2 с идут в ролике встык —
    # это один рисунок на 4 с (100 кадров), он не появляется заново на стыке
    assert [(o.template, o.start, o.frames) for o in out if o.template == "keyword"] == [("keyword", 200, 100)]
    # Пометка — только у ожидания, на стыке (кадр 250), 1,6 с
    badges = [o for o in out if o.template == "time_compressed"]
    assert [(o.start, o.frames) for o in badges] == [(250, 40)]
    # Автор вернул графику — её нет; выключил пометку — нет и её
    graphics.review_graphic(conn, pid, g["id"], "revert", "user")
    conn.execute("UPDATE projects SET mark_waiting = 0")
    assert graphics.overlays(conn, pid, edl) == []


def test_pan_and_hold_between_fullscreen_graphics(project):
    """Между двумя графиками во весь кадр лицо не мелькает, следующая входит
    переходом PAN. Графика с enter=pan въезжает за 0,3 с; та, что кончается
    ровно там, держится, пока следующая въезжает."""
    ws, conn, pid, aid, rev = project
    graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=20 * S, src_end_us=24 * S,
                         template="chapter", props={"title": "Глава", "hold": True}, goal="глава")
    graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=24 * S, src_end_us=28 * S,
                         template="steps", props={"steps": ["Нарезка", "Сборка"], "active": 0, "enter": "pan"},
                         goal="этап")
    edl = store.compile_revision(conn, pid, rev)
    out = {o.template: o for o in graphics.overlays(conn, pid, edl)}
    assert out["steps"].pan == 8 and out["steps"].props["enter"] == "pan"  # 0,3 с при 25 к/с
    assert out["chapter"].extend == 8 and out["chapter"].props["hold"] is True
    assert out["chapter"].pan == 0 and out["steps"].extend == 0
    # У вставки страницы своё enter (cut, up) остаётся; pan — общее
    assert graphics.validate("chapter", {"title": "x", "enter": "cut"})["enter"] == "cut"
    for bad in ({"enter": "slide"}, {"hold": "да"}):
        with pytest.raises(EdlError):
            graphics.validate("chapter", {"title": "x", **bad})


@pytest.mark.media
def test_pan_moves_frame_left_and_graphic_in(project, fx_plain):
    """Переход PAN в собранном ролике: до графики — кадр исходника; в середине
    перехода картинка уехала влево на полкадра (правая половина — уже графика);
    после — графика целиком."""
    ws, conn, pid, aid, rev = project
    graphics.add_graphic(conn, pid, actor="user", asset_id=aid, src_start_us=20 * S, src_end_us=24 * S,
                         template="chapter", props={"title": "Этап", "enter": "pan"}, goal="этап")
    job = jobs.enqueue(conn, pid, "export", actor="user", revision_id=rev)
    worker.serve(ws, stop={}, once=True)
    done = jobs.get_job(conn, pid, job["id"])
    assert done["state"] == "completed", done["error"]
    frames = list(iter_frames(done["output_path"]))
    assert len(frames) == 1500
    source = list(iter_frames(fx_plain.path))
    h, w = source[0].shape[:2]
    d = lambda a, b: float(np.abs(a.astype(int) - b.astype(int)).mean())  # noqa: E731
    assert d(frames[499], source[499]) < 3
    mid = frames[504]  # 0,3 с = 8 кадров; середина — 4-й, smoothstep(0,5) = 0,5: сдвиг на полкадра
    # Левая половина — правая половина исходника (размыта по горизонтали): ближе к ней, чем к своему месту
    assert d(mid[:, : w // 2], source[504][:, w // 2:]) < d(mid[:, : w // 2], source[504][:, : w // 2])
    # Правая половина — графика: тёмная сцена
    assert float(mid[:, w // 2 + 8:].mean()) < 40
    assert d(frames[530], source[530]) > 40


@pytest.mark.media
def test_export_draws_graphics_only_in_their_place(project, fx_plain):
    ws, conn, pid, aid, rev = project
    graphics.add_graphic(conn, pid, actor="user", asset_id=aid, src_start_us=20 * S, src_end_us=24 * S,
                         template="keyword", props={"text": "Главное — проверка"}, goal="вывод")
    job = jobs.enqueue(conn, pid, "export", actor="user", revision_id=rev)
    worker.serve(ws, stop={}, once=True)
    done = jobs.get_job(conn, pid, job["id"])
    assert done["state"] == "completed", done["error"]
    tech = qa.list_reports(conn, pid)[0]["technical"]
    assert tech["frames"]["ok"] and tech["audio_samples"]["ok"] and tech["matches_revision"]["ok"]
    frames = list(iter_frames(done["output_path"]))
    assert len(frames) == 1500
    # Сравниваем с тем же кадром исходника: вне графики — тот же кадр, на её
    # участке (кадры 500–600) — поверх карточка с текстом
    source = list(iter_frames(fx_plain.path))
    diff = lambda k: float(np.abs(frames[k].astype(int) - source[k].astype(int)).max())  # noqa: E731
    assert read_frame_number(frames[100]) == 100 and read_frame_number(frames[1300]) == 1300
    assert diff(100) < 40 and diff(499) < 40 and diff(620) < 40
    assert diff(550) > 100
    # Тот же экспорт снова — из кеша рисунков; поменяли графику — новая задача
    assert jobs.enqueue(conn, pid, "export", actor="user", revision_id=rev)["id"] == job["id"]
    graphics.add_graphic(conn, pid, actor="user", asset_id=aid, src_start_us=30 * S, src_end_us=33 * S,
                         template="lower_third", props={"title": "Claude Code"}, goal="название")
    assert jobs.enqueue(conn, pid, "export", actor="user", revision_id=rev)["id"] != job["id"]


@pytest.mark.media
def test_export_with_fullscreen_motion_keeps_frames(project, fx_plain):
    """Графика во весь кадр, сплошная посередине и прозрачная на уходе: Chrome
    сохраняет сплошные кадры без прозрачности — без выравнивания формата ffmpeg
    пересобирал фильтры посреди рисунка, и ролик выходил с лишними кадрами."""
    ws, conn, pid, aid, rev = project
    graphics.add_graphic(conn, pid, actor="user", asset_id=aid, src_start_us=20 * S, src_end_us=22 * S,
                         template="chapter", props={"title": "Тайм-коды", "number": "2", "enter": "cut"}, goal="глава")
    job = jobs.enqueue(conn, pid, "export", actor="user", revision_id=rev)
    worker.serve(ws, stop={}, once=True)
    done = jobs.get_job(conn, pid, job["id"])
    assert done["state"] == "completed", done["error"]
    frames = list(iter_frames(done["output_path"]))
    assert len(frames) == 1500
    source = list(iter_frames(fx_plain.path))
    diff = lambda k: float(np.abs(frames[k].astype(int) - source[k].astype(int)).mean())  # noqa: E731
    # Вне главы — кадр исходника; в середине (кадр 525) — сплошной фон сцены
    assert diff(480) < 3 and diff(570) < 3 and diff(525) > 40
    cache = next(p for p in ws.project(pid, "cache").rglob("spec.json") if '"chapter"' in p.read_text()).parent
    assert len({graphics._png_color_type(p) for p in cache.glob("[0-9]*.png")}) == 1


@pytest.mark.media
def test_templates_fit_the_frame(tmp_path):
    """Стартовые шаблоны на обычном и длинном содержании: каждая строка текста в
    кадре с полями и никакие две строки не налезают друг на друга и на
    картинку (graphics/fitcheck.mjs)."""
    import json
    import subprocess
    page = tmp_path / "страница.png"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=1800x6000", "-frames:v", "1", str(page)], check=True)
    long = "Почему автоматизация документации ломается на больших проектах"
    raw = [
        ("lower_third", {"title": "Claude Code", "subtitle": "агент в терминале"}),
        ("lower_third", {"title": long, "subtitle": long + " и как это исправить"}),
        ("term", {"term": "Контекстное окно", "definition": "Сколько текста модель держит в голове за один раз. " * 3}),
        ("keyword", {"text": long[:90]}),
        ("chapter", {"title": long[:80], "number": "12"}),
        ("steps", {"title": "Как начать", "steps": ["Скачать", "Запустить", "Проверить", "Поправить"], "active": 1}),
        ("compare", {"title": "Два способа", "left": {"title": "Руками", "items": ["долго", "без ошибок", "дорого"]},
                     "right": {"title": "Агентом", "items": ["быстро", "надо проверять", "дёшево"]}, "note": "решает задача"}),
        ("checklist", {"title": "Перед записью", "items": [{"text": "микрофон", "state": "done"}, {"text": "свет", "state": "now"},
                                                          {"text": "экран", "state": "todo"}]}),
        ("command", {"text": "uv run longform-agent prj_0123456789ab", "caption": "в папке backend"}),
        ("page_insert", {"page": "0123456789abcdef"}),
    ]
    cases = []
    for i, (template, props) in enumerate(raw):
        clean = graphics.validate(template, props)  # поля — допустимые для сервера
        if template == "page_insert":
            clean = {**clean, "src": str(page), "w": 1800, "h": 6000, "bg": "#ffffff"}
        cases.append({"name": f"{i}-{template}", "template": template, "props": clean, "durationMs": 4000})
    spec = tmp_path / "cases.json"
    spec.write_text(json.dumps({"width": 1920, "height": 1080, "cases": cases}, ensure_ascii=False))
    out = subprocess.run(["node", str(graphics.GRAPHICS_DIR / "fitcheck.mjs"), str(spec)], cwd=graphics.GRAPHICS_DIR,
                         capture_output=True, text=True, check=True).stdout
    problems = {r["name"]: r["problems"] for r in json.loads(out) if r["problems"]}
    assert problems == {}


def test_capture_finds_phrase_lines_for_highlight(tmp_path):
    """capture.mjs с фразой: находит её на странице без учёта регистра, лишних
    пробелов, типографских кавычек и «ё», отдаёт прямоугольники строк (фраза на
    двух строках — два); фразы нет — marks: null. Для своего шаблона-маркера."""
    import json
    import subprocess
    page = tmp_path / "страница.html"
    page.write_text("""<!doctype html><meta charset="utf-8"><body style="margin:40px;font:20px/1.5 sans-serif">
        <h1>Скиллы</h1><p style="width:420px">Навыки загружаются по требованию, поэтому вам не нужно повторять
        одни и те же «указания» в разных разговорах. Ещё одна строка.</p></body>""", encoding="utf-8")
    out = tmp_path / "снимок.png"

    def run(phrase):
        done = subprocess.run(["node", str(graphics.GRAPHICS_DIR / "capture.mjs"), page.as_uri(), str(out), "900", "4000", phrase],
                              capture_output=True, text=True, timeout=120, check=True)
        return json.loads(done.stdout.strip().splitlines()[-1])

    meta = run('поэтому вам  не нужно повторять одни и те же "указания" в разных')
    marks = meta["marks"]
    assert marks and len(marks) >= 2  # фраза переносится в колонке 420 точек
    for x0, y0, x1, y1 in marks:
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    assert marks[1][1] > marks[0][1]  # строки сверху вниз
    assert run("ЕЩЕ ОДНА СТРОКА")["marks"]  # ё и регистр
    assert run("такой фразы на странице нет")["marks"] is None


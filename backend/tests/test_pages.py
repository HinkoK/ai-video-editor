"""Вставки страниц: снимок по ссылке, свой снимок автора, графика page_insert.

Страница — маленький сайт, который тест поднимает сам на 127.0.0.1: интернет
не нужен, Chrome снимает её так же, как настоящую документацию."""

import functools
import http.server
import subprocess
import threading
from fractions import Fraction

import numpy as np
import pytest

from longform import graphics, pages, store
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.store import Forbidden
from longform.workspace import Workspace

pytestmark = pytest.mark.media
S = 1_000_000
PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Лучшие практики</title>
<style>body{margin:0;font:20px sans-serif} .b{height:600px} .cookie-banner{position:fixed;bottom:0;left:0;right:0;
height:200px;background:#f00}</style></head><body>
<div class="b" style="background:#204080"></div><div class="b" style="background:#40a060"></div>
<div class="b" style="background:#c08020"></div><div class="b" style="background:#802060"></div>
<div class="cookie-banner">Мы используем cookie</div></body></html>"""


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "сайт"
    root.mkdir()
    (root / "index.html").write_text(PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


@pytest.fixture
def project(tmp_path, fx_plain):
    ws = Workspace(tmp_path / "рабочая")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "вставки", "user")
    a = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    store.create_initial_revision(conn, p["id"], "user")
    yield ws, conn, p["id"], a["id"]
    conn.close()


def _rgb(path):
    w, h = pages._png_size(path)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 4)


def test_capture_and_insert(project, site, tmp_path):
    ws, conn, pid, aid = project
    pg = pages.capture(conn, ws, pid, site, "agent")
    # Двойная чёткость: окно 900 CSS-пикселей — снимок 1800; страница 2400 CSS — 4800
    assert (pg["width"], pg["height"], pg["title"], pg["bg"]) == (1800, 4800, "Лучшие практики", "#ffffff")
    img = _rgb(pages.path(ws, pid, pg["id"]))
    # Плашка про cookie спрятана: внизу — цвет страницы, а не красная плашка
    assert tuple(img[-50, 100, :3]) != (255, 0, 0) and abs(int(img[-50, 100, 0]) - 0x80) < 10
    # Тот же снимок ещё раз — та же запись, файл не дублируется
    assert pages.capture(conn, ws, pid, site, "user")["id"] == pg["id"]
    assert len(list((ws.project(pid, "pages")).glob("*.png"))) == 1
    for bad in ("file:///etc/passwd", "javascript:alert(1)"):
        with pytest.raises(EdlError, match="http"):
            pages.capture(conn, ws, pid, bad, "agent")

    # Графика: без снимка нельзя, со снимком — можно
    with pytest.raises(EdlError, match="capture_page"):
        graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=2 * S, src_end_us=5 * S,
                             template="page_insert", props={"page": "0" * 16}, goal="показать страницу")
    graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=2 * S, src_end_us=5 * S,
                         template="page_insert", props={"page": pg["id"], "enter": "up", "zoom": 0.5},
                         goal="показать страницу")
    with pytest.raises(Exception, match="пересекается"):
        graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=4 * S, src_end_us=6 * S,
                             template="keyword", props={"text": "Бесплатно"}, goal="g")
    edl = store.compile_revision(conn, pid, store.get_project(conn, pid)["current_revision_id"])
    assert [o.template for o in graphics.overlays(conn, pid, edl)] == ["page_insert"]

    # Рисунок вставки: во весь кадр, страница прокручивается вверх
    o = graphics.overlays(conn, pid, edl)[0]
    from dataclasses import replace
    o = replace(o, props={**o.props, "src": str(pages.path(ws, pid, pg["id"])), "w": pg["width"], "h": pg["height"],
                          "bg": pg["bg"]})
    d = graphics.render_frames(o, Fraction(25), 640, 360, tmp_path / "кеш")
    first, last = _rgb(d / "00020.png"), _rgb(d / "00085.png")
    assert (first[..., 3] == 255).all()
    col = lambda img: img[:, 320, :3].astype(int)  # noqa: E731
    # Граница синего и зелёного блоков к концу выше, чем в начале
    edge = lambda img: int(np.argmax(np.abs(np.diff(col(img)[40:], axis=0)).sum(axis=1) > 60)) + 40  # noqa: E731
    assert edge(last) < edge(first)


def test_own_snapshot_only_by_author(project, tmp_path):
    ws, conn, pid, aid = project
    shot = tmp_path / "каталог.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=800x600", "-frames:v", "1",
                    str(shot)], check=True)
    with pytest.raises(Forbidden):
        pages.import_file(conn, ws, pid, str(shot), "", "agent")
    pg = pages.import_file(conn, ws, pid, str(shot), "", "user")
    assert (pg["width"], pg["height"], pg["url"], pg["title"], pg["bg"]) == (800, 600, None, "каталог", "#ffffff")
    assert shot.exists()  # свой файл автора не трогаем
    with pytest.raises(EdlError, match="PNG"):
        pages.import_file(conn, ws, pid, str(tmp_path / "заметка.txt"), "", "user")


def test_pages_over_http(project, site, tmp_path):
    from fastapi.testclient import TestClient

    from longform.api import Settings, create_app

    ws, conn, pid, aid = project
    app = create_app(Settings(db_path=ws.db, user_token="u", agent_token="a", workspace_root=ws.root,
                              allowed_hosts=frozenset({"testserver"}), allowed_origins=frozenset({"http://testserver"})))
    client, user, agent = TestClient(app), {"Authorization": "Bearer u"}, {"Authorization": "Bearer a"}
    r = client.post(f"/api/projects/{pid}/pages", json={"url": site, "width": 700}, headers=agent)
    assert r.status_code == 201 and r.json()["width"] == 1400
    page_id = r.json()["id"]
    assert [p["id"] for p in client.get(f"/api/projects/{pid}/pages", headers=agent).json()] == [page_id]
    png = client.get(f"/api/projects/{pid}/pages/{page_id}/preview?width=300", headers=agent)
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.post(f"/api/projects/{pid}/pages", json={"url": site, "width": 100}, headers=agent).status_code == 422
    # Свой файл подкладывает только автор
    shot = tmp_path / "снимок.png"
    shot.write_bytes((ws.project(pid, "pages") / f"{page_id}.png").read_bytes())
    assert client.post(f"/api/projects/{pid}/pages/import", json={"path": str(shot)}, headers=agent).status_code == 403
    assert client.post(f"/api/projects/{pid}/pages/import", json={"path": str(shot)}, headers=user).status_code == 201
    assert client.get(f"/api/projects/{pid}/pages/{'f' * 16}/preview", headers=agent).status_code == 404


def test_content_box_centers_the_column(tmp_path):
    """Колонка текста во вставке — по центру рамки (правило автора 08.10):
    подсказка zoom/cx ставит середину колонки в середину кадра; колонка,
    упёртая в правый край, помечена «обрезана справа»."""
    import subprocess

    from longform.pages import COLUMN_SHARE, content_box

    def page(x, w):
        path = tmp_path / f"p{x}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=white:s=1000x600,drawbox=x={x}:y=100:w={w}:h=300:color=black:t=fill",
                        "-frames:v", "1", str(path)], check=True)
        return content_box(path)

    mid = page(300, 400)
    assert abs(mid["center"] - 0.5) < 0.01 and not mid["cut_right"]
    assert abs(mid["zoom"] - COLUMN_SHARE / 0.4) < 0.02 and mid["cx"] == 0.5
    side = page(250, 400)  # колонка 0,25–0,65: центр 0,45
    z, cx = side["zoom"], side["cx"]
    # Середина видимого окна шаблона page_insert (доли ширины снимка) = середина колонки
    assert abs((cx * (z - 1) + 0.5) / z - side["center"]) < 0.005
    assert page(400, 600)["cut_right"]

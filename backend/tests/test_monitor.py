"""Монитор программы (как в Premiere): положение окна лица и рамки экрана,
выставленное автором мышью, правка участков и графики, окно предпросмотра
графики. Устройство — docs/tech.md, «Монитор программы»."""

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from longform import graphics, store, visual
from longform.api import Settings, create_app
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.render.compose import Shot, VisualPlan, compose_graph, pip_geometry, side_geometry
from longform.store import Forbidden
from media_fixtures import make_fixture

S = 1_000_000


def test_face_and_card_boxes_are_checked():
    assert visual.validate_params("side", {"face": {"x": 0.7, "y": 0.6, "size": 0.2}, "card": {"x": 0.05, "y": 0.1, "w": 0.6}}) == \
        {"face": {"x": 0.7, "y": 0.6, "size": 0.2}, "card": {"x": 0.05, "y": 0.1, "w": 0.6}}
    assert visual.validate_params("pip", {"face": {"x": 0.7, "y": 0.6, "size": 0.25}})["face"] == {"x": 0.7, "y": 0.6, "size": 0.25}
    for layout, params, words in [
        ("camera", {"face": {"x": 0.1, "y": 0.1, "size": 0.2}}, "face"),
        ("pip", {"card": {"x": 0.1, "y": 0.1, "w": 0.5}}, "card"),
        ("side", {"face": {"x": 0.1, "y": 0.1, "size": 2}}, "face.size"),
        ("side", {"card": [0.1, 0.1, 0.5]}, "card"),
    ]:
        with pytest.raises(EdlError, match=words):
            visual.validate_params(layout, params)


def test_boxes_move_the_face_and_the_screen_in_the_render():
    w, h = 1920, 1080
    plain = side_geometry(Shot(0, 10, "side"), w, h, 1920, 1080)
    moved = side_geometry(Shot(0, 10, "side", face_box=(0.05, 0.6, 0.2), card_box=(0.3, 0.05, 0.65)), w, h, 1920, 1080)
    assert moved["face"] == (96, 648, 384) and moved["face"] != plain["face"]
    # Высота рамки — по пропорции экрана
    assert moved["card"] == (576, 54, 1248, 702)
    assert pip_geometry(Shot(0, 10, "pip", face_box=(0.1, 0.1, 0.25)), w, h, 1920, 1080) == (192, 108, 480, 270)
    # В сборке окно лица стоит там, где автор его поставил
    graph = compose_graph(VisualPlan(w, h, (Shot(0, 10, "side", face_box=(0.05, 0.6, 0.2)),), {}), "[S]", "[C]",
                          Fraction(25), (1920, 1080), (1920, 1080))
    assert "overlay=96:648" in graph


@pytest.fixture
def synced(tmp_path):
    screen = make_fixture(tmp_path / "экран.mp4", fps=Fraction(25), frames=500, gop=25)
    camera = make_fixture(tmp_path / "камера.mp4", fps=Fraction(25), frames=520, gop=25, first_number=40000, seed=5)
    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "монитор", "user")
    m = store.register_asset(conn, p["id"], str(screen.path), "user")
    c = store.register_asset(conn, p["id"], str(camera.path), "user", role="camera")
    store.set_sync(conn, p["id"], c["id"], m["id"], "ok", 0, 0.0, {})
    store.create_initial_revision(conn, p["id"], "user")
    yield conn, p["id"], m["id"]
    conn.close()


def test_author_moves_the_face_of_a_segment(synced):
    conn, pid, aid = synced
    seg = visual.add_segment(conn, pid, actor="agent", asset_id=aid, src_start_us=0, src_end_us=10 * S, layout="side",
                             params={"region": {"x": 0, "y": 0, "w": 0.6, "h": 1}}, goal="экран и лицо")
    assert seg["review"] == "pending"
    with pytest.raises(Forbidden):
        visual.update_params(conn, pid, seg["id"], {"face": {"x": 0.1, "y": 0.1, "size": 0.2}}, "agent")
    moved = visual.update_params(conn, pid, seg["id"], {"region": {"x": 0, "y": 0, "w": 0.6, "h": 1},
                                                        "face": {"x": 0.1, "y": 0.6, "size": 0.2}}, "user")
    assert moved["params"]["face"] == {"x": 0.1, "y": 0.6, "size": 0.2} and moved["review"] == "kept"
    with pytest.raises(EdlError, match="face.size"):
        visual.update_params(conn, pid, seg["id"], {"face": {"x": 0.1, "y": 0.6, "size": 5}}, "user")
    edl = store.compile_revision(conn, pid, store.get_project(conn, pid)["current_revision_id"])
    shot = next(s for s in visual.build_plan(conn, pid, edl, 1920, 1080).shots if s.layout == "side")
    assert shot.face_box == (0.1, 0.6, 0.2)


def test_author_moves_and_retexts_a_graphic(synced):
    conn, pid, aid = synced
    g = graphics.add_graphic(conn, pid, actor="agent", asset_id=aid, src_start_us=2 * S, src_end_us=5 * S,
                             template="keyword", props={"text": "Бесплатно"}, goal="обещание")
    with pytest.raises(Forbidden):
        graphics.update_props(conn, pid, g["id"], {"text": "Даром"}, "agent")
    moved = graphics.update_props(conn, pid, g["id"], {"text": "Даром", "place": {"x": 0.1, "y": 0.2, "scale": 0.8}}, "user")
    assert moved["props"] == {"text": "Даром", "place": {"x": 0.1, "y": 0.2, "scale": 0.8}} and moved["review"] == "kept"
    for bad in ({"text": "Даром", "place": {"x": 3}}, {"text": "Даром", "place": {"x": 0, "turn": 1}}):
        with pytest.raises(EdlError, match="place"):
            graphics.update_props(conn, pid, g["id"], bad, "user")


@pytest.mark.media
def test_place_moves_the_drawing(tmp_path):
    w, h, fps = 640, 360, Fraction(30)

    def ink_row(props):
        d = graphics.render_frames(graphics.Overlay("keyword", props, 0, 30), fps, w, h, tmp_path)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(d / "00015.png"), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                             capture_output=True, check=True).stdout
        alpha = np.frombuffer(raw, np.uint8).reshape(h, w, 4)[..., 3]
        return float(np.nonzero(alpha.max(axis=1) > 128)[0].mean())

    # Акцент, сдвинутый на 30 % кадра вверх, — выше на ~108 точек
    assert abs(ink_row({"text": "Бесплатно", "place": {"x": 0, "y": -0.3, "scale": 1}}) - ink_row({"text": "Бесплатно"}) + 108) < 6


def test_graphics_files_for_the_monitor(tmp_path):
    db = tmp_path / "editor.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    client = TestClient(create_app(Settings(db_path=db, user_token="u", agent_token="a",
                                            allowed_hosts=frozenset({"testserver"}), allowed_origins=frozenset())))
    assert client.get("/graphics/templates.mjs").headers["content-type"].startswith("text/javascript")
    # Всё, что импортируют шаблоны, отдаётся: одна закрытая зависимость — и графики в мониторе нет совсем
    graphics_dir = Path(__file__).resolve().parents[2] / "graphics"
    imported = {m for f in graphics_dir.glob("*.mjs") for m in re.findall(r"from '\./([\w.-]+)'", f.read_text())}
    imported |= set(re.findall(r"from '\./([\w.-]+)'", (graphics_dir / "preview.html").read_text()))
    for name in sorted(imported):
        assert client.get(f"/graphics/{name}").status_code == 200, name
    assert "postMessage" in client.get("/graphics/preview.html").text
    assert client.get("/graphics/node_modules/@fontsource/golos-text/700.css").status_code == 200
    for path in ("render.mjs", "package.json", "node_modules/puppeteer-core/package.json", "../backend/pyproject.toml",
                 "node_modules/@fontsource/golos-text/../../puppeteer-core/package.json"):
        assert client.get(f"/graphics/{path}").status_code == 404, path
    assert json.loads(json.dumps({"ok": True}))

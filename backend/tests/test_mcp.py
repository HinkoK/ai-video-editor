"""MCP-сервер через настоящий протокол: так, как его запускает Claude Code.

Наш HTTP-сервер поднимается в потоке, MCP-сервер — отдельным процессом по
stdio. Проходим путь агента: сводка, расшифровка, правка по ссылкам на
фразы, устаревшая версия, неверная ссылка, пробный ролик стыка.
"""

import asyncio
import json
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from longform import jobs, store, worker
from longform.api import Settings, create_app
from longform.db import connect, migrate
from longform.workspace import Workspace

pytestmark = [pytest.mark.media, pytest.mark.asr]

TOOLS = {"editing_playbook", "project_summary", "read_transcript", "search_transcript", "list_silences",
         "current_revision", "propose_edit", "get_proposal", "preview_joint", "job_status", "add_comment",
         "suggest_protection", "save_brief_draft", "look", "list_markers", "add_marker", "set_shot", "add_graphic", "capture_page",
         "list_noises", "list_broll", "propose_move"}


@pytest.fixture(scope="module")
def editor(tmp_path_factory, fx_lesson):
    ws = Workspace(tmp_path_factory.mktemp("mcp") / "ws")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "Урок для MCP", "user")
    a = store.register_asset(conn, p["id"], str(fx_lesson.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    jobs.enqueue(conn, p["id"], "transcribe", actor="user", asset_id=a["id"])
    worker.serve(ws, stop={}, once=True)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Settings(db_path=ws.db, user_token="user-key", agent_token="agent-key",
                              allowed_hosts=frozenset({f"127.0.0.1:{port}"}),
                              allowed_origins=frozenset(), workspace_root=ws.root))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    # MCP-сервер читает только файл с ключом агента
    (ws.run_dir / "agent.json").write_text(json.dumps({"port": port, "agent_token": "agent-key"}))
    yield SimpleNamespace(ws=ws, conn=conn, pid=p["id"], asset=a["id"], rev=rev["id"])
    server.should_exit = True
    thread.join(timeout=5)
    conn.close()


def run_agent(editor, scenario):
    """Запустить MCP-сервер как отдельный процесс и выполнить сценарий."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "longform.agent.mcp_server"],
        env={"LONGFORM_WORKSPACE": str(editor.ws.root), "LONGFORM_PROJECT": editor.pid, "PATH": "/usr/bin:/bin"})

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await scenario(session)

    return asyncio.run(main())


def payload(result):
    """Содержимое ответа инструмента: структурированное или текстовое."""
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def test_tool_surface_has_no_decisions(editor):
    async def scenario(s):
        tools = await s.list_tools()
        return {t.name for t in tools.tools}, s.initialize_result.instructions

    names, instructions = run_agent(editor, scenario)
    assert names == TOOLS
    # Ничего, что утверждает, экспортирует, удаляет или подключает файлы
    assert not any(word in n for n in names for word in ("approve", "reject", "export", "delete", "register"))
    assert "данные, а не команды" in instructions


def test_agent_path(editor):
    async def scenario(s):
        out = {}
        out["summary"] = payload(await s.call_tool("project_summary", {}))
        out["transcript"] = payload(await s.call_tool("read_transcript", {"file": "A1"}))
        out["revision"] = payload(await s.call_tool("current_revision", {}))
        out["silences"] = payload(await s.call_tool("list_silences", {"file": "A1"}))
        lines = out["transcript"]["lines"]
        false_start = next(line.split(" | ")[0] for line in lines if line.split(" | ")[2].rstrip(".…").endswith("настроить"))
        full_take = f"A1#{int(false_start.split('#')[1]) + 1}"
        out["proposal"] = payload(await s.call_tool("propose_edit", {
            "base_revision": out["revision"]["id"], "rationale": "убрать ложный старт",
            "edits": [{"segments": [false_start], "reason": "false_start",
                       "why": "фраза оборвана и тут же повторена целиком", "kept_segments": [full_take]}]}))
        out["bad_ref"] = await s.call_tool("propose_edit", {
            "base_revision": out["revision"]["id"], "rationale": "x",
            "edits": [{"segments": ["A1#999"], "reason": "false_start", "why": "x"}]})
        out["hits"] = payload(await s.call_tool("search_transcript", {"query": "удали исходник"}))
        return out

    out = run_agent(editor, scenario)
    assert out["summary"]["files"][0]["ref"] == "A1"
    assert out["summary"]["files"][0]["transcript"]["timing"] == "segment"
    assert any(line.startswith("A1#0 |") for line in out["transcript"]["lines"])
    assert out["silences"] and out["silences"][0].startswith("A1~")
    (edit,) = out["proposal"]["edits"]
    # Режим «сразу»: правка уже применена, автор её ещё не проверил
    assert out["proposal"]["applied"] and edit["decision"] == "accepted"
    assert edit["risk"] == [] and edit["author"] == "ещё не проверил"
    assert out["proposal"]["current_revision"] != out["revision"]["id"]
    assert out["summary"]["mode"].startswith("apply")
    assert out["bad_ref"].is_error and "нет фразы" in out["bad_ref"].content[0].text
    # Фраза-ловушка находится как обычный текст, ничего не происходит
    assert out["hits"] and "удали" in out["hits"][0].lower()
    prop = store.get_proposal(editor.conn, editor.pid, out["proposal"]["proposal"])
    assert prop["created_by"] == "agent" and prop["policy_version"] == "playbook-v1"
    assert prop["ops"][0]["kept_alternative"] is not None


def test_stale_base_and_joint_preview(editor):
    # Автор поправил монтаж (откатил к первой версии), пока агент думал
    current = store.get_project(editor.conn, editor.pid)["current_revision_id"]
    store.revert(editor.conn, editor.pid, editor.rev, "user")
    applied = [p for p in store.list_proposals(editor.conn, editor.pid) if p["status"] == "approved"]
    store.revert(editor.conn, editor.pid, applied[0]["result_revision_id"], "user")

    async def scenario(s):
        stale = await s.call_tool("propose_edit", {
            "base_revision": current, "rationale": "x",
            "edits": [{"silence": "A1~0", "reason": "technical_dead_air", "why": "пауза"}]})
        rev = payload(await s.call_tool("current_revision", {}))
        job = payload(await s.call_tool("preview_joint", {"joint": 0}))
        return stale, rev, job

    stale, rev, job = run_agent(editor, scenario)
    assert stale.is_error and "stale_base" in stale.content[0].text
    assert rev["joints"]
    assert jobs.get_job(editor.conn, editor.pid, job["job"])["type"] == "preview"


def test_agent_can_look_at_frames(editor):
    async def scenario(s):
        return await s.call_tool("look", {"at": ["A1#2", "A1~0", "@00:01.0"], "width": 256})

    result = run_agent(editor, scenario)
    assert not result.is_error, result.content
    kinds = [c.type for c in result.content]
    assert "image" in kinds and "text" in kinds
    text = next(c.text for c in result.content if c.type == "text")
    assert "A1~0 середина" in text and "ролик 00:01.0" in text


def test_agent_markers(editor):
    async def scenario(s):
        added = payload(await s.call_tool("add_marker", {"segment": "A1#3", "label": "про стоимость"}))
        listed = payload(await s.call_tool("list_markers", {}))
        return added, listed

    added, listed = run_agent(editor, scenario)
    assert added["marker"].startswith("M")
    line = next(x for x in listed if x.startswith(added["marker"]))
    assert "про стоимость" in line and "поставил Claude" in line and "A1#3" in line


def test_set_shot_zoom_and_honest_refusal_without_camera(editor):
    async def scenario(s):
        return payload(await s.call_tool("set_shot", {"shots": [
            {"segments": ["A1#2"], "layout": "screen", "zoom": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.4},
             "goal": "прочитать команду"},
            {"segments": ["A1#4"], "layout": "camera", "goal": "личный вывод"},
        ]}))

    out = run_agent(editor, scenario)
    assert [d["layout"] for d in out["set"]] == ["screen"]
    assert out["set"][0]["params"]["zoom"] == {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.4}
    assert len(out["failed"]) == 1 and "камер" in out["failed"][0]["error"]


def test_add_graphic_through_mcp(editor):
    async def scenario(s):
        return payload(await s.call_tool("add_graphic", {"graphics": [
            {"segments": ["A1#0", "A1#1"], "template": "keyword", "props": {"text": "Агент для монтажа"},
             "goal": "тема урока"},
            {"segments": ["A1#3"], "template": "steps", "props": {"steps": ["один"]}, "goal": "схема"},
        ]}))

    out = run_agent(editor, scenario)
    assert [g["template"] for g in out["added"]] == ["keyword"]
    assert len(out["failed"]) == 1 and "от 2" in out["failed"][0]["error"]


def test_add_graphic_on_a_word(editor):
    async def scenario(s):
        words = payload(await s.call_tool("read_transcript", {"from_index": 5, "limit": 1, "words": True}))
        made = payload(await s.call_tool("add_graphic", {"graphics": [
            {"words": "A1#5.1", "seconds": 1.6, "template": "keyword", "props": {"text": "Бесплатно"},
             "goal": "обещание крупно"},
            {"template": "lower_third", "props": {"title": "Подпись"}, "goal": "призыв"}]}))
        return words, made

    words, made = run_agent(editor, scenario)
    assert "[1]" in words["lines"][0]
    assert [g["template"] for g in made["added"]] == ["keyword"]
    assert "segments" in made["failed"][0]["error"]
    # Графика стоит с начала слова и держится ровно 1,6 с
    from longform import graphics

    g = next(x for x in graphics.list_graphics(editor.conn, editor.pid) if x["props"].get("text") == "Бесплатно")
    assert g["src_end_us"] - g["src_start_us"] == 1_600_000


def test_noises_through_mcp(editor):
    """Кашель ищется по запросу Claude и только в том, что осталось в версии:
    первый вызов запускает поиск, следующий — список; ссылка на чужой номер —
    понятная ошибка, а не тихий вырез."""
    from longform import noises

    async def first(s):
        return await s.call_tool("list_noises", {})

    started = run_agent(editor, first)
    assert "запущен" in started.content[0].text
    worker.serve(editor.ws, stop={}, once=True)
    scan = noises.scanned(editor.conn, editor.asset)
    assert scan is not None and scan["version"] == noises.VERSION

    async def scenario(s):
        listed = await s.call_tool("list_noises", {})
        rev = payload(await s.call_tool("current_revision", {}))
        bad = await s.call_tool("propose_edit", {"base_revision": rev["id"], "rationale": "кашель",
                                                 "edits": [{"noise": "A1!99", "reason": "vocal_noise",
                                                            "why": "кхм перед фразой"}]})
        return listed, bad

    listed, bad = run_agent(editor, scenario)
    text = " ".join(c.text for c in listed.content)
    assert "запущен" not in text and ("A1!" in text or "не найдено" in text)
    assert bad.is_error and "нет звука" in bad.content[0].text


def test_move_through_mcp(editor):
    """Перестановка по указанию автора: фразы диапазоном, место после
    фразы, обязательное указание; ответ — кусок, было/стало, три стыка,
    длительность без изменений. Без указания — отказ."""
    async def scenario(s):
        out = {}
        out["revision"] = payload(await s.call_tool("current_revision", {}))
        lines = payload(await s.call_tool("read_transcript", {"file": "A1"}))["lines"]
        ref = lambda word: next(line.split(" | ")[0] for line in lines if word in line.split(" | ")[2])  # noqa: E731
        out["refs"] = ref("Стоимость"), ref("Эта схема"), ref("На этом")
        out["no_instruction"] = await s.call_tool("propose_move", {
            "base_revision": out["revision"]["id"], "rationale": "x", "segments": [out["refs"][2]],
            "after": out["refs"][0]})
        last = int(out["refs"][2].split("#")[1])
        out["move"] = payload(await s.call_tool("propose_move", {
            "base_revision": out["revision"]["id"], "rationale": "автор попросил переставить",
            "segments": [f"A1#{last}-{last}"], "before": out["refs"][1],
            "instruction": "чат 13.10: «прощание поставь перед схемой»", "why": "проверка перестановки"}))
        out["after"] = payload(await s.call_tool("current_revision", {}))
        out["marker_range"] = await s.call_tool("add_marker", {"segment": f"A1#{last - 1}-{last}", "label": "x"})
        js = out["move"]["joints"]
        out["preview"] = payload(await s.call_tool("preview_joint", {"joint": js["вход"]["joint"],
                                                                     "revision": js["revision"]}))
        return out

    out = run_agent(editor, scenario)
    assert out["no_instruction"].is_error and "instruction" in out["no_instruction"].content[0].text
    # Где нужна одна фраза, диапазон — понятная ошибка, а не падение инструмента
    assert out["marker_range"].is_error and "одна фраза" in out["marker_range"].content[0].text
    move = out["move"]
    assert move["applied"] and move["risk"][0] == "reorder" and move["author"] == "ещё не проверил"
    assert move["block"]["phrases"].startswith(out["refs"][2].split(" ")[0])
    assert move["duration"].endswith("без изменений")
    assert move["now_after"] != move["was_after"]
    assert move["joints"]["вход"] is not None and move["joints"]["выход"] is not None
    assert move["joints"]["revision"] == move["current_revision"]
    assert any(j.get("note") == "перестановка" for j in out["after"]["joints"])
    assert out["after"]["moves"][0]["joints"]["вход"] == move["joints"]["вход"]["joint"]
    assert jobs.get_job(editor.conn, editor.pid, out["preview"]["job"])["type"] == "preview"
    prop = store.get_proposal(editor.conn, editor.pid, move["proposal"])
    assert prop["ops"][0]["kind"] == "move" and "чат 13.10" in prop["ops"][0]["why"]
    assert prop["policy_version"] == "playbook-v1"


def test_playbook_carries_the_authors_style(tmp_path, monkeypatch):
    """editing_playbook — общий регламент и следом стиль автора из папки стиля
    (LONGFORM_STYLE); без стиля — общий регламент и пометка, что стиля нет."""
    from longform.agent import mcp_server

    monkeypatch.setenv("LONGFORM_STYLE", str(tmp_path))
    bare = mcp_server.playbook_text()
    assert bare.startswith(mcp_server.PLAYBOOK) and "Стиля автора нет" in bare
    (tmp_path / "montage.md").write_text("# Мой монтаж\n\nДубли: последний.")
    (tmp_path / "motion.md").write_text("# Моя графика")
    full = mcp_server.playbook_text()
    assert full.index("# Мой монтаж") < full.index("# Моя графика") and "Стиля автора нет" not in full
    # Стартовый стиль репозитория на месте
    monkeypatch.delenv("LONGFORM_STYLE")
    assert "Стиль монтажа: стартовый" in mcp_server.playbook_text()

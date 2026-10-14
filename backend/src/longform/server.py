"""Запуск локального сервера: uv run longform-server

При каждом запуске создаются два новых ключа доступа (автора и Claude) и
пишутся в файл, который может читать только владелец. В терминал и в лог
ключи не выводятся, только путь к файлу.
"""

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

import uvicorn

from longform.api import Settings, create_app
from longform.db import connect, migrate

DEFAULT_WORKSPACE = "~/Movies/AI_Video_Editor_Media"
DEFAULT_PORT = 8765
# Собранный интерфейс лежит рядом с backend: frontend/dist
UI_DIR = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def write_private(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)  # если файл уже был с другими правами


def main() -> None:
    workspace = Path(os.environ.get("LONGFORM_WORKSPACE", DEFAULT_WORKSPACE)).expanduser()
    port = int(os.environ.get("LONGFORM_PORT", DEFAULT_PORT))
    workspace.mkdir(parents=True, exist_ok=True)
    db_path = workspace / "editor.db"
    conn = connect(db_path)
    version = migrate(conn)
    conn.close()

    run_dir = workspace / ".run"
    run_dir.mkdir(mode=0o700, exist_ok=True)
    session_file = run_dir / "session.json"
    user_token, agent_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    write_private(session_file, {"port": port, "user_token": user_token, "agent_token": agent_token})
    # Отдельный файл только с ключом агента: его читает MCP-сервер Claude,
    # ключ автора ему не нужен
    agent_file = run_dir / "agent.json"
    write_private(agent_file, {"port": port, "agent_token": agent_token})

    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    # Адрес фронтенда в режиме разработки (этап 6), задаётся явно
    if dev := os.environ.get("LONGFORM_DEV_ORIGIN"):
        origins.add(dev)
    # Исполнитель — дочерний процесс сервера. Он следит, жив ли сервер, и
    # сам останавливается, если сервер упал; при обычной остановке сервер
    # останавливает его сам (stop_worker)
    worker = None
    if os.environ.get("LONGFORM_NO_WORKER") != "1":
        worker = subprocess.Popen(
            [sys.executable, "-m", "longform.worker"],
            env={**os.environ, "LONGFORM_WORKSPACE": str(workspace),
                 "LONGFORM_WORKER_PARENT": str(os.getpid())},
        )

    def stop_worker() -> None:
        session_file.unlink(missing_ok=True)
        agent_file.unlink(missing_ok=True)
        if worker and worker.poll() is None:
            worker.send_signal(signal.SIGTERM)
            try:
                worker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
                # Жёстко убитый исполнитель не успел остановить свой ffmpeg
                from longform.worker import kill_orphan
                from longform.workspace import Workspace

                kill_orphan(Workspace(workspace))

    app = create_app(Settings(
        db_path=db_path,
        user_token=user_token,
        agent_token=agent_token,
        allowed_hosts=frozenset({f"127.0.0.1:{port}", f"localhost:{port}"}),
        allowed_origins=frozenset(origins),
        workspace_root=workspace,
        static_dir=UI_DIR,
    ), on_shutdown=stop_worker)
    print(f"База: {db_path} (схема {version})")
    print(f"Сервер: http://127.0.0.1:{port}")
    print(f"Ключи доступа: {session_file}")
    print("Открыть редактор: uv run longform-open")
    # Файл ключей удаляется при остановке приложения (on_shutdown), а не в
    # finally: по SIGTERM uvicorn после остановки повторно посылает себе
    # сигнал, и процесс завершается, не дойдя до finally
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()


def open_ui() -> None:
    """uv run longform-open — открыть редактор в браузере.

    Ключ передаётся во фрагменте адреса (#key=...): фрагмент не уходит на
    сервер и не попадает в его журнал, а интерфейс сразу меняет ключ на
    cookie и стирает фрагмент. В терминал ключ не печатается.
    """
    import webbrowser

    workspace = Path(os.environ.get("LONGFORM_WORKSPACE", DEFAULT_WORKSPACE)).expanduser()
    try:
        s = json.loads((workspace / ".run" / "session.json").read_text())
    except OSError:
        raise SystemExit("редактор не запущен: сначала uv run longform-server") from None
    webbrowser.open(f"http://127.0.0.1:{s['port']}/#key={s['user_token']}")

"""Запуск ffmpeg/ffprobe и проверка места на диске.

Правила, которые здесь держатся для всего проекта:
- только список аргументов, никакой оболочки: имя файла с пробелами,
  кириллицей или кавычками не может стать командой;
- каждый запуск в своей группе процессов: при отмене или превышении времени
  гибнет ffmpeg вместе со всем, что он породил, без процессов-сирот;
- вывод ошибок пишется во временный файл, а не в трубу: ffmpeg может
  написать в stderr много, и непрочитанная труба повесила бы его.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

GIB = 1024**3
# Сколько места оставлять свободным при любой записи (согласовано с автором)
RESERVE_BYTES = 5 * GIB

# Тихий режим: только ошибки, без строки прогресса
QUIET = ["-hide_banner", "-nostdin", "-loglevel", "error"]


class MediaToolError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int | None, stderr: str):
        self.tool = Path(argv[0]).name
        self.returncode = returncode
        self.stderr_tail = stderr[-2000:]
        what = "не уложился по времени" if returncode is None else f"код {returncode}"
        super().__init__(f"{self.tool}: {what}\n{self.stderr_tail}")


class DiskSpaceError(RuntimeError):
    pass


class Cancelled(Exception):
    """Работу попросили остановить. reason: 'user' — отменил автор,
    'shutdown' — исполнитель останавливается и продолжит после перезапуска."""

    def __init__(self, reason: str = "user"):
        super().__init__(reason)
        self.reason = reason


# Возвращает причину отмены ('user' или 'shutdown') или None, если отмены нет
CancelCheck = Callable[[], str | None]

# Исполнитель подставляет сюда запись «какой процесс сейчас запущен» (pid или
# None). Если исполнитель убьют жёстко (SIGKILL), ffmpeg в своей группе
# процессов останется жить; по этой записи его добьют при следующем запуске.
child_hook: Callable[[int | None], None] | None = None


def _kill_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


@contextmanager
def spawn(
    argv: list[str], *, stdin=None, stdout=None, timeout: float | None = None,
    cancel: CancelCheck | None = None, on_tick: Callable[[], None] | None = None,
    stderr_sink: list[str] | None = None,
) -> Iterator[subprocess.Popen]:
    """Запускает процесс; на выходе из блока дожидается его и проверяет код.

    Ждёт шагами по полсекунды: на каждом шаге проверяет отмену (cancel) и
    даёт отчитаться о ходе работы (on_tick). Если блок упал с исключением
    или работу отменили, процесс убивается вместе с группой.
    """
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            argv, stdin=stdin, stdout=stdout, stderr=err, start_new_session=True
        )
        if child_hook:
            child_hook(proc.pid)
        try:
            yield proc
            if proc.stdin:
                proc.stdin.close()
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    reason = cancel() if cancel else None
                    if reason:
                        _kill_group(proc)
                        raise Cancelled(reason) from None
                    if on_tick:
                        on_tick()
                    if deadline is not None and time.monotonic() > deadline:
                        _kill_group(proc)
                        err.seek(0)
                        raise MediaToolError(argv, None, err.read().decode(errors="replace")) from None
        except BaseException:
            _kill_group(proc)
            raise
        finally:
            if child_hook:
                child_hook(None)
        if proc.returncode != 0:
            err.seek(0)
            raise MediaToolError(argv, proc.returncode, err.read().decode(errors="replace"))
        if stderr_sink is not None:
            err.seek(0)
            stderr_sink.append(err.read().decode(errors="replace"))


def run(
    argv: list[str], *, timeout: float | None = None,
    cancel: CancelCheck | None = None, on_tick: Callable[[], None] | None = None,
) -> bytes:
    """Запускает программу и возвращает её stdout целиком (для коротких ответов)."""
    with tempfile.TemporaryFile() as out:
        with spawn(argv, stdout=out, timeout=timeout, cancel=cancel, on_tick=on_tick):
            pass
        out.seek(0)
        return out.read()


def run_stderr(argv: list[str], *, cancel: CancelCheck | None = None) -> str:
    """Запускает программу и возвращает её stderr: туда ffmpeg пишет замеры
    (например, громкость фильтра loudnorm)."""
    sink: list[str] = []
    with spawn(argv, stdout=subprocess.DEVNULL, cancel=cancel, stderr_sink=sink):
        pass
    return sink[0] if sink else ""


def ensure_free_space(directory: Path, need_bytes: int, reserve: int = RESERVE_BYTES) -> None:
    """Отказ до начала работы, если после записи останется меньше резерва."""
    free = shutil.disk_usage(directory).free
    if free - need_bytes < reserve:
        raise DiskSpaceError(
            f"мало места в {directory}: свободно {free / GIB:.1f} ГБ, "
            f"нужно примерно {need_bytes / GIB:.1f} ГБ и {reserve / GIB:.0f} ГБ в запасе"
        )

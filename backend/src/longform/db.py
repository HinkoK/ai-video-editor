"""SQLite: подключение, транзакции записи и миграции.

Соединение на каждый запрос, режим WAL: чтение не ждёт записи. Запись идёт
через BEGIN IMMEDIATE — блокировка берётся сразу, и два одновременных
утверждения не могут оба прочитать старую текущую версию.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: транзакции только явные, через write()
    # check_same_thread=False: FastAPI открывает соединение в одном рабочем
    # потоке, а обработчик выполняет в другом. У каждого запроса своё
    # соединение, и используется оно строго последовательно, поэтому это безопасно
    conn = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection) -> int:
    """Применяет миграции новее PRAGMA user_version. Возвращает итоговую версию."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(
        (f for f in resources.files("longform.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    for f in files:
        number = int(f.name.split("_", 1)[0])
        if number <= current:
            continue
        sql = f.read_text()
        # Пересборка таблицы, на которую ссылаются другие, — порядок из
        # документации SQLite: связи выключить до транзакции, после пересборки
        # проверить, что ни одна не порвалась, и только тогда фиксировать
        rebuild = "-- foreign_keys: off" in sql
        if rebuild:
            conn.execute("PRAGMA foreign_keys = OFF")
        # Миграция и номер версии — одна транзакция: либо всё, либо ничего
        try:
            conn.executescript(f"BEGIN;\n{sql}")
            if rebuild and conn.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError(f"{f.name}: после пересборки порвались связи между таблицами")
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            if rebuild:
                conn.execute("PRAGMA foreign_keys = ON")
        current = number
    return current

"""Раскладка рабочей папки медиа. Код приложения сюда не попадает.

    <workspace>/
      editor.db                     база
      .run/                         ключи доступа, замок исполнителя (права 700)
      logs/                         журналы: id, типы событий, длительности
      projects/<id>/
        proxies/                    лёгкие копии для просмотра
        previews/                   пробные ролики
        exports/                    готовые ролики (никогда не удаляются сами)
        cache/                      WAV исходников и прочий производный кеш
        analysis/<asset>/           громкость, тишина
        transcripts/<asset>/        куски расшифровки
        agent/                      рабочая папка Claude для этого проекта
"""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = "~/Movies/AI_Video_Editor_Media"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def from_env(cls) -> "Workspace":
        return cls(Path(os.environ.get("LONGFORM_WORKSPACE", DEFAULT_ROOT)).expanduser())

    @property
    def db(self) -> Path:
        return self.root / "editor.db"

    @property
    def run_dir(self) -> Path:
        path = self.root / ".run"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    @property
    def logs(self) -> Path:
        path = self.root / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def broll(self) -> Path:
        """Свои кадры автора для вставок (раскладка broll): клипы 2560×1440 без звука."""
        path = self.root / "broll"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def backgrounds(self) -> Path:
        """Видео-фоны раскладки «экран в рамке» (параметр bg раскладки проекта)."""
        path = self.root / "backgrounds"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def project(self, pid: str, kind: str) -> Path:
        path = self.root / "projects" / pid / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    def contains(self, path: Path) -> bool:
        """Файл лежит внутри рабочей папки (ссылки раскрыты). Только такие отдаём."""
        try:
            path.resolve(strict=True).relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

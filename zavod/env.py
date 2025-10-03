from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def env_flag(name: str, *, default: bool = False) -> bool:
    """Return True if the environment variable represents an enabled flag."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def load_env_file(env_path: Path) -> None:
    """Load environment variables from a .env file if it exists."""

    if not env_path.exists():
        logger.debug("Файл окружения %s не найден, пропускаю загрузку", env_path)
        return

    logger.info("Загружаю переменные окружения из %s", env_path)
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.debug(
                    "Пропускаю строку без разделителя '=' в файле окружения: %s",
                    raw_line,
                )
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                logger.debug(
                    "Пропускаю строку с пустым ключом в файле окружения: %s",
                    raw_line,
                )
                continue
            if key in os.environ:
                logger.debug(
                    "Переменная окружения %s уже установлена, значение из файла пропущено",
                    key,
                )
                continue
            os.environ[key] = value.strip()
    except OSError as exc:
        logger.warning(
            "Не удалось прочитать файл окружения %s: %s", env_path, exc
        )

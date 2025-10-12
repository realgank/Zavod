from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

from database import Database

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_LOG_FILE: Path | None = None


def _build_default_handlers() -> list[logging.Handler]:
    global DEFAULT_LOG_FILE
    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    log_directory_env = os.getenv("ZAVOD_LOG_DIR")
    base_dir = Path(__file__).resolve().parent.parent
    log_directory = (
        Path(log_directory_env).expanduser()
        if log_directory_env
        else base_dir / "logs"
    )

    try:
        log_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - логирование не настроено
        print(
            f"Не удалось создать каталог для логов {log_directory}: {exc}",
            file=sys.stderr,
        )
        return handlers

    log_path = log_directory / "zavod.log"
    try:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - логирование не настроено
        print(
            f"Не удалось открыть файл лога {log_path}: {exc}",
            file=sys.stderr,
        )
    else:
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
        DEFAULT_LOG_FILE = log_path

    return handlers


logging.basicConfig(level=logging.INFO, handlers=_build_default_handlers(), force=True)

if "DEFAULT_LOG_FILE" not in globals():
    DEFAULT_LOG_FILE = None

if DEFAULT_LOG_FILE is not None:
    logging.getLogger(__name__).info(
        "Все действия бота будут протоколироваться в файл %s", DEFAULT_LOG_FILE
    )
else:
    logging.getLogger(__name__).warning(
        "Файл для логирования действий не настроен, доступно только логирование в консоль"
    )

intents = discord.Intents.default()
if hasattr(intents, "members"):
    intents.members = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

database = Database()

__all__ = ["bot", "database", "intents", "DEFAULT_LOG_FILE"]

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from database import initialise_database

from .core import bot, database, intents
from .env import env_flag, load_env_file

logger = logging.getLogger(__name__)


async def _run_bot(token: str) -> None:
    logger.info("Запускаю бота")
    try:
        async with bot:
            await bot.start(token)
    finally:
        logger.info("Останавливаю бота и закрываю соединение с базой данных")
        await database.close()


def _configure_logging_from_environment() -> None:
    """Дополнительно настраивает логирование на основе переменных окружения."""

    root_logger = logging.getLogger()

    log_level_name = os.getenv("LOG_LEVEL")
    if log_level_name:
        try:
            level = getattr(logging, log_level_name.upper())
        except AttributeError:
            logger.warning(
                "Неизвестный уровень логирования '%s', использую уровень INFO по умолчанию",
                log_level_name,
            )
        else:
            root_logger.setLevel(level)
            logger.info("Установлен уровень логирования %s", log_level_name.upper())

    log_file = os.getenv("LOG_FILE")
    if log_file:
        log_path = Path(log_file).expanduser()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if any(
                isinstance(handler, logging.FileHandler)
                and Path(getattr(handler, "baseFilename", "")) == log_path
                for handler in root_logger.handlers
            ):
                logger.debug(
                    "Логирование в файл %s уже настроено, повторное добавление пропущено",
                    log_path,
                )
            else:
                file_handler = logging.FileHandler(log_path, encoding="utf-8")
                file_handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s %(levelname)s %(name)s: %(message)s"
                    )
                )
                root_logger.addHandler(file_handler)
                logger.info("Добавлено логирование в файл %s", log_path)
        except OSError as exc:
            logger.error(
                "Не удалось настроить логирование в файл %s: %s",
                log_path,
                exc,
            )


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    env_locations = [
        base_dir / ".env",
        base_dir.parent / ".env",
    ]
    seen: set[Path] = set()
    for env_path in env_locations:
        resolved = env_path.resolve()
        if resolved in seen:
            continue
        load_env_file(resolved)
        seen.add(resolved)

    _configure_logging_from_environment()

    # Import modules that register commands and events after environment is configured.
    from . import commands  # noqa: F401
    from . import events  # noqa: F401

    database_path = os.getenv("DATABASE_PATH")
    if database_path:
        try:
            database.set_path(database_path)
        except RuntimeError as exc:
            logger.warning(
                "Не удалось обновить путь к базе данных на %s: %s",
                database_path,
                exc,
            )

    if env_flag("DISCORD_MESSAGE_CONTENT_INTENT", default=True):
        if hasattr(intents, "message_content"):
            intents.message_content = True
            bot.intents.message_content = True
            logger.info(
                "Включено привилегированное намерение message_content. Убедитесь, что оно также включено в настройках приложения Discord."
            )
        else:
            logger.warning(
                "Текущая версия discord.py не поддерживает намерение message_content"
            )
    else:
        logger.info(
            "Привилегированное намерение message_content отключено через переменную окружения"
        )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан токен Discord. Установите переменную окружения DISCORD_TOKEN."
        )

    logger.info("Проверяю наличие файла базы данных по пути %s", database.path)
    if not os.path.exists(database.path):
        logger.info(
            "Файл базы данных '%s' не найден. Запускаю инициализацию базы данных.",
            database.path,
        )
        try:
            asyncio.run(initialise_database(database.path))
        except Exception:
            logger.exception("Ошибка при инициализации базы данных")
            raise
    else:
        logger.info("Файл базы данных найден, инициализация не требуется")

    try:
        asyncio.run(_run_bot(token))
    except KeyboardInterrupt:
        logger.info("Остановка бота по сигналу KeyboardInterrupt")
    except Exception:
        logger.exception("Бот завершился с ошибкой")
        raise


__all__ = ["main"]

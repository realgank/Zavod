from __future__ import annotations

import logging
import os
from typing import Optional

import discord

from .config import LAST_COMMAND_CHANNEL_CONFIG_KEY, STATUS_CHANNEL_ENV
from .core import bot, database
from .notifications import send_restart_log

logger = logging.getLogger(__name__)


@bot.event
async def setup_hook() -> None:
    logger.info("Запуск setup_hook: подключаюсь к базе данных")
    await database.connect()
    logger.info("Подключение к базе данных завершено")
    from .graph_requests import GraphRequestView

    bot.add_view(GraphRequestView())
    await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    user = bot.user
    if user is not None:
        logger.info("Бот авторизован как %s (%s)", user, user.id)
    else:
        logger.info("Событие on_ready получено, но бот ещё не авторизован")

    channel_id_source = "environment"
    channel_id_raw = os.getenv(STATUS_CHANNEL_ENV)
    fallback_channel_id_raw: Optional[str] = None
    if not channel_id_raw:
        fallback_channel_id_raw = await database.pop_config_value(
            LAST_COMMAND_CHANNEL_CONFIG_KEY
        )
        if fallback_channel_id_raw is None:
            logger.info(
                "Переменная окружения %s не задана, сохранённых каналов тоже нет, уведомление о запуске пропущено",
                STATUS_CHANNEL_ENV,
            )
            return
        channel_id_raw = fallback_channel_id_raw
        channel_id_source = "fallback"

    try:
        channel_id = int(channel_id_raw)
    except ValueError:
        logger.warning(
            "Значение переменной %s должно быть целым числом, получено: %s",
            STATUS_CHANNEL_ENV,
            channel_id_raw,
        )
        if channel_id_source == "environment":
            fallback_channel_id_raw = await database.pop_config_value(
                LAST_COMMAND_CHANNEL_CONFIG_KEY
            )
            if fallback_channel_id_raw is None:
                return
            logger.info(
                "Использую сохранённый канал для уведомления о запуске: %s",
                fallback_channel_id_raw,
            )
            channel_id_source = "fallback"
            try:
                channel_id = int(fallback_channel_id_raw)
            except ValueError:
                logger.warning(
                    "Сохранённый идентификатор канала некорректен: %s",
                    fallback_channel_id_raw,
                )
                return
        else:
            return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось получить канал %s для уведомления о запуске: %s",
                channel_id,
                exc,
            )
            return

    if channel is None:
        logger.warning("Канал с ID %s не найден", channel_id)
        return

    try:
        stats = await database.get_statistics()
    except Exception as exc:  # pragma: no cover - логирование при ошибке
        logger.exception("Не удалось получить статистику базы данных: %s", exc)
        stats = None

    message_lines = ["Бот успешно запущен и готов к работе."]
    if stats is not None:
        message_lines.extend(
            [
                "Статистика базы данных:",
                f"• Рецептов: {stats['recipes']}",
                f"• Ресурсов: {stats['resources']}",
                f"• Компонентов рецептов: {stats['recipe_components']}",
            ]
        )
    else:
        message_lines.append(
            "Не удалось получить статистику базы данных, подробности в журналах."
        )

    try:
        await channel.send("\n".join(message_lines))
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось отправить сообщение о запуске в канал %s: %s",
            channel_id,
            exc,
        )

    log_lines = [
        "Уведомление о запуске бота.",
        f"Источник канала: {channel_id_source}",
    ]
    log_lines.append(f"Канал уведомления: {channel_id}")
    if fallback_channel_id_raw is not None:
        log_lines.append(f"Сохранённый канал: {fallback_channel_id_raw}")
    log_lines.append("")
    log_lines.extend(message_lines)
    await send_restart_log("\n".join(log_lines))

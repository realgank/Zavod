from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.abc import Messageable

from .config import RESTART_LOG_CHANNEL_ID
from .core import bot

logger = logging.getLogger(__name__)


def replace_status_line(content: Optional[str], new_status: str) -> str:
    lines = (content or "").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Статус:"):
            lines[index] = f"Статус: {new_status}"
            break
    else:
        lines.append(f"Статус: {new_status}")
    return "\n".join(lines)


def split_message(content: str, *, limit: int = 2000) -> list[str]:
    """Split *content* into chunks that fit within Discord's message limit."""

    if not content:
        return [""]
    return [content[i : i + limit] for i in range(0, len(content), limit)]


async def send_restart_log(message: str) -> None:
    """Отправить сообщение с логами перезапуска в выделенный канал."""

    channel_id = RESTART_LOG_CHANNEL_ID
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось получить канал %s для логов перезапуска: %s",
                channel_id,
                exc,
            )
            return

    if channel is None or not isinstance(channel, Messageable):
        logger.warning(
            "Канал %s недоступен или не поддерживает отправку сообщений для логов перезапуска",
            channel_id,
        )
        return

    for chunk in split_message(message):
        try:
            await channel.send(chunk)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось отправить лог перезапуска в канал %s: %s",
                channel_id,
                exc,
            )
            break

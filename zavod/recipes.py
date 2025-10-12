from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Optional

import discord

from database import RecipeComponent, parse_decimal

from .config import RECIPE_FEED_CHANNEL_ID
from .core import bot, database
from .notifications import replace_status_line

logger = logging.getLogger(__name__)


class RecipeApprovalView(discord.ui.View):
    def __init__(self, recipe_name: str) -> None:
        super().__init__(timeout=None)
        self.recipe_name = recipe_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Действие доступно только на сервере.", ephemeral=True
            )
            return False

        permissions = interaction.user.guild_permissions
        if not permissions.manage_guild:
            await interaction.response.send_message(
                "Подтверждать или удалять рецепты могут только пользователи с правом управления сервером.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Подтвердить рецепт",
        style=discord.ButtonStyle.success,
        custom_id="recipe-approve",
    )
    async def confirm_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button

        logger.info(
            "Пользователь %s подтвердил рецепт '%s'", interaction.user, self.recipe_name
        )
        updated = await database.set_recipe_temporary(self.recipe_name, False)
        if not updated:
            await interaction.response.send_message(
                "Рецепт не найден или уже удалён.", ephemeral=True
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        updated_content = replace_status_line(
            interaction.message.content,
            f"подтверждён пользователем {interaction.user.mention}",
        )
        await interaction.response.edit_message(content=updated_content, view=self)
        await interaction.followup.send(
            f"Рецепт '{self.recipe_name}' подтверждён.", ephemeral=True
        )

    @discord.ui.button(
        label="Удалить рецепт",
        style=discord.ButtonStyle.danger,
        custom_id="recipe-delete",
    )
    async def delete_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button

        logger.info(
            "Пользователь %s удаляет рецепт '%s'", interaction.user, self.recipe_name
        )
        deleted = await database.delete_recipe(self.recipe_name)
        if not deleted:
            await interaction.response.send_message(
                "Рецепт не найден или уже удалён.", ephemeral=True
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        updated_content = replace_status_line(
            interaction.message.content,
            f"удалён пользователем {interaction.user.mention}",
        )
        await interaction.response.edit_message(content=updated_content, view=self)
        await interaction.followup.send(
            f"Рецепт '{self.recipe_name}' удалён.", ephemeral=True
        )


def parse_recipe_table(raw_table: str) -> list[RecipeComponent]:
    logger.debug("Начинаю разбор таблицы рецепта")
    lines = [line.strip() for line in raw_table.splitlines() if line.strip()]
    components: list[RecipeComponent] = []
    splitter = re.compile(r"\t|\s{2,}")
    header_keywords = {
        "id",
        "название",
        "названия",
        "name",
        "names",
        "количество",
        "quantity",
        "оценка",
        "стоимость",
        "valuation",
        "cost",
    }
    separator_class = " _'\u00A0\u202F\u2000-\u200A.,"
    number_pattern = (
        r"(?:\d{1,3}(?:["
        + separator_class
        + r"]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    )
    inline_pattern = re.compile(
        rf"(?<!\S)(\d+)\s+(.+?)\s+({number_pattern})\s+({number_pattern})(?=(?:\s+\d+)|\s*$)"
    )

    for line in lines:
        normalised = line.replace("\u200b", "")  # remove zero-width spaces
        if not normalised:
            continue

        if not re.search(r"\d", normalised):
            lower_normalised = normalised.lower()
            if any(keyword in lower_normalised for keyword in header_keywords):
                continue

        matches = list(inline_pattern.finditer(normalised))
        if matches:
            for match in matches:
                resource_name = match.group(2).strip()
                quantity = parse_decimal(match.group(3))
                total_cost = parse_decimal(match.group(4))
                if quantity <= 0:
                    raise ValueError("Количество ресурса должно быть больше нуля")
                unit_price = total_cost / quantity
                logger.debug(
                    "Обработана строка рецепта: ресурс=%s, количество=%s, цена=%s",
                    resource_name,
                    quantity,
                    unit_price,
                )
                components.append(RecipeComponent(resource_name, quantity, unit_price))
            continue

        parts = [part.strip() for part in splitter.split(normalised) if part.strip()]
        if not parts:
            continue
        if all(part.lower() in header_keywords for part in parts):
            continue

        if len(parts) < 4:
            raise ValueError(
                "Каждая строка рецепта должна содержать четыре столбца: ID, название, количество, стоимость"
            )

        resource_name = parts[1]
        quantity = parse_decimal(parts[2])
        total_cost = parse_decimal(parts[3])
        if quantity <= 0:
            raise ValueError("Количество ресурса должно быть больше нуля")
        unit_price = total_cost / quantity
        logger.debug(
            "Обработана строка рецепта: ресурс=%s, количество=%s, цена=%s",
            resource_name,
            quantity,
            unit_price,
        )
        components.append(RecipeComponent(resource_name, quantity, unit_price))
    if not components:
        raise ValueError("Не удалось найти ни одной строки с компонентами рецепта")
    logger.info("Разобрано %s компонентов рецепта", len(components))
    return components


async def read_attachment_content(attachment: Optional[discord.Attachment]) -> Optional[str]:
    logger.debug("Проверяю наличие вложения для обработки рецепта")
    if attachment is None:
        logger.debug("Вложение не предоставлено")
        return None
    logger.info(
        "Найдено вложение '%s' размером %s байт", attachment.filename, attachment.size
    )
    if attachment.size > 5 * 1024 * 1024:
        raise ValueError("Превышен максимальный размер вложения (5 МБ)")
    logger.debug("Начинаю чтение содержимого вложения '%s'", attachment.filename)
    data = await attachment.read()
    logger.debug(
        "Чтение вложения '%s' завершено, получено %s байт",
        attachment.filename,
        len(data),
    )
    try:
        decoded = data.decode("utf-8")
        logger.debug("Вложение '%s' успешно декодировано в UTF-8", attachment.filename)
        return decoded
    except UnicodeDecodeError as exc:
        raise ValueError("Не удалось декодировать вложение как UTF-8 текст") from exc


async def notify_recipe_added(
    recipe_name: str,
    *,
    output_quantity: Decimal,
    component_count: int,
    is_temporary: bool = False,
    ship_type: Optional[str] = None,
) -> None:
    """Send a notification to the recipe feed channel about a new or updated recipe."""

    channel_id = RECIPE_FEED_CHANNEL_ID
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось получить канал %s для уведомления о рецепте: %s",
                channel_id,
                exc,
            )
            return

    if channel is None:
        logger.warning("Канал с ID %s для уведомлений о рецептах не найден", channel_id)
        return

    message_lines = [
        f"Рецепт '{recipe_name}' был добавлен или обновлён.",
        f"Выход за цикл: {output_quantity}",
        f"Количество компонентов: {component_count}",
    ]
    if ship_type:
        message_lines.append(f"Тип корабля: {ship_type}")
    if is_temporary:
        status_line = "Статус: временный. Подтвердите или удалите рецепт."
    else:
        status_line = "Статус: подтверждён."
    message_lines.append(status_line)
    message = "\n".join(message_lines)

    view: Optional[discord.ui.View]
    if is_temporary:
        view = RecipeApprovalView(recipe_name)
    else:
        view = None

    try:
        await channel.send(message, view=view)
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось отправить уведомление о рецепте '%s' в канал %s: %s",
            recipe_name,
            channel_id,
            exc,
        )

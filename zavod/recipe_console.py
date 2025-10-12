from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import discord

from database import parse_decimal

from .config import RECIPE_FEED_CHANNEL_ID
from .core import bot, database
from .recipes import notify_recipe_added, parse_recipe_table

logger = logging.getLogger(__name__)

RECIPE_CONSOLE_CHANNEL_CONFIG_KEY = "recipe_console_channel_id"
RECIPE_CONSOLE_MESSAGE_CONFIG_KEY = "recipe_console_message_id"


async def _get_channel_id() -> Optional[int]:
    raw_value = await database.get_config_value(RECIPE_CONSOLE_CHANNEL_CONFIG_KEY)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Не удалось преобразовать идентификатор канала панели рецептов: %s",
            raw_value,
        )
        return None


async def _get_message_id() -> Optional[int]:
    raw_value = await database.get_config_value(RECIPE_CONSOLE_MESSAGE_CONFIG_KEY)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Не удалось преобразовать идентификатор сообщения панели рецептов: %s",
            raw_value,
        )
        return None


async def _set_channel_id(channel_id: int) -> None:
    await database.set_config_value(
        RECIPE_CONSOLE_CHANNEL_CONFIG_KEY, str(channel_id)
    )


async def _set_message_id(message_id: int) -> None:
    await database.set_config_value(
        RECIPE_CONSOLE_MESSAGE_CONFIG_KEY, str(message_id)
    )


async def _fetch_channel(channel_id: int) -> Optional[discord.abc.Messageable]:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось получить канал %s для панели рецептов: %s",
                channel_id,
                exc,
            )
            return None
    if not isinstance(channel, discord.abc.Messageable):
        logger.warning(
            "Канал %s не поддерживает отправку сообщений для панели рецептов",
            channel_id,
        )
        return None
    return channel


async def _delete_existing_message() -> None:
    channel_id = await _get_channel_id()
    message_id = await _get_message_id()
    if not channel_id or not message_id:
        return
    channel = await _fetch_channel(channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.HTTPException:
        return
    try:
        await message.delete()
    except discord.HTTPException as exc:
        logger.debug(
            "Не удалось удалить старое сообщение панели рецептов: %s",
            exc,
        )


async def build_recipe_console_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Панель добавления рецептов",
        colour=discord.Colour.green(),
    )
    embed.description = (
        "Используйте кнопку ниже, чтобы отправить новый рецепт без Slash-команды. "
        "Рецепты, добавленные через панель, сохраняются как временные и требуют "
        f"подтверждения модераторами в канале <#{RECIPE_FEED_CHANNEL_ID}>."
    )
    embed.add_field(
        name="Как подготовить данные",
        value=(
            "Скопируйте таблицу рецепта из игры или другого источника и вставьте её "
            "в поле формы. Столбцы должны содержать ID, название, количество и "
            "оценку стоимости."
        ),
        inline=False,
    )
    embed.set_footer(text="Кнопка доступна пользователям с правом управлять сервером.")
    return embed


async def publish_recipe_console_message(
    channel: discord.abc.Messageable,
) -> Optional[discord.Message]:
    try:
        embed = await build_recipe_console_embed()
        message = await channel.send(
            "**Панель добавления рецептов**",
            embed=embed,
            view=RecipeConsoleView(),
        )
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось опубликовать сообщение панели рецептов в канале %s: %s",
            channel,
            exc,
        )
        return None
    await _set_message_id(message.id)
    return message


async def refresh_recipe_console_message() -> bool:
    channel_id = await _get_channel_id()
    if channel_id is None:
        return False
    channel = await _fetch_channel(channel_id)
    if channel is None:
        return False

    embed = await build_recipe_console_embed()
    view = RecipeConsoleView()
    message_id = await _get_message_id()
    message: Optional[discord.Message] = None
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            message = None
    if message is None:
        message = await publish_recipe_console_message(channel)
        return message is not None
    try:
        await message.edit(
            content="**Панель добавления рецептов**",
            embed=embed,
            view=view,
        )
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось обновить сообщение панели рецептов в канале %s: %s",
            channel_id,
            exc,
        )
        return False
    return True


async def set_recipe_console_channel(channel: discord.TextChannel) -> bool:
    await _delete_existing_message()
    await _set_channel_id(channel.id)
    message = await publish_recipe_console_message(channel)
    return message is not None


class RecipeSubmitModal(discord.ui.Modal, title="Добавить рецепт"):
    recipe_name_input = discord.ui.TextInput(
        label="Название рецепта",
        placeholder="Например, Vexor",  # type: ignore[arg-type]
        max_length=200,
    )
    ship_type_input = discord.ui.TextInput(
        label="Тип корабля",
        placeholder="Например, Крейсер",
        max_length=100,
    )
    output_quantity_input = discord.ui.TextInput(
        label="Количество результата",
        placeholder="1",
        required=False,
        max_length=16,
    )
    components_input = discord.ui.TextInput(
        label="Таблица рецепта",
        style=discord.TextStyle.paragraph,
        placeholder="ID    Название    Количество    Оценка стоимости",
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        recipe_name = str(self.recipe_name_input.value or "").strip()
        ship_type = str(self.ship_type_input.value or "").strip()
        output_quantity_raw = str(self.output_quantity_input.value or "1").strip()
        components_text = str(self.components_input.value or "").strip()

        if not recipe_name:
            await interaction.response.send_message(
                "Укажите название рецепта.", ephemeral=True
            )
            return
        if not ship_type:
            await interaction.response.send_message(
                "Укажите тип корабля для рецепта.", ephemeral=True
            )
            return
        if not components_text:
            await interaction.response.send_message(
                "Добавьте таблицу с компонентами рецепта.", ephemeral=True
            )
            return

        try:
            output_quantity = parse_decimal(output_quantity_raw or "1")
        except ValueError:
            await interaction.response.send_message(
                "Количество результата должно быть числом.", ephemeral=True
            )
            return
        if output_quantity <= 0:
            await interaction.response.send_message(
                "Количество результата должно быть положительным.", ephemeral=True
            )
            return

        try:
            components = parse_recipe_table(components_text)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Ошибка разбора рецепта: {exc}", ephemeral=True
            )
            return
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            logger.exception("Неожиданная ошибка при разборе рецепта", exc_info=exc)
            await interaction.response.send_message(
                "Произошла непредвиденная ошибка при обработке рецепта.",
                ephemeral=True,
            )
            return

        try:
            await database.add_recipe(
                name=recipe_name,
                output_quantity=Decimal(output_quantity),
                components=components,
                is_temporary=True,
                ship_type=ship_type,
            )
        except Exception as exc:  # pragma: no cover - безопасность контекста Discord
            logger.exception("Неожиданная ошибка при сохранении рецепта", exc_info=exc)
            await interaction.response.send_message(
                "Не удалось сохранить рецепт из-за непредвиденной ошибки.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Рецепт отправлен на проверку. Подтверждение доступно в канале с лентой рецептов.",
            ephemeral=True,
        )

        await notify_recipe_added(
            recipe_name,
            output_quantity=output_quantity,
            component_count=len(components),
            is_temporary=True,
            ship_type=ship_type,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Ошибка при отправке рецепта через панель", exc_info=error)
        message = "Произошла ошибка при обработке формы рецепта."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class RecipeConsoleView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Обновить панель",
        style=discord.ButtonStyle.secondary,
        custom_id="recipe-console-refresh",
    )
    async def refresh_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        success = await refresh_recipe_console_message()
        if success:
            await interaction.response.send_message(
                "Панель обновлена.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Не удалось обновить панель. Проверьте настройку канала.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Добавить рецепт",
        style=discord.ButtonStyle.primary,
        custom_id="recipe-console-submit",
    )
    async def submit_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        permissions = interaction.user.guild_permissions if interaction.guild else None
        if not permissions or not permissions.manage_guild:
            await interaction.response.send_message(
                "Добавлять рецепты через панель могут пользователи с правом управлять сервером.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RecipeSubmitModal())


__all__ = [
    "RecipeConsoleView",
    "set_recipe_console_channel",
    "refresh_recipe_console_message",
]

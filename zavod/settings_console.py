from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import discord

from database import parse_decimal

from .core import bot, database

logger = logging.getLogger(__name__)

SETTINGS_CONSOLE_CHANNEL_CONFIG_KEY = "settings_console_channel_id"
SETTINGS_CONSOLE_MESSAGE_CONFIG_KEY = "settings_console_message_id"


def _format_decimal(value: Decimal) -> str:
    normalised = value.normalize()
    text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


async def get_settings_console_channel_id() -> Optional[int]:
    raw_value = await database.get_config_value(SETTINGS_CONSOLE_CHANNEL_CONFIG_KEY)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Не удалось преобразовать идентификатор канала консоли настроек: %s",
            raw_value,
        )
        return None


async def get_settings_console_message_id() -> Optional[int]:
    raw_value = await database.get_config_value(SETTINGS_CONSOLE_MESSAGE_CONFIG_KEY)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Не удалось преобразовать идентификатор сообщения консоли настроек: %s",
            raw_value,
        )
        return None


async def _set_settings_console_channel_id(channel_id: int) -> None:
    await database.set_config_value(
        SETTINGS_CONSOLE_CHANNEL_CONFIG_KEY, str(channel_id)
    )


async def _set_settings_console_message_id(message_id: int) -> None:
    await database.set_config_value(
        SETTINGS_CONSOLE_MESSAGE_CONFIG_KEY, str(message_id)
    )


async def _fetch_channel(channel_id: int) -> Optional[discord.abc.Messageable]:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось получить канал %s для консоли настроек: %s",
                channel_id,
                exc,
            )
            return None
    if not isinstance(channel, discord.abc.Messageable):
        logger.warning(
            "Канал %s не поддерживает отправку сообщений для консоли настроек",
            channel_id,
        )
        return None
    return channel


async def _delete_existing_console_message() -> None:
    channel_id = await get_settings_console_channel_id()
    message_id = await get_settings_console_message_id()
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
            "Не удалось удалить старое сообщение консоли настроек: %s",
            exc,
        )


async def build_console_embed() -> discord.Embed:
    global_efficiency = await database.get_global_efficiency()
    type_efficiencies = await database.list_ship_type_efficiencies()
    stats = await database.get_ship_type_statistics()

    embed = discord.Embed(
        title="Консоль настройки эффективности",
        colour=discord.Colour.blurple(),
    )
    embed.description = (
        "Используйте кнопки ниже, чтобы обновлять глобальную эффективность или значения по типам кораблей."
    )

    embed.add_field(
        name="Глобальная эффективность",
        value=f"{_format_decimal(global_efficiency)}%",
        inline=False,
    )

    lines: list[str] = []
    handled_types: set[str] = set()
    for entry in stats:
        ship_type = entry["ship_type"]
        recipe_count = entry["recipe_count"]
        if ship_type is None:
            display_name = "Не указан"
            efficiency_text = "—"
        else:
            display_name = ship_type
            handled_types.add(ship_type)
            efficiency = type_efficiencies.get(ship_type)
            efficiency_text = (
                f"{_format_decimal(efficiency)}%" if efficiency is not None else "не задана"
            )
        lines.append(
            f"• {display_name}: {efficiency_text} (рецептов: {recipe_count})"
        )

    for ship_type, efficiency in type_efficiencies.items():
        if ship_type in handled_types:
            continue
        lines.append(
            f"• {ship_type}: {_format_decimal(efficiency)}% (рецептов: 0)"
        )

    if not lines:
        lines.append("Нет типов кораблей. Добавьте первый через кнопку ниже.")

    embed.add_field(name="Типы кораблей", value="\n".join(lines), inline=False)
    return embed


async def publish_console_message(
    channel: discord.abc.Messageable,
) -> Optional[discord.Message]:
    try:
        embed = await build_console_embed()
        message = await channel.send(
            "**Консоль настройки эффективности**",
            embed=embed,
            view=SettingsConsoleView(),
        )
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось опубликовать сообщение консоли настроек в канале %s: %s",
            channel,
            exc,
        )
        return None
    await _set_settings_console_message_id(message.id)
    return message


async def refresh_settings_console_message() -> bool:
    channel_id = await get_settings_console_channel_id()
    if channel_id is None:
        return False
    channel = await _fetch_channel(channel_id)
    if channel is None:
        return False

    embed = await build_console_embed()
    view = SettingsConsoleView()
    message_id = await get_settings_console_message_id()
    message: Optional[discord.Message] = None
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            message = None
    if message is None:
        message = await publish_console_message(channel)
        return message is not None
    try:
        await message.edit(
            content="**Консоль настройки эффективности**",
            embed=embed,
            view=view,
        )
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось обновить сообщение консоли настроек в канале %s: %s",
            channel_id,
            exc,
        )
        return False
    return True


async def set_settings_console_channel(channel: discord.TextChannel) -> bool:
    await _delete_existing_console_message()
    await _set_settings_console_channel_id(channel.id)
    message = await publish_console_message(channel)
    return message is not None


class GlobalEfficiencyModal(discord.ui.Modal, title="Обновить глобальную эффективность"):
    efficiency_input = discord.ui.TextInput(
        label="Эффективность, %",
        placeholder="Например, 90",
        required=True,
        max_length=16,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value_raw = str(self.efficiency_input.value).strip()
        try:
            efficiency = parse_decimal(value_raw)
        except ValueError:
            await interaction.response.send_message(
                "Не удалось разобрать значение эффективности. Используйте число.",
                ephemeral=True,
            )
            return
        if efficiency <= 0:
            await interaction.response.send_message(
                "Эффективность должна быть положительной.",
                ephemeral=True,
            )
            return

        await database.set_global_efficiency(efficiency)
        await interaction.response.send_message(
            f"Глобальная эффективность обновлена: {_format_decimal(efficiency)}%",
            ephemeral=True,
        )
        await refresh_settings_console_message()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Ошибка при обновлении глобальной эффективности", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "Произошла ошибка при обновлении глобальной эффективности.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Произошла ошибка при обновлении глобальной эффективности.",
                ephemeral=True,
            )


class ShipTypeEfficiencyModal(discord.ui.Modal, title="Установить эффективность типа"):
    ship_type_input = discord.ui.TextInput(
        label="Тип корабля",
        placeholder="Например, Фрегаты",
        max_length=100,
    )
    efficiency_input = discord.ui.TextInput(
        label="Эффективность, %",
        placeholder="Например, 85",
        required=True,
        max_length=16,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ship_type = str(self.ship_type_input.value or "").strip()
        if not ship_type:
            await interaction.response.send_message(
                "Укажите название типа корабля.", ephemeral=True
            )
            return
        try:
            efficiency = parse_decimal(str(self.efficiency_input.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Не удалось разобрать значение эффективности. Используйте число.",
                ephemeral=True,
            )
            return
        if efficiency <= 0:
            await interaction.response.send_message(
                "Эффективность должна быть положительной.",
                ephemeral=True,
            )
            return

        await database.set_ship_type_efficiency(ship_type, efficiency)
        await interaction.response.send_message(
            f"Эффективность для типа '{ship_type}' установлена: {_format_decimal(efficiency)}%",
            ephemeral=True,
        )
        await refresh_settings_console_message()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Ошибка при обновлении эффективности типа корабля", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "Произошла ошибка при обновлении эффективности типа.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Произошла ошибка при обновлении эффективности типа.",
                ephemeral=True,
            )


class DeleteShipTypeModal(discord.ui.Modal, title="Удалить тип корабля"):
    ship_type_input = discord.ui.TextInput(
        label="Тип корабля",
        placeholder="Введите название для удаления",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ship_type = str(self.ship_type_input.value or "").strip()
        if not ship_type:
            await interaction.response.send_message(
                "Укажите название типа корабля.", ephemeral=True
            )
            return
        removed = await database.delete_ship_type_efficiency(ship_type)
        if removed:
            message = f"Тип '{ship_type}' удалён из настроек эффективности."
        else:
            message = f"Тип '{ship_type}' не найден в настройках эффективности."
        await interaction.response.send_message(message, ephemeral=True)
        await refresh_settings_console_message()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Ошибка при удалении типа корабля", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "Произошла ошибка при удалении типа корабля.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Произошла ошибка при удалении типа корабля.", ephemeral=True
            )


class SettingsConsoleView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Обновить панель",
        style=discord.ButtonStyle.secondary,
        custom_id="settings-console-refresh",
    )
    async def refresh_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        success = await refresh_settings_console_message()
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
        label="Глобальная эффективность",
        style=discord.ButtonStyle.primary,
        custom_id="settings-console-global-efficiency",
    )
    async def global_efficiency_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await interaction.response.send_modal(GlobalEfficiencyModal())

    @discord.ui.button(
        label="Добавить/обновить тип",
        style=discord.ButtonStyle.success,
        custom_id="settings-console-set-type",
    )
    async def set_type_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await interaction.response.send_modal(ShipTypeEfficiencyModal())

    @discord.ui.button(
        label="Удалить тип",
        style=discord.ButtonStyle.danger,
        custom_id="settings-console-delete-type",
    )
    async def delete_type_button(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await interaction.response.send_modal(DeleteShipTypeModal())


__all__ = [
    "SettingsConsoleView",
    "set_settings_console_channel",
    "refresh_settings_console_message",
    "get_settings_console_channel_id",
]

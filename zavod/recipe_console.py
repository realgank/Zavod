from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import discord

from database import RecipeComponent, parse_decimal

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
            "оценку стоимости. При необходимости укажите в дополнительном поле "
            "стоимость чертежа, стоимость создания чертежа, стоимость создания "
            "рецепта и таблицу ресурсов чертежа. Цена рецепта рассчитывается "
            "автоматически на основе стоимости компонентов."
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
    blueprint_data_input = discord.ui.TextInput(
        label="Чертёж и дополнительные расходы",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "Стоимость чертежа: 1 500 000\n"
            "Стоимость создания чертежа: 150 000\n"
            "Стоимость создания рецепта: 750 000\n"
            "ID    Название    Количество    Оценка стоимости"
        ),
        required=False,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        recipe_name = str(self.recipe_name_input.value or "").strip()
        ship_type = str(self.ship_type_input.value or "").strip()
        output_quantity_raw = str(self.output_quantity_input.value or "1").strip()
        components_text = str(self.components_input.value or "").strip()
        blueprint_data_text = str(self.blueprint_data_input.value or "").strip()

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

        blueprint_cost: Optional[Decimal] = None
        blueprint_creation_cost: Optional[Decimal] = None
        recipe_creation_cost: Optional[Decimal] = None
        blueprint_components: list[RecipeComponent] = []

        if blueprint_data_text:
            blueprint_lines = [
                line.strip()
                for line in blueprint_data_text.splitlines()
                if line.strip()
            ]
            blueprint_table_lines: list[str] = []
            blueprint_cost_prefixes = (
                "стоимость чертежа",
                "цена чертежа",
                "blueprint cost",
                "blueprint_cost",
            )
            blueprint_creation_cost_prefixes = (
                "стоимость создания чертежа",
                "цена создания чертежа",
                "blueprint creation cost",
                "blueprint_creation_cost",
            )
            recipe_creation_cost_prefixes = (
                "стоимость создания рецепта",
                "цена создания рецепта",
                "стоимость создания",
                "цена создания",
                "creation cost",
                "creation_cost",
            )
            for line in blueprint_lines:
                lower_line = line.lower()
                if any(
                    lower_line.startswith(prefix + ":")
                    for prefix in blueprint_cost_prefixes
                ):
                    raw_cost = line.split(":", 1)[1].strip()
                    if raw_cost:
                        try:
                            blueprint_cost = parse_decimal(raw_cost)
                        except ValueError:
                            await interaction.response.send_message(
                                "Стоимость чертежа должна быть числом.",
                                ephemeral=True,
                            )
                            return
                        if blueprint_cost < 0:
                            await interaction.response.send_message(
                                "Стоимость чертежа не может быть отрицательной.",
                                ephemeral=True,
                            )
                            return
                    continue
                if any(
                    lower_line.startswith(prefix + ":")
                    for prefix in blueprint_creation_cost_prefixes
                ):
                    raw_blueprint_creation_cost = line.split(":", 1)[1].strip()
                    if raw_blueprint_creation_cost:
                        try:
                            blueprint_creation_cost = parse_decimal(
                                raw_blueprint_creation_cost
                            )
                        except ValueError:
                            await interaction.response.send_message(
                                "Стоимость создания чертежа должна быть числом.",
                                ephemeral=True,
                            )
                            return
                        if blueprint_creation_cost < 0:
                            await interaction.response.send_message(
                                "Стоимость создания чертежа не может быть отрицательной.",
                                ephemeral=True,
                            )
                            return
                    continue
                if any(
                    lower_line.startswith(prefix + ":")
                    for prefix in recipe_creation_cost_prefixes
                ):
                    raw_creation_cost = line.split(":", 1)[1].strip()
                    if raw_creation_cost:
                        try:
                            recipe_creation_cost = parse_decimal(raw_creation_cost)
                        except ValueError:
                            await interaction.response.send_message(
                                "Стоимость создания рецепта должна быть числом.",
                                ephemeral=True,
                            )
                            return
                        if recipe_creation_cost < 0:
                            await interaction.response.send_message(
                                "Стоимость создания рецепта не может быть отрицательной.",
                                ephemeral=True,
                            )
                            return
                    continue
                blueprint_table_lines.append(line)

            if blueprint_table_lines:
                try:
                    blueprint_components = parse_recipe_table(
                        "\n".join(blueprint_table_lines)
                    )
                except ValueError as exc:
                    await interaction.response.send_message(
                        f"Ошибка разбора ресурсов чертежа: {exc}",
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

        post_save_updates: list[str] = []

        try:
            if blueprint_cost is not None:
                await database.set_recipe_blueprint_cost(recipe_name, blueprint_cost)
                post_save_updates.append("Стоимость чертежа сохранена.")
        except Exception as exc:
            logger.exception(
                "Неожиданная ошибка при сохранении стоимости чертежа",
                exc_info=exc,
            )
            await interaction.response.send_message(
                "Рецепт сохранён, но не удалось обновить стоимость чертежа.",
                ephemeral=True,
            )
            return

        try:
            if blueprint_creation_cost is not None:
                await database.set_recipe_blueprint_creation_cost(
                    recipe_name, blueprint_creation_cost
                )
                post_save_updates.append(
                    "Стоимость создания чертежа сохранена."
                )
        except Exception as exc:
            logger.exception(
                "Неожиданная ошибка при сохранении стоимости создания чертежа",
                exc_info=exc,
            )
            await interaction.response.send_message(
                "Рецепт сохранён, но не удалось обновить стоимость создания чертежа.",
                ephemeral=True,
            )
            return

        try:
            if recipe_creation_cost is not None:
                await database.set_recipe_creation_cost(
                    recipe_name, recipe_creation_cost
                )
                post_save_updates.append(
                    "Стоимость создания рецепта сохранена."
                )
        except Exception as exc:
            logger.exception(
                "Неожиданная ошибка при сохранении стоимости создания рецепта",
                exc_info=exc,
            )
            await interaction.response.send_message(
                "Рецепт сохранён, но не удалось обновить стоимость создания рецепта.",
                ephemeral=True,
            )
            return

        try:
            if blueprint_components:
                await database.set_recipe_blueprint_components(
                    recipe_name, blueprint_components
                )
                post_save_updates.append("Ресурсы чертежа сохранены.")
        except Exception as exc:
            logger.exception(
                "Неожиданная ошибка при сохранении ресурсов чертежа",
                exc_info=exc,
            )
            await interaction.response.send_message(
                "Рецепт сохранён, но не удалось обновить ресурсы чертежа.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "\n".join(
                [
                    "Рецепт отправлен на проверку. Подтверждение доступно в канале с лентой рецептов.",
                    *post_save_updates,
                ]
            ),
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

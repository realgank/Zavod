from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Optional

import discord
from discord import app_commands

from database import (
    CircularRecipeReferenceError,
    RecipeNotFoundError,
    ResourcePriceNotFoundError,
    parse_decimal,
)

from .config import LAST_COMMAND_CHANNEL_CONFIG_KEY, RECIPE_FEED_CHANNEL_ID, STATUS_CHANNEL_ENV
from .core import bot, database
from .notifications import send_restart_log
from .recipes import notify_recipe_added, parse_recipe_table, read_attachment_content
from .update import pull_latest_code, restart_service_if_configured
from .graph_requests import (
    add_graph_request_role,
    clear_graph_request_message_reference,
    clear_graph_request_roles,
    get_graph_request_channel_id,
    get_graph_request_message_id,
    get_graph_request_role_ids,
    remove_graph_request_role,
    send_graph_request_message,
    set_graph_request_message,
)

logger = logging.getLogger(__name__)


@bot.tree.command(name="add_recipe", description="Добавить или обновить рецепт")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    recipe_name="Название рецепта",
    output_quantity="Количество результата на цикл",
    table="Текстовая таблица с компонентами",
    file="Текстовый файл с таблицей рецепта",
)
async def add_recipe_command(
    interaction: discord.Interaction,
    recipe_name: str,
    output_quantity: Optional[int] = 1,
    table: Optional[str] = None,
    file: Optional[discord.Attachment] = None,
) -> None:
    """Добавляет или обновляет рецепт."""

    logger.info(
        "Получена команда add_recipe: пользователь=%s, рецепт=%s, количество=%s",
        interaction.user,
        recipe_name,
        output_quantity,
    )
    if output_quantity is None:
        output_quantity = 1
    if output_quantity <= 0:
        await interaction.response.send_message(
            "Количество результата должно быть положительным", ephemeral=False
        )
        return

    await interaction.response.defer(thinking=True)

    table_text = table
    if table_text is None:
        attachment_text = await read_attachment_content(file)
        table_text = attachment_text
    if table_text is None:
        await interaction.followup.send(
            "Не найден текст рецепта. Отправьте таблицу в поле команды или приложите файл.",
            ephemeral=False,
        )
        return

    try:
        components = parse_recipe_table(table_text)
        await database.add_recipe(
            name=recipe_name,
            output_quantity=Decimal(output_quantity),
            components=components,
            is_temporary=True,
        )
    except ValueError as exc:
        await interaction.followup.send(
            f"Ошибка разбора рецепта: {exc}", ephemeral=False
        )
        return
    except Exception as exc:  # pragma: no cover - safety net for discord command context
        logging.exception("Unexpected error while adding recipe")
        await interaction.followup.send(
            f"Произошла непредвиденная ошибка: {exc}", ephemeral=False
        )
        return

    logger.info(
        "Рецепт '%s' успешно сохранён, обновлено %s ресурсов", recipe_name, len(components)
    )
    await interaction.followup.send(
        "\n".join(
            [
                f"Рецепт '{recipe_name}' сохранён как временный.",
                "Обновлены цены {count} ресурсов.".format(count=len(components)),
                "Подтверждение доступно в канале <#{RECIPE_FEED_CHANNEL_ID}>.",
            ]
        ),
        ephemeral=False,
    )

    await notify_recipe_added(
        recipe_name,
        output_quantity=Decimal(output_quantity),
        component_count=len(components),
        is_temporary=True,
    )


@bot.tree.command(name="price", description="Рассчитать стоимость рецепта")
@app_commands.describe(
    recipe_name="Название рецепта",
    efficiency="Эффективность производства в процентах",
)
async def recipe_price_command(
    interaction: discord.Interaction,
    recipe_name: str,
    efficiency: Optional[float] = None,
) -> None:
    """Рассчитывает стоимость рецепта с учётом эффективности."""

    efficiency_decimal: Optional[Decimal]
    logger.info(
        "Получена команда price: пользователь=%s, рецепт=%s, эффективность=%s",
        interaction.user,
        recipe_name,
        efficiency,
    )
    if efficiency is None:
        efficiency_decimal = None
    else:
        try:
            efficiency_decimal = parse_decimal(str(efficiency))
        except ValueError:
            await interaction.response.send_message(
                "Эффективность должна быть числом", ephemeral=False
            )
            return

    try:
        result = await database.calculate_recipe_cost(recipe_name, efficiency_decimal)
    except RecipeNotFoundError:
        await interaction.response.send_message(
            f"Рецепт '{recipe_name}' не найден", ephemeral=False
        )
        return
    except ResourcePriceNotFoundError as exc:
        await interaction.response.send_message(str(exc), ephemeral=False)
        return
    except CircularRecipeReferenceError as exc:
        await interaction.response.send_message(str(exc), ephemeral=False)
        return
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=False)
        return

    effective_efficiency = result["efficiency"]
    run_cost = result["run_cost"]
    unit_cost = result["unit_cost"]
    output_quantity = result["output_quantity"]
    components = result["components"]

    resource_lines = ["Ресурсы:"]
    if components:
        for component in components:
            quantity_display = format(component["quantity"], ",")
            resource_lines.append(
                " • {name}: {quantity}".format(
                    name=component["resource_name"],
                    quantity=quantity_display,
                )
            )
    else:
        resource_lines.append(" • Нет компонентов")

    logger.info(
        "Расчёт стоимости рецепта '%s' завершён: эффективность=%s, стоимость цикла=%s",
        recipe_name,
        effective_efficiency,
        run_cost,
    )
    await interaction.response.send_message(
        "\n".join(
            [
                f"Расчёт для '{recipe_name}'",
                f"Эффективность: {effective_efficiency}%",
                f"Количество на цикл: {output_quantity}",
                f"Стоимость единицы: {unit_cost:,.2f}",
                *resource_lines,
                f"Итого за все: {run_cost:,.2f}",
            ]
        )
    )


@recipe_price_command.autocomplete("recipe_name")
async def recipe_price_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Автодополнение названий рецептов для команды расчёта цены."""

    del interaction
    recipe_names = await database.search_recipe_names(current)
    return [app_commands.Choice(name=name, value=name) for name in recipe_names]


@bot.tree.command(name="resource_price", description="Показать цену ресурса")
@app_commands.describe(resource_name="Название ресурса")
async def resource_price_command(
    interaction: discord.Interaction, *, resource_name: str
) -> None:
    """Показывает последнюю сохранённую цену ресурса."""

    logger.info(
        "Получена команда resource_price: пользователь=%s, ресурс=%s",
        interaction.user,
        resource_name,
    )
    price = await database.get_resource_unit_price(resource_name)
    if price is None:
        await interaction.response.send_message(
            f"Цена для ресурса '{resource_name}' не найдена", ephemeral=False
        )
        return
    logger.info("Цена для ресурса '%s' составила %s", resource_name, price)
    await interaction.response.send_message(
        f"Текущая цена '{resource_name}': {price:,.2f}", ephemeral=False
    )


@resource_price_command.autocomplete("resource_name")
async def resource_price_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Автодополнение названий ресурсов из базы данных."""

    del interaction  # параметр требуется интерфейсом автодополнения
    resource_names = await database.search_resource_names(current)
    return [
        app_commands.Choice(name=name, value=name) for name in resource_names
    ]


@bot.tree.command(name="set_efficiency", description="Установить глобальную эффективность")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(value="Новое значение эффективности в процентах")
async def set_efficiency_command(interaction: discord.Interaction, value: float) -> None:
    """Устанавливает глобальную эффективность по умолчанию."""

    logger.info(
        "Получена команда set_efficiency: пользователь=%s, значение=%s",
        interaction.user,
        value,
    )
    try:
        efficiency = parse_decimal(str(value))
    except ValueError:
        await interaction.response.send_message(
            "Эффективность должна быть числом", ephemeral=False
        )
        return
    if efficiency <= 0:
        await interaction.response.send_message(
            "Эффективность должна быть положительной", ephemeral=False
        )
        return

    await database.set_global_efficiency(efficiency)
    logger.info("Установлена глобальная эффективность: %s", efficiency)
    await interaction.response.send_message(
        f"Глобальная эффективность установлена на {efficiency}%", ephemeral=False
    )


@bot.tree.command(name="global_efficiency", description="Показать глобальную эффективность")
async def global_efficiency_command(interaction: discord.Interaction) -> None:
    """Показывает текущую глобальную эффективность."""

    logger.info(
        "Получена команда global_efficiency: пользователь=%s", interaction.user
    )
    value = await database.get_global_efficiency()
    logger.info("Текущая глобальная эффективность: %s", value)
    await interaction.response.send_message(
        f"Текущая глобальная эффективность: {value}%", ephemeral=False
    )


@bot.tree.command(name="update_bot", description="Обновить код бота из GitHub")
@app_commands.checks.has_permissions(administrator=True)
async def update_bot_command(interaction: discord.Interaction) -> None:
    """Обновляет код бота из GitHub репозитория."""

    logger.info("Получена команда update_bot от пользователя %s", interaction.user)
    await interaction.response.defer(thinking=True)
    try:
        result = await pull_latest_code()
    except FileNotFoundError:
        await interaction.followup.send(
            "Git не установлен на сервере", ephemeral=False
        )
        return
    except RuntimeError as exc:
        message = f"Не удалось обновить бота: {exc}"
        if len(message) > 1900:
            message = message[:1900] + "…"
        logger.warning("Обновление кода завершилось с ошибкой: %s", exc)
        await interaction.followup.send(message, ephemeral=False)
        return

    if len(result) > 1900:
        result = result[:1900] + "…"
    restart_message: Optional[str]
    restart_log_message: Optional[str] = None
    try:
        restart_message = await restart_service_if_configured()
    except RuntimeError as exc:
        logger.warning("Перезапуск после обновления завершился с ошибкой: %s", exc)
        restart_message = f"Обновление выполнено, но перезапуск не удался: {exc}"
        restart_log_message = restart_message
    else:
        if (
            restart_message
            and not os.getenv(STATUS_CHANNEL_ENV)
            and interaction.channel_id is not None
        ):
            await database.set_config_value(
                LAST_COMMAND_CHANNEL_CONFIG_KEY,
                str(interaction.channel_id),
            )
            logger.info(
                "Сохранил канал %s для уведомления после перезапуска",
                interaction.channel_id,
            )
        if restart_message:
            restart_log_message = restart_message

    if restart_log_message:
        restart_log_lines = [
            "Перезапуск после команды /update_bot.",
            f"Пользователь: {interaction.user} (ID: {interaction.user.id})",
        ]
        if interaction.guild_id is not None:
            restart_log_lines.append(f"Сервер: {interaction.guild_id}")
        if interaction.channel_id is not None:
            restart_log_lines.append(f"Канал команды: {interaction.channel_id}")
        restart_log_lines.extend(["", restart_log_message])
        await send_restart_log("\n".join(restart_log_lines))

    logger.info("Команда update_bot завершилась успешно")
    response_lines = ["Успешно обновлено из GitHub. Итог:", result]
    if restart_message:
        response_lines.extend(["", restart_message])
    await interaction.followup.send("\n".join(response_lines), ephemeral=False)


graph_group = app_commands.Group(
    name="graph",
    description="Настройки системы заявок на крафт",
)


@graph_group.command(name="set_channel", description="Указать канал для заявок на крафт")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Канал, где будет размещена кнопка создания заявки")
async def graph_set_channel_command(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    logger.info(
        "Получена команда graph set_channel: пользователь=%s, канал=%s",
        interaction.user,
        channel,
    )
    if interaction.guild is None:
        await interaction.response.send_message(
            "Команда доступна только на сервере.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    previous_message_id = await get_graph_request_message_id()
    previous_channel_id = await get_graph_request_channel_id()
    cleanup_note: Optional[str] = None
    if previous_channel_id is not None and previous_message_id is not None:
        try:
            old_channel = interaction.guild.get_channel(previous_channel_id)
            if old_channel is None:
                old_channel = await interaction.client.fetch_channel(previous_channel_id)
            if isinstance(old_channel, (discord.TextChannel, discord.Thread)):
                old_message = await old_channel.fetch_message(previous_message_id)
                await old_message.delete()
                logger.info(
                    "Удалено предыдущее сообщение заявок на крафт: канал=%s, сообщение=%s",
                    previous_channel_id,
                    previous_message_id,
                )
        except discord.NotFound:
            logger.info(
                "Предыдущее сообщение заявок на крафт не найдено при удалении"
            )
        except discord.HTTPException as exc:
            cleanup_note = "Не удалось удалить предыдущее сообщение."
            logger.warning(
                "Ошибка удаления предыдущего сообщения заявок на крафт %s/%s: %s",
                previous_channel_id,
                previous_message_id,
                exc,
            )

    await clear_graph_request_message_reference()

    try:
        message = await send_graph_request_message(channel)
    except discord.HTTPException as exc:
        logger.exception("Не удалось отправить сообщение с заявками на крафт: %s", exc)
        await interaction.followup.send(
            "Не удалось отправить сообщение с кнопкой в выбранный канал. Проверьте права доступа и попробуйте снова.",
            ephemeral=True,
        )
        return

    await set_graph_request_message(channel.id, message.id)
    response_lines = [
        f"Сообщение с кнопкой размещено в {channel.mention}.",
        f"ID сообщения: {message.id}",
    ]
    if cleanup_note:
        response_lines.append(cleanup_note)
    await interaction.followup.send("\n".join(response_lines), ephemeral=True)


@graph_group.command(name="add_role", description="Добавить роль для уведомлений о заявках")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(role="Роль, которая должна получать уведомления")
async def graph_add_role_command(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    logger.info(
        "Получена команда graph add_role: пользователь=%s, роль=%s",
        interaction.user,
        role,
    )
    if interaction.guild is None:
        await interaction.response.send_message(
            "Команда доступна только на сервере.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    added = await add_graph_request_role(role.id)
    if added:
        message = f"Роль {role.mention} добавлена в список уведомлений."
    else:
        message = f"Роль {role.mention} уже находится в списке уведомлений."
    await interaction.followup.send(message, ephemeral=True)


@graph_group.command(name="remove_role", description="Удалить роль из уведомлений")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(role="Роль, которую необходимо удалить")
async def graph_remove_role_command(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    logger.info(
        "Получена команда graph remove_role: пользователь=%s, роль=%s",
        interaction.user,
        role,
    )
    if interaction.guild is None:
        await interaction.response.send_message(
            "Команда доступна только на сервере.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    removed = await remove_graph_request_role(role.id)
    if removed:
        message = f"Роль {role.mention} удалена из списка уведомлений."
    else:
        message = f"Роль {role.mention} не найдена в списке уведомлений."
    await interaction.followup.send(message, ephemeral=True)


@graph_group.command(name="clear_roles", description="Очистить список ролей уведомлений")
@app_commands.checks.has_permissions(manage_guild=True)
async def graph_clear_roles_command(interaction: discord.Interaction) -> None:
    logger.info(
        "Получена команда graph clear_roles: пользователь=%s", interaction.user
    )
    if interaction.guild is None:
        await interaction.response.send_message(
            "Команда доступна только на сервере.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    await clear_graph_request_roles()
    await interaction.followup.send(
        "Список ролей для уведомлений очищен.", ephemeral=True
    )


@graph_group.command(name="list_roles", description="Показать роли уведомлений")
@app_commands.checks.has_permissions(manage_guild=True)
async def graph_list_roles_command(interaction: discord.Interaction) -> None:
    logger.info(
        "Получена команда graph list_roles: пользователь=%s", interaction.user
    )
    if interaction.guild is None:
        await interaction.response.send_message(
            "Команда доступна только на сервере.", ephemeral=True
        )
        return

    role_ids = await get_graph_request_role_ids()
    if not role_ids:
        await interaction.response.send_message(
            "Список ролей уведомлений пуст.", ephemeral=True
        )
        return

    lines = ["Текущие роли уведомлений:"]
    for role_id in role_ids:
        role = interaction.guild.get_role(role_id)
        if role is not None:
            lines.append(f"• {role.mention} (ID: {role.id})")
        else:
            lines.append(f"• ID {role_id} — роль не найдена")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


bot.tree.add_command(graph_group)

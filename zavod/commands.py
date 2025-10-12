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
from .recipe_console import (
    refresh_recipe_console_message,
    set_recipe_console_channel,
)
from .settings_console import (
    refresh_settings_console_message,
    set_settings_console_channel,
)
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


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _match_ship_types(types: list[str], current: str) -> list[app_commands.Choice[str]]:
    search = current.strip().lower()
    if not search:
        filtered = types[:25]
    else:
        filtered = [
            value
            for value in types
            if search in value.lower()
        ][:25]
    return [app_commands.Choice(name=value, value=value) for value in filtered]


@bot.tree.command(name="add_recipe", description="Добавить или обновить рецепт")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    recipe_name="Название рецепта",
    ship_type="Тип корабля",
    output_quantity="Количество результата на цикл",
    table="Текстовая таблица с компонентами",
    file="Текстовый файл с таблицей рецепта",
)
async def add_recipe_command(
    interaction: discord.Interaction,
    recipe_name: str,
    ship_type: str,
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
        ship_type,
    )
    if output_quantity is None:
        output_quantity = 1
    if output_quantity <= 0:
        await interaction.response.send_message(
            "Количество результата должно быть положительным", ephemeral=False
        )
        return

    normalised_ship_type = ship_type.strip()
    if not normalised_ship_type:
        await interaction.response.send_message(
            "Укажите тип корабля для рецепта.", ephemeral=False
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
            ship_type=normalised_ship_type,
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
        ship_type=normalised_ship_type,
    )


@add_recipe_command.autocomplete("ship_type")
async def add_recipe_ship_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    types = await database.get_known_ship_types()
    return _match_ship_types(types, current)


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
    ship_type = result.get("ship_type")
    efficiency_source = result.get("efficiency_source", "custom")
    blueprint_cost = result.get("blueprint_cost")
    creation_cost = result.get("creation_cost")
    blueprint_creation_cost = result.get("blueprint_creation_cost")
    total_with_additions = result.get("total_with_additions")
    unit_cost_with_additions = result.get("unit_cost_with_additions")
    blueprint_components = result.get("blueprint_components", [])
    blueprint_components_cost = result.get("blueprint_components_cost")

    resource_lines = ["Ресурсы рецепта:"]
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
        resource_lines.append(" • Не указаны (не учтены)")

    blueprint_resource_lines = ["Ресурсы чертежа:"]
    if blueprint_components:
        for component in blueprint_components:
            quantity_display = format(component["quantity"], ",")
            blueprint_resource_lines.append(
                " • {name}: {quantity}".format(
                    name=component["resource_name"],
                    quantity=quantity_display,
                )
            )
    else:
        blueprint_resource_lines.append(" • Не указаны (не учтены)")

    logger.info(
        "Расчёт стоимости рецепта '%s' завершён: эффективность=%s, стоимость цикла=%s",
        recipe_name,
        effective_efficiency,
        run_cost,
    )
    if efficiency_source == "ship_type" and ship_type:
        efficiency_line = (
            f"Эффективность типа '{ship_type}': {effective_efficiency}%"
        )
    elif efficiency_source == "global":
        efficiency_line = f"Эффективность (глобальная): {effective_efficiency}%"
    else:
        efficiency_line = f"Эффективность: {effective_efficiency}%"

    type_line = f"Тип корабля: {ship_type}" if ship_type else "Тип корабля: не указан"

    summary_lines = [
        f"Расчёт для '{recipe_name}'",
        efficiency_line,
        type_line,
        f"Количество на цикл: {output_quantity}",
        f"Стоимость единицы: {unit_cost:,.2f}",
    ]

    summary_lines.append("")
    summary_lines.append("Рецепт:")
    summary_lines.append(f" • Цена (компоненты): {run_cost:,.2f}")
    summary_lines.append(
        (
            f" • Стоимость создания: {creation_cost:,.2f}"
            if creation_cost is not None
            else " • Стоимость создания: не задана (не учтена)"
        )
    )

    summary_lines.append("")
    summary_lines.append("Чертёж:")
    summary_lines.append(
        (
            f" • Цена (компоненты): {blueprint_components_cost:,.2f}"
            if blueprint_components
            else " • Цена (компоненты): не указана (не учтена)"
        )
    )
    summary_lines.append(
        (
            f" • Стоимость создания: {blueprint_creation_cost:,.2f}"
            if blueprint_creation_cost is not None
            else " • Стоимость создания: не задана (не учтена)"
        )
    )
    summary_lines.append(
        (
            f" • Стоимость покупки: {blueprint_cost:,.2f}"
            if blueprint_cost is not None
            else " • Стоимость покупки: не задана (не учтена)"
        )
    )

    summary_lines.append("")
    summary_lines.extend(resource_lines)
    summary_lines.extend(blueprint_resource_lines)
    summary_lines.append("")
    summary_lines.append(f"Итого за все (без доп. расходов): {run_cost:,.2f}")

    additions_present = (
        bool(blueprint_components)
        or blueprint_cost is not None
        or creation_cost is not None
        or blueprint_creation_cost is not None
    )
    if additions_present:
        summary_lines.append("")
        if blueprint_components:
            summary_lines.append(
                f"Стоимость компонентов чертежа: {blueprint_components_cost:,.2f}"
            )
        summary_lines.append(
            f"Итого с доп. расходами: {total_with_additions:,.2f}"
        )
        if unit_cost_with_additions is not None:
            summary_lines.append(
                "Стоимость единицы с доп. расходами: {value:,.2f}".format(
                    value=unit_cost_with_additions
                )
            )

    await interaction.response.send_message(
        "\n".join(summary_lines)
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


@bot.tree.command(
    name="set_recipe_blueprint_components",
    description="Задать ресурсы для чертежа рецепта",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    recipe_name="Название рецепта",
    table="Текстовая таблица с ресурсами чертежа",
    file="Текстовый файл с таблицей чертежа",
)
async def set_recipe_blueprint_components_command(
    interaction: discord.Interaction,
    recipe_name: str,
    table: Optional[str] = None,
    file: Optional[discord.Attachment] = None,
) -> None:
    logger.info(
        "Получена команда set_recipe_blueprint_components: пользователь=%s, рецепт=%s",
        interaction.user,
        recipe_name,
    )
    await interaction.response.defer(thinking=True)

    table_text = table
    if table_text is None:
        attachment_text = await read_attachment_content(file)
        table_text = attachment_text
    if table_text is None:
        await interaction.followup.send(
            "Не найден текст ресурсов чертежа. Укажите таблицу в команде или приложите файл.",
            ephemeral=False,
        )
        return

    try:
        components = parse_recipe_table(table_text)
        await database.set_recipe_blueprint_components(recipe_name, components)
    except ValueError as exc:
        await interaction.followup.send(
            f"Ошибка разбора таблицы: {exc}", ephemeral=False
        )
        return
    except RecipeNotFoundError:
        await interaction.followup.send(
            f"Рецепт '{recipe_name}' не найден", ephemeral=False
        )
        return
    except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
        logger.exception("Unexpected error while setting blueprint components")
        await interaction.followup.send(
            f"Произошла непредвиденная ошибка: {exc}", ephemeral=False
        )
        return

    await interaction.followup.send(
        "Для чертежа рецепта '{recipe}' сохранено {count} компонентов.".format(
            recipe=recipe_name,
            count=len(components),
        ),
        ephemeral=False,
    )


@set_recipe_blueprint_components_command.autocomplete("recipe_name")
async def set_recipe_blueprint_components_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    recipe_names = await database.search_recipe_names(current)
    return [app_commands.Choice(name=name, value=name) for name in recipe_names]


@bot.tree.command(
    name="set_recipe_blueprint_cost",
    description="Установить стоимость чертежа рецепта",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    recipe_name="Название рецепта",
    value="Стоимость чертежа",
)
async def set_recipe_blueprint_cost_command(
    interaction: discord.Interaction,
    recipe_name: str,
    value: float,
) -> None:
    logger.info(
        "Получена команда set_recipe_blueprint_cost: пользователь=%s, рецепт=%s, стоимость=%s",
        interaction.user,
        recipe_name,
        value,
    )
    try:
        cost = parse_decimal(str(value))
    except ValueError:
        await interaction.response.send_message(
            "Стоимость должна быть числом",
            ephemeral=False,
        )
        return
    if cost < 0:
        await interaction.response.send_message(
            "Стоимость не может быть отрицательной",
            ephemeral=False,
        )
        return
    try:
        await database.set_recipe_blueprint_cost(recipe_name, cost)
    except RecipeNotFoundError:
        await interaction.response.send_message(
            f"Рецепт '{recipe_name}' не найден",
            ephemeral=False,
        )
        return
    await interaction.response.send_message(
        "Стоимость чертежа для '{recipe}' установлена на {cost}".format(
            recipe=recipe_name,
            cost=_format_decimal(cost),
        ),
        ephemeral=False,
    )


@set_recipe_blueprint_cost_command.autocomplete("recipe_name")
async def set_recipe_blueprint_cost_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    recipe_names = await database.search_recipe_names(current)
    return [app_commands.Choice(name=name, value=name) for name in recipe_names]


@bot.tree.command(
    name="set_recipe_blueprint_creation_cost",
    description="Установить стоимость создания чертежа",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    recipe_name="Название рецепта",
    value="Стоимость создания чертежа",
)
async def set_recipe_blueprint_creation_cost_command(
    interaction: discord.Interaction,
    recipe_name: str,
    value: float,
) -> None:
    logger.info(
        "Получена команда set_recipe_blueprint_creation_cost: пользователь=%s, рецепт=%s, стоимость=%s",
        interaction.user,
        recipe_name,
        value,
    )
    try:
        cost = parse_decimal(str(value))
    except ValueError:
        await interaction.response.send_message(
            "Стоимость должна быть числом",
            ephemeral=False,
        )
        return
    if cost < 0:
        await interaction.response.send_message(
            "Стоимость не может быть отрицательной",
            ephemeral=False,
        )
        return
    try:
        await database.set_recipe_blueprint_creation_cost(recipe_name, cost)
    except RecipeNotFoundError:
        await interaction.response.send_message(
            f"Рецепт '{recipe_name}' не найден",
            ephemeral=False,
        )
        return
    await interaction.response.send_message(
        "Стоимость создания чертежа для '{recipe}' установлена на {cost}".format(
            recipe=recipe_name,
            cost=_format_decimal(cost),
        ),
        ephemeral=False,
    )


@set_recipe_blueprint_creation_cost_command.autocomplete("recipe_name")
async def set_recipe_blueprint_creation_cost_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    recipe_names = await database.search_recipe_names(current)
    return [app_commands.Choice(name=name, value=name) for name in recipe_names]


@bot.tree.command(
    name="set_recipe_creation_cost",
    description="Установить цену создания рецепта",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    recipe_name="Название рецепта",
    value="Цена создания",
)
async def set_recipe_creation_cost_command(
    interaction: discord.Interaction,
    recipe_name: str,
    value: float,
) -> None:
    logger.info(
        "Получена команда set_recipe_creation_cost: пользователь=%s, рецепт=%s, стоимость=%s",
        interaction.user,
        recipe_name,
        value,
    )
    try:
        cost = parse_decimal(str(value))
    except ValueError:
        await interaction.response.send_message(
            "Стоимость должна быть числом",
            ephemeral=False,
        )
        return
    if cost < 0:
        await interaction.response.send_message(
            "Стоимость не может быть отрицательной",
            ephemeral=False,
        )
        return
    try:
        await database.set_recipe_creation_cost(recipe_name, cost)
    except RecipeNotFoundError:
        await interaction.response.send_message(
            f"Рецепт '{recipe_name}' не найден",
            ephemeral=False,
        )
        return
    await interaction.response.send_message(
        "Цена создания для '{recipe}' установлена на {cost}".format(
            recipe=recipe_name,
            cost=_format_decimal(cost),
        ),
        ephemeral=False,
    )


@set_recipe_creation_cost_command.autocomplete("recipe_name")
async def set_recipe_creation_cost_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    recipe_names = await database.search_recipe_names(current)
    return [app_commands.Choice(name=name, value=name) for name in recipe_names]


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


@bot.tree.command(
    name="set_ship_type_efficiency",
    description="Установить эффективность для типа корабля",
)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    ship_type="Название типа корабля",
    value="Эффективность в процентах",
)
async def set_ship_type_efficiency_command(
    interaction: discord.Interaction, ship_type: str, value: float
) -> None:
    logger.info(
        "Получена команда set_ship_type_efficiency: пользователь=%s, тип=%s, значение=%s",
        interaction.user,
        ship_type,
        value,
    )
    normalised_type = ship_type.strip()
    if not normalised_type:
        await interaction.response.send_message(
            "Укажите название типа корабля.", ephemeral=False
        )
        return
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

    await database.set_ship_type_efficiency(normalised_type, efficiency)
    await refresh_settings_console_message()
    await interaction.response.send_message(
        "Эффективность для типа '{type}' установлена на {value}%".format(
            type=normalised_type,
            value=_format_decimal(efficiency),
        ),
        ephemeral=False,
    )


@set_ship_type_efficiency_command.autocomplete("ship_type")
async def set_ship_type_efficiency_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    types = await database.get_known_ship_types()
    return _match_ship_types(types, current)


@bot.tree.command(
    name="delete_ship_type_efficiency",
    description="Удалить настройку эффективности типа корабля",
)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(ship_type="Название типа корабля")
async def delete_ship_type_efficiency_command(
    interaction: discord.Interaction, ship_type: str
) -> None:
    logger.info(
        "Получена команда delete_ship_type_efficiency: пользователь=%s, тип=%s",
        interaction.user,
        ship_type,
    )
    normalised_type = ship_type.strip()
    if not normalised_type:
        await interaction.response.send_message(
            "Укажите название типа корабля.", ephemeral=False
        )
        return

    removed = await database.delete_ship_type_efficiency(normalised_type)
    await refresh_settings_console_message()
    if removed:
        message = f"Настройка эффективности для типа '{normalised_type}' удалена."
    else:
        message = f"Тип '{normalised_type}' не найден в настройках эффективности."
    await interaction.response.send_message(message, ephemeral=False)


@delete_ship_type_efficiency_command.autocomplete("ship_type")
async def delete_ship_type_efficiency_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    del interaction
    types = await database.get_known_ship_types()
    return _match_ship_types(types, current)


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


@bot.tree.command(
    name="ship_type_efficiencies",
    description="Показать эффективности по типам кораблей",
)
async def ship_type_efficiencies_command(
    interaction: discord.Interaction,
) -> None:
    logger.info(
        "Получена команда ship_type_efficiencies: пользователь=%s",
        interaction.user,
    )
    global_efficiency = await database.get_global_efficiency()
    type_efficiencies = await database.list_ship_type_efficiencies()
    stats = await database.get_ship_type_statistics()

    lines = [
        "Глобальная эффективность: {value}%".format(
            value=_format_decimal(global_efficiency)
        )
    ]
    if not stats and not type_efficiencies:
        lines.append("Типы кораблей не настроены.")
    else:
        lines.append("Настройки по типам:")
        handled = set()
        for entry in stats:
            ship_type = entry["ship_type"]
            recipe_count = entry["recipe_count"]
            if ship_type is None:
                lines.append(
                    f"• Не указан: рецептов {recipe_count}"
                )
                continue
            handled.add(ship_type)
            efficiency = type_efficiencies.get(ship_type)
            if efficiency is None:
                lines.append(
                    f"• {ship_type}: эффективность не задана (рецептов {recipe_count})"
                )
            else:
                lines.append(
                    "• {type}: {value}% (рецептов {count})".format(
                        type=ship_type,
                        value=_format_decimal(efficiency),
                        count=recipe_count,
                    )
                )
        for ship_type, efficiency in type_efficiencies.items():
            if ship_type in handled:
                continue
            lines.append(
                "• {type}: {value}% (рецептов 0)".format(
                    type=ship_type,
                    value=_format_decimal(efficiency),
                )
            )

    await interaction.response.send_message("\n".join(lines), ephemeral=False)


@bot.tree.command(
    name="set_settings_console_channel",
    description="Назначить канал консоли настройки эффективности",
)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Текстовый канал для размещения консоли")
async def set_settings_console_channel_command(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    logger.info(
        "Получена команда set_settings_console_channel: пользователь=%s, канал=%s",
        interaction.user,
        channel,
    )
    success = await set_settings_console_channel(channel)
    if success:
        message = (
            f"Консоль настройки эффективности размещена в канале {channel.mention}."
        )
    else:
        message = (
            "Не удалось опубликовать консоль настройки. Проверьте права доступа."
        )
    await interaction.response.send_message(message, ephemeral=False)


@bot.tree.command(
    name="refresh_settings_console",
    description="Обновить консоль настройки эффективности",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def refresh_settings_console_command(
    interaction: discord.Interaction,
) -> None:
    logger.info(
        "Получена команда refresh_settings_console: пользователь=%s",
        interaction.user,
    )
    success = await refresh_settings_console_message()
    if success:
        message = "Консоль настроек обновлена."
    else:
        message = "Не удалось обновить консоль. Проверьте, настроен ли канал."
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(
    name="set_recipe_console_channel",
    description="Назначить канал панели добавления рецептов",
)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Текстовый канал для публикации панели")
async def set_recipe_console_channel_command(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    logger.info(
        "Получена команда set_recipe_console_channel: пользователь=%s, канал=%s",
        interaction.user,
        channel,
    )
    success = await set_recipe_console_channel(channel)
    if success:
        message = f"Панель добавления рецептов размещена в канале {channel.mention}."
    else:
        message = (
            "Не удалось опубликовать панель добавления рецептов. Проверьте права доступа."
        )
    await interaction.response.send_message(message, ephemeral=False)


@bot.tree.command(
    name="refresh_recipe_console",
    description="Обновить панель добавления рецептов",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def refresh_recipe_console_command(
    interaction: discord.Interaction,
) -> None:
    logger.info(
        "Получена команда refresh_recipe_console: пользователь=%s",
        interaction.user,
    )
    success = await refresh_recipe_console_message()
    if success:
        message = "Панель добавления рецептов обновлена."
    else:
        message = "Не удалось обновить панель. Проверьте настройку канала."
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(
    name="audit_recipe_types",
    description="Показать рецепты без указанного типа",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def audit_recipe_types_command(
    interaction: discord.Interaction,
) -> None:
    logger.info(
        "Получена команда audit_recipe_types: пользователь=%s",
        interaction.user,
    )
    recipes_without_type = await database.get_recipes_without_type()
    if not recipes_without_type:
        message = "Все рецепты имеют указанный тип."
    else:
        preview_limit = 20
        preview = recipes_without_type[:preview_limit]
        lines = ["Рецепты без типа:"]
        lines.extend(f"• {name}" for name in preview)
        if len(recipes_without_type) > preview_limit:
            lines.append(
                f"…и ещё {len(recipes_without_type) - preview_limit} рецептов без типа."
            )
        message = "\n".join(lines)
    await interaction.response.send_message(message, ephemeral=True)


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

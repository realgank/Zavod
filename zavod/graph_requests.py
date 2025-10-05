from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Iterable, Optional

import discord

from .core import database
from database import CircularRecipeReferenceError, ResourcePriceNotFoundError

logger = logging.getLogger(__name__)

GRAPH_REQUEST_CHANNEL_CONFIG_KEY = "graph_request_channel_id"
GRAPH_REQUEST_MESSAGE_CONFIG_KEY = "graph_request_message_id"
GRAPH_REQUEST_ROLE_CONFIG_KEY = "graph_request_role_ids"
GRAPH_REQUEST_SHIP_SCHEDULE_CONFIG_KEY = "graph_request_ship_schedule"


def _encode_role_ids(role_ids: Iterable[int]) -> str:
    unique_sorted_ids = sorted({role_id for role_id in role_ids if role_id > 0})
    return json.dumps(unique_sorted_ids, ensure_ascii=False)


def _decode_role_ids(raw: Optional[str]) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Не удалось разобрать список ролей для заявок на граф из значения: %s", raw
        )
        return []
    result: list[int] = []
    for value in data:
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            logger.debug(
                "Пропускаю некорректное значение идентификатора роли: %s", value
            )
            continue
        if role_id > 0:
            result.append(role_id)
    return result


async def get_graph_request_channel_id() -> Optional[int]:
    raw_value = await database.get_config_value(GRAPH_REQUEST_CHANNEL_CONFIG_KEY)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except ValueError:
        logger.warning(
            "Сохранённый идентификатор канала заявок на граф некорректен: %s", raw_value
        )
        return None


async def set_graph_request_channel_id(channel_id: int) -> None:
    await database.set_config_value(
        GRAPH_REQUEST_CHANNEL_CONFIG_KEY, str(channel_id)
    )


async def get_graph_request_message_id() -> Optional[int]:
    raw_value = await database.get_config_value(GRAPH_REQUEST_MESSAGE_CONFIG_KEY)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except ValueError:
        logger.warning(
            "Сохранённый идентификатор сообщения для заявок на граф некорректен: %s",
            raw_value,
        )
        return None


async def set_graph_request_message(channel_id: int, message_id: int) -> None:
    await set_graph_request_channel_id(channel_id)
    await database.set_config_value(
        GRAPH_REQUEST_MESSAGE_CONFIG_KEY, str(message_id)
    )


async def clear_graph_request_message_reference() -> None:
    await database.pop_config_value(GRAPH_REQUEST_MESSAGE_CONFIG_KEY)


async def get_graph_request_role_ids() -> list[int]:
    raw_value = await database.get_config_value(GRAPH_REQUEST_ROLE_CONFIG_KEY)
    return _decode_role_ids(raw_value)


async def set_graph_request_role_ids(role_ids: Iterable[int]) -> None:
    encoded = _encode_role_ids(role_ids)
    await database.set_config_value(GRAPH_REQUEST_ROLE_CONFIG_KEY, encoded)


def _normalise_ship_name(name: object) -> Optional[str]:
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    return stripped or None


async def get_graph_ship_names() -> list[str]:
    raw_value = await database.get_config_value(GRAPH_REQUEST_SHIP_SCHEDULE_CONFIG_KEY)
    if not raw_value:
        return []
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError:
        logger.warning(
            "Не удалось разобрать список кораблей графика из значения: %s", raw_value
        )
        return []

    unique: list[str] = []
    seen: set[str] = set()
    for item in decoded:
        name = _normalise_ship_name(item)
        if not name:
            continue
        if name in seen:
            continue
        unique.append(name)
        seen.add(name)
    return unique


async def set_graph_ship_names(names: Iterable[str]) -> None:
    unique: list[str] = []
    seen: set[str] = set()
    for value in names:
        name = _normalise_ship_name(value)
        if not name:
            continue
        if name in seen:
            continue
        unique.append(name)
        seen.add(name)
    await database.set_config_value(
        GRAPH_REQUEST_SHIP_SCHEDULE_CONFIG_KEY,
        json.dumps(unique, ensure_ascii=False),
    )


async def add_graph_request_role(role_id: int) -> bool:
    role_ids = await get_graph_request_role_ids()
    if role_id in role_ids:
        return False
    role_ids.append(role_id)
    await set_graph_request_role_ids(role_ids)
    return True


async def remove_graph_request_role(role_id: int) -> bool:
    role_ids = await get_graph_request_role_ids()
    if role_id not in role_ids:
        return False
    updated = [existing for existing in role_ids if existing != role_id]
    await set_graph_request_role_ids(updated)
    return True


async def clear_graph_request_roles() -> None:
    await set_graph_request_role_ids([])


def _format_quantity(value: Decimal) -> str:
    if value == value.to_integral():
        formatted = f"{int(value):,}"
    else:
        formatted = f"{value.normalize():f}"
    return formatted.replace(",", " ")


def _format_currency(value: Decimal) -> str:
    quantised = value.quantize(Decimal("0.01"))
    formatted = f"{quantised:,.2f}"
    return formatted.replace(",", " ")


async def send_graph_request_message(channel: discord.TextChannel) -> discord.Message:
    content = (
        "В этом канале принимаются заявки на граф.\n"
        "Нажмите кнопку ниже, чтобы создать отдельную тему с вашей заявкой."
    )
    view = GraphRequestView()
    message = await channel.send(content, view=view)
    return message


async def _get_request_channel(
    guild: discord.Guild, channel_id: int
) -> Optional[discord.TextChannel]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await guild.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось получить канал %s для создания заявки на граф: %s",
                channel_id,
                exc,
            )
            return None
        else:
            channel = fetched
    if isinstance(channel, discord.TextChannel):
        return channel
    logger.warning(
        "Канал %s имеет неподдерживаемый тип %s для заявок на граф",
        channel_id,
        type(channel).__name__,
    )
    return None


async def _create_request_thread(
    channel: discord.TextChannel,
    requester: discord.Member,
    recipe_name: str,
) -> discord.Thread:
    base_name = f"Граф • {recipe_name}"
    if requester.display_name:
        base_name += f" • {requester.display_name}"
    thread_name = base_name[:100]
    try:
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=10080,
            reason=f"Заявка на граф от {requester}"
        )
    except discord.HTTPException as exc:
        logger.warning(
            "Не удалось создать приватную тему для заявки на граф, пробую открытую: %s",
            exc,
        )
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
            reason=f"Заявка на граф от {requester}"
        )
    return thread


async def _prepare_thread(
    thread: discord.Thread,
    requester: discord.Member,
    roles: list[discord.Role],
) -> None:
    try:
        await thread.add_user(requester)
    except discord.HTTPException as exc:
        logger.debug(
            "Не удалось добавить инициатора заявки %s в тему %s: %s",
            requester,
            thread.id,
            exc,
        )

    added_users: set[int] = {requester.id}
    for role in roles:
        for member in role.members:
            if member.bot:
                continue
            if member.id in added_users:
                continue
            try:
                await thread.add_user(member)
            except discord.HTTPException as exc:
                logger.debug(
                    "Не удалось добавить пользователя %s из роли %s в тему %s: %s",
                    member,
                    role.id,
                    thread.id,
                    exc,
                )
            else:
                added_users.add(member.id)


async def _format_component_lines(recipe: dict[str, object]) -> list[str]:
    components = recipe.get("components")
    if not isinstance(components, list) or not components:
        return ["• Нет сохранённых компонентов рецепта"]
    lines: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        resource_name = str(component.get("resource_name", "Неизвестный ресурс"))
        try:
            quantity = Decimal(str(component.get("quantity", "0")))
        except Exception:  # pragma: no cover - защита от некорректных значений
            quantity_display = "?"
        else:
            quantity_display = _format_quantity(quantity)
        lines.append(f"• {resource_name}: {quantity_display}")
    return lines or ["• Нет сохранённых компонентов рецепта"]


class GraphRequestModal(discord.ui.Modal):
    def __init__(self, *, channel_id: int, ship_name: str) -> None:
        super().__init__(title="Новая заявка на граф")
        self._channel_id = channel_id
        self._ship_name = ship_name
        self.comment_input = discord.ui.TextInput(
            label="Комментарий", required=False, style=discord.TextStyle.paragraph, max_length=500
        )
        self.add_item(self.comment_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Заявку можно создать только на сервере.", ephemeral=True
            )
            return

        ship_name = self._ship_name

        recipe = await database.get_recipe(ship_name)
        if recipe is None:
            await interaction.response.send_message(
                "Рецепт для выбранного корабля не найден. Обратитесь к администрации.",
                ephemeral=True,
            )
            return

        channel = await _get_request_channel(guild, self._channel_id)
        if channel is None:
            await interaction.response.send_message(
                "Не удалось найти канал для создания заявки. Обратитесь к администрации.",
                ephemeral=True,
            )
            return

        try:
            thread = await _create_request_thread(
                channel, interaction.user, recipe.get("name", ship_name)
            )
        except discord.HTTPException as exc:
            logger.exception("Не удалось создать тему для заявки на граф: %s", exc)
            await interaction.response.send_message(
                "Не удалось создать тему для заявки. Попробуйте позже или обратитесь к администрации.",
                ephemeral=True,
            )
            return

        role_ids = await get_graph_request_role_ids()
        roles = [role for role_id in role_ids if (role := guild.get_role(role_id))]
        await _prepare_thread(thread, interaction.user, roles)

        comment = self.comment_input.value.strip()
        cost_result = None
        try:
            cost_result = await database.calculate_recipe_cost(ship_name)
        except ResourcePriceNotFoundError as exc:
            logger.info(
                "Не удалось рассчитать стоимость для '%s' в заявке на граф: %s",
                ship_name,
                exc,
            )
        except CircularRecipeReferenceError as exc:
            logger.warning(
                "Обнаружена циклическая ссылка при расчёте стоимости '%s': %s",
                ship_name,
                exc,
            )
        except ValueError as exc:
            logger.warning(
                "Некорректные данные рецепта '%s' при расчёте стоимости: %s",
                ship_name,
                exc,
            )
        lines = [
            f"{interaction.user.mention} хочет построить **{recipe.get('name', ship_name)}**.",
        ]
        if comment:
            lines.append("")
            lines.append(f"Комментарий: {comment}")
        lines.append("")
        lines.append("Требуемые ресурсы:")
        lines.extend(await _format_component_lines(recipe))
        if cost_result is not None:
            lines.append("")
            lines.append(
                "Итоговая стоимость: {run} ISK за цикл (≈ {unit} ISK за единицу).".format(
                    run=_format_currency(cost_result["run_cost"]),
                    unit=_format_currency(cost_result["unit_cost"]),
                )
            )

        if roles:
            role_mentions = " ".join(role.mention for role in roles)
            lines.append("")
            lines.append(f"Уведомление: {role_mentions}")

        try:
            await thread.send("\n".join(lines))
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось отправить сообщение в тему %s: %s", thread.id, exc
            )

        await interaction.response.send_message(
            f"Заявка создана: {thread.mention}", ephemeral=True
        )


class GraphRequestView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Кнопка доступна только на сервере.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Создать запрос",
        style=discord.ButtonStyle.primary,
        custom_id="graph-request:create",
    )
    async def create_request(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        channel_id = await get_graph_request_channel_id()
        if channel_id is None:
            await interaction.response.send_message(
                "Канал для заявок не настроен. Обратитесь к администрации.",
                ephemeral=True,
            )
            return
        ship_names = await get_graph_ship_names()
        if not ship_names:
            await interaction.response.send_message(
                "График кораблей не настроен. Обратитесь к администрации.",
                ephemeral=True,
            )
            return
        view = GraphShipSelectionView(channel_id=channel_id, ship_names=ship_names)
        await interaction.response.send_message(
            "Выберите корабль из графика:", view=view, ephemeral=True
        )


class GraphShipSelect(discord.ui.Select):
    def __init__(self, *, channel_id: int, ship_names: list[str]) -> None:
        options: list[discord.SelectOption] = []
        limited_names = ship_names[:25]
        if len(ship_names) > 25:
            logger.warning(
                "Список кораблей графика содержит %s элементов, отображаются только первые 25",
                len(ship_names),
            )
        for name in limited_names:
            label = name[:100] or name
            options.append(discord.SelectOption(label=label, value=name))
        super().__init__(
            placeholder="Выберите корабль из графика",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        ship_name = self.values[0]
        await interaction.response.send_modal(
            GraphRequestModal(channel_id=self._channel_id, ship_name=ship_name)
        )
        if self.view is not None:
            self.view.stop()


class GraphShipSelectionView(discord.ui.View):
    def __init__(self, *, channel_id: int, ship_names: list[str]) -> None:
        super().__init__(timeout=300)
        self.add_item(GraphShipSelect(channel_id=channel_id, ship_names=ship_names))


__all__ = [
    "GRAPH_REQUEST_CHANNEL_CONFIG_KEY",
    "GRAPH_REQUEST_MESSAGE_CONFIG_KEY",
    "GRAPH_REQUEST_ROLE_CONFIG_KEY",
    "GRAPH_REQUEST_SHIP_SCHEDULE_CONFIG_KEY",
    "GraphRequestModal",
    "GraphRequestView",
    "add_graph_request_role",
    "clear_graph_request_message_reference",
    "clear_graph_request_roles",
    "get_graph_request_channel_id",
    "get_graph_request_message_id",
    "get_graph_request_role_ids",
    "get_graph_ship_names",
    "remove_graph_request_role",
    "send_graph_request_message",
    "set_graph_request_channel_id",
    "set_graph_request_message",
    "set_graph_request_role_ids",
    "set_graph_ship_names",
]

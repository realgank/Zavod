import asyncio
import logging
import os
import re
import shlex
import tempfile
from asyncio import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from database import (
    CircularRecipeReferenceError,
    Database,
    RecipeComponent,
    RecipeNotFoundError,
    ResourcePriceNotFoundError,
    initialise_database,
    parse_decimal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Return True if the environment variable represents an enabled flag."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

database = Database()


STATUS_CHANNEL_ENV = "BOT_STATUS_CHANNEL_ID"
LAST_COMMAND_CHANNEL_CONFIG_KEY = "last_command_channel_id"
RECIPE_FEED_CHANNEL_ID = 1423404992273977364
RESTART_LOG_CHANNEL_ID = 1423405721998987306


def _replace_status_line(content: Optional[str], new_status: str) -> str:
    lines = (content or "").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Статус:"):
            lines[index] = f"Статус: {new_status}"
            break
    else:
        lines.append(f"Статус: {new_status}")
    return "\n".join(lines)


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

        updated_content = _replace_status_line(
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

        updated_content = _replace_status_line(
            interaction.message.content,
            f"удалён пользователем {interaction.user.mention}",
        )
        await interaction.response.edit_message(content=updated_content, view=self)
        await interaction.followup.send(
            f"Рецепт '{self.recipe_name}' удалён.", ephemeral=True
        )


def _load_env_file(env_path: Path) -> None:
    """Load environment variables from a .env file if it exists."""

    if not env_path.exists():
        logger.debug("Файл окружения %s не найден, пропускаю загрузку", env_path)
        return

    logger.info("Загружаю переменные окружения из %s", env_path)
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.debug(
                    "Пропускаю строку без разделителя '=' в файле окружения: %s",
                    raw_line,
                )
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                logger.debug(
                    "Пропускаю строку с пустым ключом в файле окружения: %s",
                    raw_line,
                )
                continue
            if key in os.environ:
                logger.debug(
                    "Переменная окружения %s уже установлена, значение из файла пропущено",
                    key,
                )
                continue
            os.environ[key] = value.strip()
    except OSError as exc:
        logger.warning(
            "Не удалось прочитать файл окружения %s: %s", env_path, exc
        )


def _parse_recipe_table(raw_table: str) -> list[RecipeComponent]:
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
    inline_pattern = re.compile(
        r"(?<!\S)(\d+)\s+(.+?)\s+([0-9]+(?:[.][0-9]+)?)\s+([0-9]+(?:[.][0-9]+)?)(?=(?:\s+\d+\s)|\s*$)"
    )

    for line in lines:
        normalised = line.replace("\u200b", "")  # remove zero-width spaces from Discord tables
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


def _split_message(content: str, *, limit: int = 2000) -> list[str]:
    """Split *content* into chunks that fit within Discord's message limit."""

    if not content:
        return [""]
    return [content[i : i + limit] for i in range(0, len(content), limit)]


async def _send_restart_log(message: str) -> None:
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

    for chunk in _split_message(message):
        try:
            await channel.send(chunk)
        except discord.HTTPException as exc:
            logger.warning(
                "Не удалось отправить лог перезапуска в канал %s: %s",
                channel_id,
                exc,
            )
            break


async def _read_attachment_content(attachment: Optional[discord.Attachment]) -> Optional[str]:
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


async def _pull_latest_code() -> str:
    logger.info("Запускаю обновление кода из GitHub")
    env = os.environ.copy()
    github_username = env.get("GITHUB_USERNAME")
    github_token = env.get("GITHUB_TOKEN")
    askpass_path: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None

    try:
        if github_token and github_username:
            fd, askpass_path = tempfile.mkstemp(prefix="git-askpass-", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as askpass_file:
                askpass_file.write("#!/usr/bin/env bash\n")
                askpass_file.write("case \"$1\" in\n")
                askpass_file.write("    *'Username'*|*'username'*)\n")
                askpass_file.write(f"        echo {shlex.quote(github_username)}\n")
                askpass_file.write("        ;;\n")
                askpass_file.write("    *)\n")
                askpass_file.write(f"        echo {shlex.quote(github_token)}\n")
                askpass_file.write("        ;;\n")
                askpass_file.write("esac\n")
            os.chmod(askpass_path, 0o700)
            env["GIT_ASKPASS"] = askpass_path
            env["SSH_ASKPASS"] = askpass_path
            env["GIT_TERMINAL_PROMPT"] = "0"

        process = await asyncio.create_subprocess_exec(
            "git",
            "pull",
            "--ff-only",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except Exception:
        if askpass_path:
            try:
                os.remove(askpass_path)
            except FileNotFoundError:
                pass
        raise

    if process is None:
        raise RuntimeError("Не удалось запустить git pull")

    stdout, stderr = await process.communicate()
    if askpass_path:
        try:
            os.remove(askpass_path)
        except FileNotFoundError:
            pass
    if process.returncode != 0:
        error_output = stderr.decode().strip() or stdout.decode().strip()
        logger.error(
            "Команда git pull завершилась с ошибкой %s: %s",
            process.returncode,
            error_output,
        )
        raise RuntimeError(error_output or "Не удалось выполнить git pull")
    output = stdout.decode().strip()
    if not output:
        output = "Изменений нет"
    logger.info("Обновление кода завершено: %s", output)
    return output


async def _schedule_process_restart(delay: float) -> None:
    """Завершить текущий процесс после указанной задержки."""

    await asyncio.sleep(delay)
    logger.info("Завершение процесса для автоматического перезапуска")
    os._exit(1)


async def _restart_service_if_configured() -> Optional[str]:
    """Перезапустить сервис, если настроены соответствующие переменные окружения."""

    restart_command = os.getenv("BOT_RESTART_COMMAND")
    if restart_command:
        logger.info("Перезапуск бота командой: %s", restart_command)
        process = await asyncio.create_subprocess_shell(
            restart_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            error_output = stderr.decode().strip() or stdout.decode().strip()
            logger.error(
                "Команда перезапуска завершилась с ошибкой %s: %s",
                process.returncode,
                error_output,
            )
            raise RuntimeError(
                error_output
                or "Команда перезапуска завершилась с ненулевым кодом возврата"
            )
        command_output = stdout.decode().strip()
        if not command_output:
            command_output = "Команда перезапуска выполнена успешно."
        logger.info("Перезапуск с помощью команды завершён успешно")
        return command_output

    if _env_flag("BOT_AUTO_RESTART", default=False):
        try:
            delay = float(os.getenv("BOT_AUTO_RESTART_DELAY", "5"))
        except ValueError:
            delay = 5.0
        if delay < 0:
            delay = 0
        logger.info(
            "Настроен автоматический перезапуск после обновления через %s секунд",
            delay,
        )
        asyncio.create_task(_schedule_process_restart(delay))
        return "Запланирован автоматический перезапуск бота."

    logger.info(
        "Переменные BOT_RESTART_COMMAND и BOT_AUTO_RESTART не заданы, перезапуск пропущен"
    )
    return None


async def _notify_recipe_added(
    recipe_name: str,
    *,
    output_quantity: Decimal,
    component_count: int,
    is_temporary: bool = False,
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


@bot.event
async def setup_hook() -> None:
    logger.info("Запуск setup_hook: подключаюсь к базе данных")
    await database.connect()
    logger.info("Подключение к базе данных завершено")
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
    await _send_restart_log("\n".join(log_lines))


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
        attachment_text = await _read_attachment_content(file)
        table_text = attachment_text
    if table_text is None:
        await interaction.followup.send(
            "Не найден текст рецепта. Отправьте таблицу в поле команды или приложите файл.",
            ephemeral=False,
        )
        return

    try:
        components = _parse_recipe_table(table_text)
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
                f"Подтверждение доступно в канале <#{RECIPE_FEED_CHANNEL_ID}>.",
            ]
        ),
        ephemeral=False,
    )

    await _notify_recipe_added(
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
                f"Стоимость цикла: {run_cost:,.2f}",
                f"Стоимость единицы: {unit_cost:,.2f}",
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
        result = await _pull_latest_code()
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
        restart_message = await _restart_service_if_configured()
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
        await _send_restart_log("\n".join(restart_log_lines))

    logger.info("Команда update_bot завершилась успешно")
    response_lines = ["Успешно обновлено из GitHub. Итог:", result]
    if restart_message:
        response_lines.extend(["", restart_message])
    await interaction.followup.send("\n".join(response_lines), ephemeral=False)


async def _run_bot(token: str) -> None:
    logger.info("Запускаю бота")
    try:
        async with bot:
            await bot.start(token)
    finally:
        logger.info("Останавливаю бота и закрываю соединение с базой данных")
        await database.close()


def main() -> None:
    env_file = Path(__file__).resolve().parent / ".env"
    _load_env_file(env_file)

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

    if _env_flag("DISCORD_MESSAGE_CONTENT_INTENT", default=True):
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
        asyncio.run(initialise_database(database.path))
    else:
        logger.info("Файл базы данных найден, инициализация не требуется")

    try:
        asyncio.run(_run_bot(token))
    except KeyboardInterrupt:
        logger.info("Остановка бота по сигналу KeyboardInterrupt")


if __name__ == "__main__":
    main()

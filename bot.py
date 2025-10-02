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

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

database = Database()


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
    for line in lines:
        normalised = line.replace("\u200b", "")  # remove zero-width spaces from Discord tables
        if "названия" in normalised.lower():
            continue
        if "id" == normalised.lower():
            continue

        parts = [part.strip() for part in splitter.split(normalised) if part.strip()]

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


async def _read_attachment_content(message: discord.Message) -> Optional[str]:
    logger.debug("Проверяю наличие вложений в сообщении %s", message.id)
    if not message.attachments:
        logger.debug("В сообщении %s вложений не найдено", message.id)
        return None
    attachment = message.attachments[0]
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


@bot.event
async def setup_hook() -> None:
    logger.info("Запуск setup_hook: подключаюсь к базе данных")
    await database.connect()
    logger.info("Подключение к базе данных завершено")


@bot.command(name="add_recipe")
@commands.has_permissions(manage_guild=True)
async def add_recipe_command(
    ctx: commands.Context,
    recipe_name: str,
    output_quantity: Optional[int] = 1,
    *,
    raw_table: Optional[str] = None,
) -> None:
    """Добавляет или обновляет рецепт."""

    logger.info(
        "Получена команда add_recipe: пользователь=%s, рецепт=%s, количество=%s",
        ctx.author,
        recipe_name,
        output_quantity,
    )
    if output_quantity is None:
        output_quantity = 1
    if output_quantity <= 0:
        await ctx.send("Количество результата должно быть положительным")
        return

    table_text = raw_table
    if table_text is None:
        attachment_text = await _read_attachment_content(ctx.message)
        table_text = attachment_text
    if table_text is None:
        await ctx.send(
            "Не найден текст рецепта. Отправьте таблицу в сообщении или приложите текстовый файл."
        )
        return

    try:
        components = _parse_recipe_table(table_text)
        await database.add_recipe(
            name=recipe_name,
            output_quantity=Decimal(output_quantity),
            components=components,
        )
    except ValueError as exc:
        await ctx.send(f"Ошибка разбора рецепта: {exc}")
        return
    except Exception as exc:  # pragma: no cover - safety net for discord command context
        logging.exception("Unexpected error while adding recipe")
        await ctx.send(f"Произошла непредвиденная ошибка: {exc}")
        return

    logger.info(
        "Рецепт '%s' успешно сохранён, обновлено %s ресурсов", recipe_name, len(components)
    )
    await ctx.send(f"Рецепт '{recipe_name}' успешно сохранён. Обновлены цены {len(components)} ресурсов.")


@bot.command(name="price")
async def recipe_price_command(
    ctx: commands.Context,
    recipe_name: str,
    efficiency: Optional[float] = None,
) -> None:
    """Рассчитывает стоимость рецепта с учётом эффективности."""

    efficiency_decimal: Optional[Decimal]
    logger.info(
        "Получена команда price: пользователь=%s, рецепт=%s, эффективность=%s",
        ctx.author,
        recipe_name,
        efficiency,
    )
    if efficiency is None:
        efficiency_decimal = None
    else:
        try:
            efficiency_decimal = parse_decimal(str(efficiency))
        except ValueError:
            await ctx.send("Эффективность должна быть числом")
            return

    try:
        result = await database.calculate_recipe_cost(recipe_name, efficiency_decimal)
    except RecipeNotFoundError:
        await ctx.send(f"Рецепт '{recipe_name}' не найден")
        return
    except ResourcePriceNotFoundError as exc:
        await ctx.send(str(exc))
        return
    except CircularRecipeReferenceError as exc:
        await ctx.send(str(exc))
        return
    except ValueError as exc:
        await ctx.send(str(exc))
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
    await ctx.send(
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


@bot.command(name="resource_price")
async def resource_price_command(ctx: commands.Context, *, resource_name: str) -> None:
    """Показывает последнюю сохранённую цену ресурса."""

    logger.info(
        "Получена команда resource_price: пользователь=%s, ресурс=%s",
        ctx.author,
        resource_name,
    )
    price = await database.get_resource_unit_price(resource_name)
    if price is None:
        await ctx.send(f"Цена для ресурса '{resource_name}' не найдена")
        return
    logger.info("Цена для ресурса '%s' составила %s", resource_name, price)
    await ctx.send(f"Текущая цена '{resource_name}': {price:,.2f}")


@bot.command(name="set_efficiency")
@commands.has_permissions(administrator=True)
async def set_efficiency_command(ctx: commands.Context, value: float) -> None:
    """Устанавливает глобальную эффективность по умолчанию."""

    logger.info(
        "Получена команда set_efficiency: пользователь=%s, значение=%s",
        ctx.author,
        value,
    )
    try:
        efficiency = parse_decimal(str(value))
    except ValueError:
        await ctx.send("Эффективность должна быть числом")
        return
    if efficiency <= 0:
        await ctx.send("Эффективность должна быть положительной")
        return

    await database.set_global_efficiency(efficiency)
    logger.info("Установлена глобальная эффективность: %s", efficiency)
    await ctx.send(f"Глобальная эффективность установлена на {efficiency}%")


@bot.command(name="global_efficiency")
async def global_efficiency_command(ctx: commands.Context) -> None:
    """Показывает текущую глобальную эффективность."""

    logger.info(
        "Получена команда global_efficiency: пользователь=%s", ctx.author
    )
    value = await database.get_global_efficiency()
    logger.info("Текущая глобальная эффективность: %s", value)
    await ctx.send(f"Текущая глобальная эффективность: {value}%")


@bot.command(name="update_bot")
@commands.has_permissions(administrator=True)
async def update_bot_command(ctx: commands.Context) -> None:
    """Обновляет код бота из GitHub репозитория."""

    logger.info("Получена команда update_bot от пользователя %s", ctx.author)
    status_message = await ctx.send("Запускаю обновление из GitHub...")
    try:
        result = await _pull_latest_code()
    except FileNotFoundError:
        await status_message.edit(content="Git не установлен на сервере")
        return
    except RuntimeError as exc:
        message = f"Не удалось обновить бота: {exc}"
        if len(message) > 1900:
            message = message[:1900] + "…"
        logger.warning("Обновление кода завершилось с ошибкой: %s", exc)
        await status_message.edit(content=message)
        return

    if len(result) > 1900:
        result = result[:1900] + "…"
    logger.info("Команда update_bot завершилась успешно")
    await status_message.edit(
        content="Успешно обновлено из GitHub. Итог:\n" + result
    )


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

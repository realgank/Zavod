import asyncio
import logging
import os
import re
from asyncio import subprocess
from decimal import Decimal
from typing import Optional

import discord
from discord.ext import commands

from database import (
    CircularRecipeReferenceError,
    Database,
    RecipeComponent,
    RecipeNotFoundError,
    ResourcePriceNotFoundError,
    parse_decimal,
)

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

database = Database()


def _parse_recipe_table(raw_table: str) -> list[RecipeComponent]:
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
        components.append(RecipeComponent(resource_name, quantity, unit_price))
    if not components:
        raise ValueError("Не удалось найти ни одной строки с компонентами рецепта")
    return components


async def _read_attachment_content(message: discord.Message) -> Optional[str]:
    if not message.attachments:
        return None
    attachment = message.attachments[0]
    if attachment.size > 5 * 1024 * 1024:
        raise ValueError("Превышен максимальный размер вложения (5 МБ)")
    data = await attachment.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Не удалось декодировать вложение как UTF-8 текст") from exc


async def _pull_latest_code() -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "pull",
        "--ff-only",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        error_output = stderr.decode().strip() or stdout.decode().strip()
        raise RuntimeError(error_output or "Не удалось выполнить git pull")
    output = stdout.decode().strip()
    if not output:
        output = "Изменений нет"
    return output


@bot.event
async def setup_hook() -> None:
    await database.connect()


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

    await ctx.send(f"Рецепт '{recipe_name}' успешно сохранён. Обновлены цены {len(components)} ресурсов.")


@bot.command(name="price")
async def recipe_price_command(
    ctx: commands.Context,
    recipe_name: str,
    efficiency: Optional[float] = None,
) -> None:
    """Рассчитывает стоимость рецепта с учётом эффективности."""

    efficiency_decimal: Optional[Decimal]
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

    price = await database.get_resource_unit_price(resource_name)
    if price is None:
        await ctx.send(f"Цена для ресурса '{resource_name}' не найдена")
        return
    await ctx.send(f"Текущая цена '{resource_name}': {price:,.2f}")


@bot.command(name="set_efficiency")
@commands.has_permissions(administrator=True)
async def set_efficiency_command(ctx: commands.Context, value: float) -> None:
    """Устанавливает глобальную эффективность по умолчанию."""

    try:
        efficiency = parse_decimal(str(value))
    except ValueError:
        await ctx.send("Эффективность должна быть числом")
        return
    if efficiency <= 0:
        await ctx.send("Эффективность должна быть положительной")
        return

    await database.set_global_efficiency(efficiency)
    await ctx.send(f"Глобальная эффективность установлена на {efficiency}%")


@bot.command(name="global_efficiency")
async def global_efficiency_command(ctx: commands.Context) -> None:
    """Показывает текущую глобальную эффективность."""

    value = await database.get_global_efficiency()
    await ctx.send(f"Текущая глобальная эффективность: {value}%")


@bot.command(name="update_bot")
@commands.has_permissions(administrator=True)
async def update_bot_command(ctx: commands.Context) -> None:
    """Обновляет код бота из GitHub репозитория."""

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
        await status_message.edit(content=message)
        return

    if len(result) > 1900:
        result = result[:1900] + "…"
    await status_message.edit(
        content="Успешно обновлено из GitHub. Итог:\n" + result
    )


async def _run_bot(token: str) -> None:
    try:
        async with bot:
            await bot.start(token)
    finally:
        await database.close()


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан токен Discord. Установите переменную окружения DISCORD_TOKEN."
        )

    try:
        asyncio.run(_run_bot(token))
    except KeyboardInterrupt:
        logging.info("Остановка бота")


if __name__ == "__main__":
    main()

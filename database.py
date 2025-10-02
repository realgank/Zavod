import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecipeComponent:
    resource_name: str
    quantity: Decimal
    unit_price: Decimal


class RecipeNotFoundError(RuntimeError):
    """Raised when a requested recipe does not exist."""


class ResourcePriceNotFoundError(RuntimeError):
    """Raised when a base price for a resource is missing."""


class CircularRecipeReferenceError(RuntimeError):
    """Raised when recipes reference each other in a cycle."""


class Database:
    def __init__(self, path: str = "zavod.db") -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        """Return the filesystem path of the SQLite database."""

        return self._path

    async def connect(self) -> None:
        if self._conn is not None:
            logger.debug("Подключение к базе данных уже установлено")
            return
        logger.info("Открываю подключение к базе данных по пути %s", self._path)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        await self._initialise_schema(conn)
        await conn.commit()
        self._conn = conn
        logger.info("Подключение к базе данных установлено")

    async def close(self) -> None:
        if self._conn is not None:
            logger.info("Закрываю подключение к базе данных")
            await self._conn.close()
            self._conn = None

    async def get_statistics(self) -> dict[str, int]:
        """Возвращает агрегированную статистику по базе данных."""

        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.debug("Собираю статистику по базе данных")
            cursor = await self._conn.execute("SELECT COUNT(*) AS count FROM recipes")
            recipe_row = await cursor.fetchone()
            await cursor.close()

            cursor = await self._conn.execute("SELECT COUNT(*) AS count FROM resources")
            resource_row = await cursor.fetchone()
            await cursor.close()

            cursor = await self._conn.execute(
                "SELECT COUNT(*) AS count FROM recipe_components"
            )
            components_row = await cursor.fetchone()
            await cursor.close()

            stats = {
                "recipes": int(recipe_row["count"] if recipe_row else 0),
                "resources": int(resource_row["count"] if resource_row else 0),
                "recipe_components": int(
                    components_row["count"] if components_row else 0
                ),
            }
            logger.info(
                "Статистика базы данных: рецептов=%s, ресурсов=%s, компонентов=%s",
                stats["recipes"],
                stats["resources"],
                stats["recipe_components"],
            )
            return stats

    async def _initialise_schema(self, conn: aiosqlite.Connection) -> None:
        logger.debug("Проверяю схему базы данных")
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                unit_price REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                output_quantity REAL NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS recipe_components (
                recipe_id INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Ensure global efficiency entry exists.
        await conn.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
            ("global_efficiency", "100"),
        )
        logger.debug("Проверка схемы завершена")

    async def add_recipe(
        self,
        name: str,
        output_quantity: Decimal,
        components: Iterable[RecipeComponent],
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info("Сохраняю рецепт '%s'", name)
            cursor = await self._conn.execute(
                "SELECT id FROM recipes WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                logger.debug("Рецепт '%s' не найден, создаю новую запись", name)
                cursor = await self._conn.execute(
                    "INSERT INTO recipes(name, output_quantity) VALUES(?, ?)",
                    (name, float(output_quantity)),
                )
                recipe_id = cursor.lastrowid
                await cursor.close()
            else:
                recipe_id = row["id"]
                logger.debug(
                    "Рецепт '%s' найден (id=%s), обновляю существующую запись", name, recipe_id
                )
                await self._conn.execute(
                    "UPDATE recipes SET output_quantity = ? WHERE id = ?",
                    (float(output_quantity), recipe_id),
                )
                await self._conn.execute(
                    "DELETE FROM recipe_components WHERE recipe_id = ?",
                    (recipe_id,),
                )

            for component in components:
                logger.debug(
                    "Добавляю компонент рецепта: рецепт=%s ресурс=%s количество=%s цена=%s",
                    name,
                    component.resource_name,
                    component.quantity,
                    component.unit_price,
                )
                await self._conn.execute(
                    """
                    INSERT INTO recipe_components(recipe_id, resource_name, quantity)
                    VALUES(?, ?, ?)
                    """,
                    (recipe_id, component.resource_name, float(component.quantity)),
                )
                await self._conn.execute(
                    """
                    INSERT INTO resources(name, unit_price)
                    VALUES(?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        unit_price = excluded.unit_price,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (component.resource_name, float(component.unit_price)),
                )

            await self._conn.commit()
            logger.info("Рецепт '%s' сохранён", name)

    async def get_recipe(self, name: str) -> Optional[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.debug("Получаю рецепт '%s'", name)

        cursor = await self._conn.execute(
            "SELECT id, name, output_quantity FROM recipes WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            logger.debug("Рецепт '%s' не найден", name)
            return None

        cursor = await self._conn.execute(
            """
            SELECT resource_name, quantity
            FROM recipe_components
            WHERE recipe_id = ?
            ORDER BY resource_name
            """,
            (row["id"],),
        )
        components = [dict(resource_name=r["resource_name"], quantity=r["quantity"]) for r in await cursor.fetchall()]
        await cursor.close()
        recipe_data = {
            "id": row["id"],
            "name": row["name"],
            "output_quantity": row["output_quantity"],
            "components": components,
        }
        logger.debug(
            "Рецепт '%s' получен: выход=%s, компонентов=%s",
            name,
            recipe_data["output_quantity"],
            len(components),
        )
        return recipe_data

    async def get_resource_unit_price(self, name: str) -> Optional[float]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.debug("Запрашиваю цену ресурса '%s'", name)

        cursor = await self._conn.execute(
            "SELECT unit_price FROM resources WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            logger.info("Цена для ресурса '%s' не найдена", name)
            return None
        unit_price = row["unit_price"]
        logger.info("Получена цена ресурса '%s': %s", name, unit_price)
        return unit_price

    async def set_global_efficiency(self, efficiency: Decimal) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.info("Устанавливаю глобальную эффективность %s", efficiency)
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO config(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("global_efficiency", str(efficiency)),
            )
            await self._conn.commit()
            logger.debug("Глобальная эффективность обновлена в базе данных")

    async def get_global_efficiency(self) -> Decimal:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.debug("Получаю значение глобальной эффективности")
        cursor = await self._conn.execute(
            "SELECT value FROM config WHERE key = ?",
            ("global_efficiency",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            logger.warning(
                "Значение глобальной эффективности отсутствует в таблице config, используется значение по умолчанию"
            )
            return Decimal("100")
        try:
            value = Decimal(row["value"])
            logger.debug("Получено значение глобальной эффективности %s", value)
            return value
        except (InvalidOperation, TypeError):
            logger.error(
                "Не удалось преобразовать значение глобальной эффективности '%s', используется значение по умолчанию",
                row["value"],
            )
            return Decimal("100")

    async def calculate_recipe_cost(
        self,
        recipe_name: str,
        efficiency: Optional[Decimal] = None,
    ) -> dict[str, Decimal]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        base_recipe = await self.get_recipe(recipe_name)
        if base_recipe is None:
            raise RecipeNotFoundError(f"Recipe '{recipe_name}' is not defined")

        if efficiency is None:
            efficiency = await self.get_global_efficiency()
        if efficiency <= 0:
            raise ValueError("Efficiency must be greater than 0")

        multiplier = Decimal("100") / efficiency
        logger.info(
            "Рассчитываю стоимость рецепта '%s' с эффективностью %s", recipe_name, efficiency
        )

        async def resource_cost(resource_name: str, visiting: set[str]) -> Decimal:
            if resource_name in visiting:
                raise CircularRecipeReferenceError(
                    f"Circular reference detected for resource '{resource_name}'"
                )
            nested_recipe = await self.get_recipe(resource_name)
            if nested_recipe is not None:
                logger.debug(
                    "Ресурс '%s' является рецептом, рассчитываю стоимость вложенного рецепта",
                    resource_name,
                )
                visiting.add(resource_name)
                cost_per_run = await recipe_cost(nested_recipe, visiting)
                visiting.remove(resource_name)
                output_quantity = Decimal(str(nested_recipe["output_quantity"]))
                if output_quantity <= 0:
                    raise ValueError(
                        f"Recipe '{resource_name}' must have positive output quantity"
                    )
                return cost_per_run / output_quantity

            price = await self.get_resource_unit_price(resource_name)
            if price is None:
                raise ResourcePriceNotFoundError(
                    f"No price registered for resource '{resource_name}'"
                )
            logger.debug(
                "Используется сохранённая цена ресурса '%s': %s",
                resource_name,
                price,
            )
            return Decimal(str(price))

        async def recipe_cost(recipe: dict[str, Any], visiting: set[str]) -> Decimal:
            logger.debug(
                "Начинаю расчёт стоимости рецепта '%s' для %s компонентов",
                recipe["name"],
                len(recipe["components"]),
            )
            total = Decimal("0")
            for component in recipe["components"]:
                component_quantity = Decimal(str(component["quantity"])) * multiplier
                component_cost = await resource_cost(component["resource_name"], visiting)
                total += component_quantity * component_cost
                logger.debug(
                    "Компонент '%s': количество=%s, цена=%s, промежуточная сумма=%s",
                    component["resource_name"],
                    component_quantity,
                    component_cost,
                    total,
                )
            return total

        total_run_cost = await recipe_cost(base_recipe, {recipe_name})
        output_quantity = Decimal(str(base_recipe["output_quantity"]))
        unit_cost = total_run_cost / output_quantity
        logger.info(
            "Стоимость рецепта '%s': цикл=%s, единица=%s", recipe_name, total_run_cost, unit_cost
        )
        logger.debug(
            "Финальный расчёт рецепта '%s': эффективность=%s, количество=%s, стоимость цикла=%s, стоимость единицы=%s",
            recipe_name,
            efficiency,
            output_quantity,
            total_run_cost,
            unit_cost,
        )
        return {
            "efficiency": efficiency,
            "run_cost": total_run_cost,
            "unit_cost": unit_cost,
            "output_quantity": output_quantity,
        }


def parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse decimal value from '{value}'") from exc


async def initialise_database(path: str = "zavod.db") -> None:
    """Ensure that the SQLite database file and schema exist.

    This helper is useful when the application is launched outside of Discord's
    lifecycle and the database file may not yet be present on disk.
    """

    path_obj = Path(path)
    if path_obj.parent and not path_obj.parent.exists():
        logger.info("Создаю директорию для базы данных: %s", path_obj.parent)
        path_obj.parent.mkdir(parents=True, exist_ok=True)

    db = Database(str(path_obj))
    try:
        logger.info("Инициализирую базу данных по пути %s", path_obj)
        await db.connect()
    finally:
        await db.close()
        logger.info("Инициализация базы данных завершена")


if __name__ == "__main__":
    asyncio.run(initialise_database())

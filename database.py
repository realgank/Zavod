import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

logger = logging.getLogger(__name__)


def _escape_like(text: str) -> str:
    """Escape characters with special meaning in LIKE patterns."""

    return (
        text.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


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


Migration = Callable[[aiosqlite.Connection], Awaitable[None]]


async def _migration_1_initialise_schema_version(conn: aiosqlite.Connection) -> None:
    """Initial migration that establishes schema version tracking."""

    logger.info("Выполняю миграцию схемы #1: инициализация версии схемы")
    # Baseline migration does not need to modify existing tables because the
    # schema is created in ``_initialise_schema`` using idempotent statements.
    # The presence of this migration ensures that older installations receive a
    # schema version entry in the config table.


async def _migration_2_add_recipe_status(conn: aiosqlite.Connection) -> None:
    """Add the ``is_temporary`` flag to recipes."""

    logger.info(
        "Выполняю миграцию схемы #2: добавление признака временного рецепта"
    )
    cursor = await conn.execute("PRAGMA table_info(recipes)")
    columns = [row["name"] for row in await cursor.fetchall()]
    await cursor.close()
    if "is_temporary" in columns:
        logger.info("Столбец is_temporary уже существует, миграция пропущена")
        return
    await conn.execute(
        "ALTER TABLE recipes ADD COLUMN is_temporary INTEGER NOT NULL DEFAULT 0"
    )


async def _migration_3_add_ship_type_support(conn: aiosqlite.Connection) -> None:
    """Add support for ship types and their efficiencies."""

    logger.info(
        "Выполняю миграцию схемы #3: поддержка типов кораблей и их эффективности"
    )
    cursor = await conn.execute("PRAGMA table_info(recipes)")
    columns = [row["name"] for row in await cursor.fetchall()]
    await cursor.close()
    if "ship_type" not in columns:
        logger.info("Добавляю столбец ship_type в таблицу recipes")
        await conn.execute("ALTER TABLE recipes ADD COLUMN ship_type TEXT")
    else:
        logger.info("Столбец ship_type уже существует, пропускаю добавление")

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ship_type_efficiencies (
            type TEXT PRIMARY KEY,
            efficiency REAL NOT NULL
        )
        """
    )


async def _migration_4_add_recipe_cost_fields(conn: aiosqlite.Connection) -> None:
    """Add blueprint and creation cost fields to recipes."""

    logger.info(
        "Выполняю миграцию схемы #4: добавление стоимостей чертежа и создания"
    )
    cursor = await conn.execute("PRAGMA table_info(recipes)")
    columns = [row["name"] for row in await cursor.fetchall()]
    await cursor.close()

    if "blueprint_cost" not in columns:
        logger.info("Добавляю столбец blueprint_cost в таблицу recipes")
        await conn.execute("ALTER TABLE recipes ADD COLUMN blueprint_cost REAL")
    else:
        logger.info(
            "Столбец blueprint_cost уже существует, пропускаю добавление"
        )

    if "creation_cost" not in columns:
        logger.info("Добавляю столбец creation_cost в таблицу recipes")
        await conn.execute("ALTER TABLE recipes ADD COLUMN creation_cost REAL")
    else:
        logger.info(
            "Столбец creation_cost уже существует, пропускаю добавление"
        )


async def _migration_5_add_blueprint_components_table(
    conn: aiosqlite.Connection,
) -> None:
    """Introduce a table for blueprint resource requirements."""

    logger.info(
        "Выполняю миграцию схемы #5: создание таблицы компонентов чертежей"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_blueprint_components (
            recipe_id INTEGER NOT NULL,
            resource_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
        )
        """
    )


async def _migration_6_add_blueprint_creation_cost(
    conn: aiosqlite.Connection,
) -> None:
    """Add separate storage for blueprint creation cost."""

    logger.info(
        "Выполняю миграцию схемы #6: добавление стоимости создания чертежа",
    )
    cursor = await conn.execute("PRAGMA table_info(recipes)")
    columns = [row["name"] for row in await cursor.fetchall()]
    await cursor.close()
    if "blueprint_creation_cost" in columns:
        logger.info(
            "Столбец blueprint_creation_cost уже существует, миграция пропущена",
        )
        return
    await conn.execute(
        "ALTER TABLE recipes ADD COLUMN blueprint_creation_cost REAL",
    )


MIGRATIONS: dict[int, Migration] = {
    1: _migration_1_initialise_schema_version,
    2: _migration_2_add_recipe_status,
    3: _migration_3_add_ship_type_support,
    4: _migration_4_add_recipe_cost_fields,
    5: _migration_5_add_blueprint_components_table,
    6: _migration_6_add_blueprint_creation_cost,
}

CURRENT_SCHEMA_VERSION = max(MIGRATIONS.keys(), default=0)


class Database:
    def __init__(self, path: str = "zavod.db") -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        """Return the filesystem path of the SQLite database."""

        return self._path

    def set_path(self, path: str) -> None:
        """Update the path used for future database connections."""

        if self._conn is not None:
            raise RuntimeError("Cannot change database path while connected")
        logger.info("Обновляю путь к базе данных: %s", path)
        self._path = path

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

    async def get_schema_version(self) -> int:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        return await self._get_schema_version(self._conn)

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
                output_quantity REAL NOT NULL DEFAULT 1,
                is_temporary INTEGER NOT NULL DEFAULT 0,
                ship_type TEXT,
                blueprint_cost REAL,
                creation_cost REAL,
                blueprint_creation_cost REAL
            );

            CREATE TABLE IF NOT EXISTS recipe_components (
                recipe_id INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recipe_blueprint_components (
                recipe_id INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ship_type_efficiencies (
                type TEXT PRIMARY KEY,
                efficiency REAL NOT NULL
            );
            """
        )
        # Ensure global efficiency entry exists.
        await conn.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
            ("global_efficiency", "100"),
        )
        await self._run_migrations(conn)
        logger.debug("Проверка схемы завершена")

    async def _get_schema_version(self, conn: aiosqlite.Connection) -> int:
        cursor = await conn.execute(
            "SELECT value FROM config WHERE key = ?",
            ("schema_version",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return 0
        raw_value = row["value"]
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                "Невалидное значение версии схемы '%s', будет использоваться 0",
                raw_value,
            )
            return 0

    async def _set_schema_version(self, conn: aiosqlite.Connection, version: int) -> None:
        await conn.execute(
            """
            INSERT INTO config(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("schema_version", str(version)),
        )

    async def _run_migrations(self, conn: aiosqlite.Connection) -> None:
        current_version = await self._get_schema_version(conn)
        if current_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "Database schema version %s is newer than supported version %s"
                % (current_version, CURRENT_SCHEMA_VERSION)
            )
        if current_version == CURRENT_SCHEMA_VERSION:
            logger.debug(
                "Версия схемы базы данных (%s) актуальна",
                current_version,
            )
            return

        logger.info(
            "Обновляю схему базы данных с версии %s до %s",
            current_version,
            CURRENT_SCHEMA_VERSION,
        )
        for next_version in range(current_version + 1, CURRENT_SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(next_version)
            if migration is None:
                raise RuntimeError(
                    f"No migration available for schema version {next_version}"
                )
            logger.debug("Применяю миграцию #%s", next_version)
            await migration(conn)
            await self._set_schema_version(conn, next_version)

        logger.info(
            "Схема базы данных обновлена до версии %s",
            CURRENT_SCHEMA_VERSION,
        )

    async def add_recipe(
        self,
        name: str,
        output_quantity: Decimal,
        components: Iterable[RecipeComponent],
        *,
        is_temporary: bool = False,
        ship_type: Optional[str] = None,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info("Сохраняю рецепт '%s'", name)
            temporary_flag = 1 if is_temporary else 0
            normalised_ship_type = (ship_type or "").strip() or None
            cursor = await self._conn.execute(
                "SELECT id FROM recipes WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                logger.debug("Рецепт '%s' не найден, создаю новую запись", name)
                cursor = await self._conn.execute(
                    """
                    INSERT INTO recipes(name, output_quantity, is_temporary, ship_type)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        name,
                        float(output_quantity),
                        temporary_flag,
                        normalised_ship_type,
                    ),
                )
                recipe_id = cursor.lastrowid
                await cursor.close()
            else:
                recipe_id = row["id"]
                logger.debug(
                    "Рецепт '%s' найден (id=%s), обновляю существующую запись", name, recipe_id
                )
                await self._conn.execute(
                    """
                    UPDATE recipes
                    SET output_quantity = ?, is_temporary = ?, ship_type = ?
                    WHERE id = ?
                    """,
                    (
                        float(output_quantity),
                        temporary_flag,
                        normalised_ship_type,
                        recipe_id,
                    ),
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

    async def set_recipe_temporary(self, name: str, is_temporary: bool) -> bool:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info(
                "Обновляю статус временного рецепта '%s': %s",
                name,
                is_temporary,
            )
            cursor = await self._conn.execute(
                "UPDATE recipes SET is_temporary = ? WHERE name = ?",
                (1 if is_temporary else 0, name),
            )
            await self._conn.commit()
            updated = cursor.rowcount > 0
            await cursor.close()
            if not updated:
                logger.warning(
                    "Рецепт '%s' не найден при обновлении статуса временности",
                    name,
                )
            return updated

    async def delete_recipe(self, name: str) -> bool:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info("Удаляю рецепт '%s'", name)
            cursor = await self._conn.execute(
                "DELETE FROM recipes WHERE name = ?",
                (name,),
            )
            await self._conn.commit()
            deleted = cursor.rowcount > 0
            await cursor.close()
            if deleted:
                logger.info("Рецепт '%s' удалён", name)
            else:
                logger.warning("Рецепт '%s' не найден для удаления", name)
            return deleted

    async def set_recipe_blueprint_cost(
        self, name: str, cost: Optional[Decimal]
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info(
                "Обновляю стоимость чертежа рецепта '%s': %s", name, cost
            )
            cursor = await self._conn.execute(
                "UPDATE recipes SET blueprint_cost = ? WHERE name = ?",
                (float(cost) if cost is not None else None, name),
            )
            await self._conn.commit()
            updated = cursor.rowcount > 0
            await cursor.close()
            if not updated:
                logger.warning(
                    "Не удалось обновить стоимость чертежа: рецепт '%s' не найден",
                    name,
                )
                raise RecipeNotFoundError(
                    f"Recipe '{name}' is not defined"
                )

    async def set_recipe_blueprint_creation_cost(
        self, name: str, cost: Optional[Decimal]
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info(
                "Обновляю стоимость создания чертежа рецепта '%s': %s",
                name,
                cost,
            )
            cursor = await self._conn.execute(
                "UPDATE recipes SET blueprint_creation_cost = ? WHERE name = ?",
                (float(cost) if cost is not None else None, name),
            )
            await self._conn.commit()
            updated = cursor.rowcount > 0
            await cursor.close()
            if not updated:
                logger.warning(
                    "Не удалось обновить стоимость создания чертежа: рецепт '%s' не найден",
                    name,
                )
                raise RecipeNotFoundError(
                    f"Recipe '{name}' is not defined"
                )

    async def set_recipe_creation_cost(
        self, name: str, cost: Optional[Decimal]
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            logger.info(
                "Обновляю цену создания рецепта '%s': %s", name, cost
            )
            cursor = await self._conn.execute(
                "UPDATE recipes SET creation_cost = ? WHERE name = ?",
                (float(cost) if cost is not None else None, name),
            )
            await self._conn.commit()
            updated = cursor.rowcount > 0
            await cursor.close()
            if not updated:
                logger.warning(
                    "Не удалось обновить цену создания: рецепт '%s' не найден",
                    name,
                )
                raise RecipeNotFoundError(
                    f"Recipe '{name}' is not defined"
                )

    async def set_recipe_blueprint_components(
        self, name: str, components: Iterable[RecipeComponent]
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            materialised_components = list(components)
            logger.info(
                "Обновляю компоненты чертежа для рецепта '%s' (%s компонентов)",
                name,
                len(materialised_components),
            )

            cursor = await self._conn.execute(
                "SELECT id FROM recipes WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                logger.warning(
                    "Не удалось обновить компоненты чертежа: рецепт '%s' не найден",
                    name,
                )
                raise RecipeNotFoundError(f"Recipe '{name}' is not defined")

            recipe_id = row["id"]
            await self._conn.execute(
                "DELETE FROM recipe_blueprint_components WHERE recipe_id = ?",
                (recipe_id,),
            )

            for component in materialised_components:
                logger.debug(
                    "Добавляю компонент чертежа: рецепт=%s ресурс=%s количество=%s цена=%s",
                    name,
                    component.resource_name,
                    component.quantity,
                    component.unit_price,
                )
                await self._conn.execute(
                    """
                    INSERT INTO recipe_blueprint_components(
                        recipe_id,
                        resource_name,
                        quantity
                    )
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
            logger.info(
                "Компоненты чертежа для рецепта '%s' обновлены (%s штук)",
                name,
                len(materialised_components),
            )

    async def get_recipe(self, name: str) -> Optional[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.debug("Получаю рецепт '%s'", name)

        cursor = await self._conn.execute(
            """
            SELECT
                id,
                name,
                output_quantity,
                is_temporary,
                ship_type,
                blueprint_cost,
                creation_cost,
                blueprint_creation_cost
            FROM recipes
            WHERE name = ?
            """,
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
        cursor = await self._conn.execute(
            """
            SELECT resource_name, quantity
            FROM recipe_blueprint_components
            WHERE recipe_id = ?
            ORDER BY resource_name
            """,
            (row["id"],),
        )
        blueprint_components = [
            dict(resource_name=r["resource_name"], quantity=r["quantity"])
            for r in await cursor.fetchall()
        ]
        await cursor.close()
        recipe_data = {
            "id": row["id"],
            "name": row["name"],
            "output_quantity": row["output_quantity"],
            "is_temporary": bool(row["is_temporary"]),
            "ship_type": (row["ship_type"] or None),
            "blueprint_cost": row["blueprint_cost"],
            "creation_cost": row["creation_cost"],
            "blueprint_creation_cost": row["blueprint_creation_cost"],
            "components": components,
            "blueprint_components": blueprint_components,
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

    async def search_resource_names(
        self, query: str = "", *, limit: int = 25
    ) -> list[str]:
        """Возвращает список ресурсов, совпадающих с запросом."""

        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        if limit <= 0:
            return []

        normalised_query = query.strip()
        pattern = f"%{_escape_like(normalised_query)}%"

        logger.debug(
            "Ищу ресурсы по запросу '%s' (ограничение %s)",
            normalised_query,
            limit,
        )

        cursor = await self._conn.execute(
            """
            SELECT name
            FROM resources
            WHERE name LIKE ? ESCAPE '\\'
            ORDER BY name COLLATE NOCASE
            LIMIT ?
            """,
            (pattern, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        names = [row["name"] for row in rows]
        logger.debug(
            "Найдено %s ресурсов по запросу '%s'", len(names), normalised_query
        )
        return names

    async def search_recipe_names(
        self, query: str = "", *, limit: int = 25
    ) -> list[str]:
        """Возвращает список рецептов, совпадающих с запросом."""

        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        if limit <= 0:
            return []

        normalised_query = query.strip()
        pattern = f"%{_escape_like(normalised_query)}%"

        logger.debug(
            "Ищу рецепты по запросу '%s' (ограничение %s)",
            normalised_query,
            limit,
        )

        cursor = await self._conn.execute(
            """
            SELECT name
            FROM recipes
            WHERE name LIKE ? ESCAPE '\\'
            ORDER BY name COLLATE NOCASE
            LIMIT ?
            """,
            (pattern, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        names = [row["name"] for row in rows]
        logger.debug(
            "Найдено %s рецептов по запросу '%s'", len(names), normalised_query
        )
        return names

    async def get_all_recipe_names(self) -> list[str]:
        """Возвращает полный список сохранённых рецептов в алфавитном порядке."""

        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            """
            SELECT name
            FROM recipes
            ORDER BY name COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

        names = [row["name"] for row in rows]
        logger.debug("Получено %s рецептов для списка кораблей", len(names))
        return names

    async def get_known_ship_types(self) -> list[str]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        types: set[str] = set()

        cursor = await self._conn.execute(
            "SELECT type FROM ship_type_efficiencies ORDER BY type COLLATE NOCASE"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            value = (row["type"] or "").strip()
            if value:
                types.add(value)

        cursor = await self._conn.execute(
            """
            SELECT DISTINCT ship_type
            FROM recipes
            WHERE ship_type IS NOT NULL AND TRIM(ship_type) <> ''
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            value = (row["ship_type"] or "").strip()
            if value:
                types.add(value)

        result = sorted(types, key=lambda item: item.lower())
        logger.debug("Найдены типы кораблей: %s", result)
        return result

    async def list_ship_type_efficiencies(self) -> dict[str, Decimal]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            """
            SELECT type, efficiency
            FROM ship_type_efficiencies
            ORDER BY type COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

        efficiencies: dict[str, Decimal] = {}
        for row in rows:
            raw_type = (row["type"] or "").strip()
            if not raw_type:
                continue
            efficiencies[raw_type] = Decimal(str(row["efficiency"]))

        logger.debug(
            "Получено %s настроек эффективности типов кораблей", len(efficiencies)
        )
        return efficiencies

    async def set_ship_type_efficiency(
        self, ship_type: str, efficiency: Decimal
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        normalised_type = ship_type.strip()
        if not normalised_type:
            raise ValueError("Ship type must not be empty")

        async with self._lock:
            logger.info(
                "Сохраняю эффективность %s для типа корабля '%s'",
                efficiency,
                normalised_type,
            )
            await self._conn.execute(
                """
                INSERT INTO ship_type_efficiencies(type, efficiency)
                VALUES(?, ?)
                ON CONFLICT(type) DO UPDATE SET efficiency = excluded.efficiency
                """,
                (normalised_type, float(efficiency)),
            )
            await self._conn.commit()

    async def get_ship_type_efficiency(
        self, ship_type: str
    ) -> Optional[Decimal]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            "SELECT efficiency FROM ship_type_efficiencies WHERE type = ?",
            (ship_type.strip(),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Decimal(str(row["efficiency"]))

    async def delete_ship_type_efficiency(self, ship_type: str) -> bool:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            cursor = await self._conn.execute(
                "DELETE FROM ship_type_efficiencies WHERE type = ?",
                (ship_type.strip(),),
            )
            await self._conn.commit()
            deleted = cursor.rowcount > 0
            await cursor.close()
            if deleted:
                logger.info("Удалена эффективность для типа корабля '%s'", ship_type)
            else:
                logger.info(
                    "Эффективность для типа корабля '%s' не найдена при удалении",
                    ship_type,
                )
            return deleted

    async def get_ship_type_statistics(self) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            """
            SELECT ship_type, COUNT(*) AS recipe_count
            FROM recipes
            GROUP BY ship_type
            ORDER BY ship_type IS NULL, ship_type COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

        stats = [
            {
                "ship_type": (row["ship_type"] or None),
                "recipe_count": int(row["recipe_count"] or 0),
            }
            for row in rows
        ]
        logger.debug(
            "Получена статистика по типам кораблей: %s", stats
        )
        return stats

    async def get_recipes_without_type(self) -> list[str]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            """
            SELECT name
            FROM recipes
            WHERE ship_type IS NULL OR TRIM(ship_type) = ''
            ORDER BY name COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        names = [row["name"] for row in rows]
        logger.debug("Найдено %s рецептов без указания типа", len(names))
        return names

    async def set_config_value(self, key: str, value: str) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO config(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await self._conn.commit()

    async def get_config_value(self, key: str) -> Optional[str]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        cursor = await self._conn.execute(
            "SELECT value FROM config WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return str(row["value"])

    async def pop_config_value(self, key: str) -> Optional[str]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT value FROM config WHERE key = ?",
                (key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            await self._conn.execute("DELETE FROM config WHERE key = ?", (key,))
            await self._conn.commit()
            return str(row["value"])

    async def set_global_efficiency(self, efficiency: Decimal) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.info("Устанавливаю глобальную эффективность %s", efficiency)
        await self.set_config_value("global_efficiency", str(efficiency))
        logger.debug("Глобальная эффективность обновлена в базе данных")

    async def get_global_efficiency(self) -> Decimal:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        logger.debug("Получаю значение глобальной эффективности")
        value_raw = await self.get_config_value("global_efficiency")
        if value_raw is None:
            logger.warning(
                "Значение глобальной эффективности отсутствует в таблице config, используется значение по умолчанию"
            )
            return Decimal("100")
        try:
            value = Decimal(value_raw)
            logger.debug("Получено значение глобальной эффективности %s", value)
            return value
        except (InvalidOperation, TypeError):
            logger.error(
                "Не удалось преобразовать значение глобальной эффективности '%s', используется значение по умолчанию",
                value_raw,
            )
            return Decimal("100")

    async def calculate_recipe_cost(
        self,
        recipe_name: str,
        efficiency: Optional[Decimal] = None,
    ) -> dict[str, Any]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        base_recipe = await self.get_recipe(recipe_name)
        if base_recipe is None:
            raise RecipeNotFoundError(f"Recipe '{recipe_name}' is not defined")

        efficiency_source = "custom"
        if efficiency is None:
            ship_type = base_recipe.get("ship_type")
            if ship_type:
                type_efficiency = await self.get_ship_type_efficiency(ship_type)
                if type_efficiency is not None:
                    efficiency = type_efficiency
                    efficiency_source = "ship_type"
            if efficiency is None:
                efficiency = await self.get_global_efficiency()
                efficiency_source = "global"
        else:
            ship_type = base_recipe.get("ship_type")

        if efficiency <= 0:
            raise ValueError("Efficiency must be greater than 0")

        multiplier = efficiency / Decimal("100")
        logger.info(
            "Рассчитываю стоимость рецепта '%s' с эффективностью %s", recipe_name, efficiency
        )

        async def resource_cost(
            resource_name: str,
            visiting: set[str],
            quantity_multiplier: Decimal,
        ) -> tuple[Decimal, dict[str, tuple[Decimal, Decimal]]]:
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
                cost_per_run, nested_breakdown = await recipe_cost(
                    nested_recipe,
                    visiting,
                    quantity_multiplier,
                )
                visiting.remove(resource_name)
                output_quantity = Decimal(str(nested_recipe["output_quantity"]))
                if output_quantity <= 0:
                    raise ValueError(
                        f"Recipe '{resource_name}' must have positive output quantity"
                    )
                unit_cost = cost_per_run / output_quantity
                aggregated_per_unit = {
                    base_name: (quantity / output_quantity, unit_price)
                    for base_name, (quantity, unit_price) in nested_breakdown.items()
                }
                return unit_cost, aggregated_per_unit

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
            unit_price = Decimal(str(price))
            return unit_price, {resource_name: (Decimal("1"), unit_price)}

        async def recipe_cost(
            recipe: dict[str, Any],
            visiting: set[str],
            quantity_multiplier: Decimal,
        ) -> tuple[Decimal, dict[str, tuple[Decimal, Decimal]]]:
            logger.debug(
                "Начинаю расчёт стоимости рецепта '%s' для %s компонентов",
                recipe["name"],
                len(recipe["components"]),
            )
            total = Decimal("0")
            breakdown: dict[str, tuple[Decimal, Decimal]] = {}
            for component in recipe["components"]:
                component_quantity = (
                    Decimal(str(component["quantity"])) * quantity_multiplier
                )
                (
                    component_cost,
                    component_breakdown,
                ) = await resource_cost(
                    component["resource_name"],
                    visiting,
                    quantity_multiplier,
                )
                total_cost = component_quantity * component_cost
                total += total_cost
                logger.debug(
                    "Компонент '%s': количество=%s, цена=%s, промежуточная сумма=%s",
                    component["resource_name"],
                    component_quantity,
                    component_cost,
                    total,
                )
                for base_name, (quantity_per_unit, unit_price) in component_breakdown.items():
                    total_quantity = quantity_per_unit * component_quantity
                    if base_name in breakdown:
                        current_quantity, _ = breakdown[base_name]
                        breakdown[base_name] = (
                            current_quantity + total_quantity,
                            unit_price,
                        )
                    else:
                        breakdown[base_name] = (total_quantity, unit_price)
            return total, breakdown

        total_run_cost, aggregated_breakdown = await recipe_cost(
            base_recipe,
            {recipe_name},
            multiplier,
        )
        output_quantity = Decimal(str(base_recipe["output_quantity"]))
        unit_cost = total_run_cost / output_quantity
        blueprint_components_data = base_recipe.get("blueprint_components") or []
        blueprint_components_cost = Decimal("0")
        blueprint_aggregated_breakdown: dict[str, tuple[Decimal, Decimal]] = {}
        if blueprint_components_data:
            blueprint_recipe = {
                "name": f"{recipe_name} (чертеж)",
                "components": blueprint_components_data,
                "output_quantity": Decimal("1"),
            }
            blueprint_components_cost, blueprint_aggregated_breakdown = await recipe_cost(
                blueprint_recipe,
                {recipe_name},
                Decimal("1"),
            )
        raw_blueprint_cost = base_recipe.get("blueprint_cost")
        raw_creation_cost = base_recipe.get("creation_cost")
        raw_blueprint_creation_cost = base_recipe.get("blueprint_creation_cost")
        blueprint_cost: Optional[Decimal]
        creation_cost: Optional[Decimal]
        blueprint_creation_cost: Optional[Decimal]
        if raw_blueprint_cost is None:
            blueprint_cost = None
        else:
            blueprint_cost = Decimal(str(raw_blueprint_cost))
        if raw_creation_cost is None:
            creation_cost = None
        else:
            creation_cost = Decimal(str(raw_creation_cost))
        if raw_blueprint_creation_cost is None:
            blueprint_creation_cost = None
        else:
            blueprint_creation_cost = Decimal(str(raw_blueprint_creation_cost))
        total_with_additions = total_run_cost + blueprint_components_cost
        if blueprint_cost is not None:
            total_with_additions += blueprint_cost
        if creation_cost is not None:
            total_with_additions += creation_cost
        if blueprint_creation_cost is not None:
            total_with_additions += blueprint_creation_cost
        if output_quantity > 0:
            unit_cost_with_additions = total_with_additions / output_quantity
        else:
            unit_cost_with_additions = total_with_additions
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
        components_breakdown = [
            {
                "resource_name": name,
                "quantity": quantity,
                "unit_cost": unit_price,
                "total_cost": unit_price * quantity,
            }
            for name, (quantity, unit_price) in sorted(aggregated_breakdown.items())
        ]
        blueprint_components_breakdown = [
            {
                "resource_name": name,
                "quantity": quantity,
                "unit_cost": unit_price,
                "total_cost": unit_price * quantity,
            }
            for name, (quantity, unit_price) in sorted(
                blueprint_aggregated_breakdown.items()
            )
        ]
        return {
            "efficiency": efficiency,
            "run_cost": total_run_cost,
            "unit_cost": unit_cost,
            "output_quantity": output_quantity,
            "components": components_breakdown,
            "ship_type": base_recipe.get("ship_type"),
            "efficiency_source": efficiency_source,
            "blueprint_cost": blueprint_cost,
            "creation_cost": creation_cost,
            "blueprint_creation_cost": blueprint_creation_cost,
            "total_with_additions": total_with_additions,
            "unit_cost_with_additions": unit_cost_with_additions,
            "blueprint_components": blueprint_components_breakdown,
            "blueprint_components_cost": blueprint_components_cost,
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

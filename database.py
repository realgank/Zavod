import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import aiosqlite


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

    async def connect(self) -> None:
        if self._conn is not None:
            return
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        await self._initialise_schema(conn)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _initialise_schema(self, conn: aiosqlite.Connection) -> None:
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

    async def add_recipe(
        self,
        name: str,
        output_quantity: Decimal,
        components: Iterable[RecipeComponent],
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT id FROM recipes WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                cursor = await self._conn.execute(
                    "INSERT INTO recipes(name, output_quantity) VALUES(?, ?)",
                    (name, float(output_quantity)),
                )
                recipe_id = cursor.lastrowid
                await cursor.close()
            else:
                recipe_id = row["id"]
                await self._conn.execute(
                    "UPDATE recipes SET output_quantity = ? WHERE id = ?",
                    (float(output_quantity), recipe_id),
                )
                await self._conn.execute(
                    "DELETE FROM recipe_components WHERE recipe_id = ?",
                    (recipe_id,),
                )

            for component in components:
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

    async def get_recipe(self, name: str) -> Optional[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            "SELECT id, name, output_quantity FROM recipes WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
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
        return {
            "id": row["id"],
            "name": row["name"],
            "output_quantity": row["output_quantity"],
            "components": components,
        }

    async def get_resource_unit_price(self, name: str) -> Optional[float]:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")

        cursor = await self._conn.execute(
            "SELECT unit_price FROM resources WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return row["unit_price"]

    async def set_global_efficiency(self, efficiency: Decimal) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO config(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("global_efficiency", str(efficiency)),
            )
            await self._conn.commit()

    async def get_global_efficiency(self) -> Decimal:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialised")
        cursor = await self._conn.execute(
            "SELECT value FROM config WHERE key = ?",
            ("global_efficiency",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return Decimal("100")
        try:
            return Decimal(row["value"])
        except (InvalidOperation, TypeError):
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

        async def resource_cost(resource_name: str, visiting: set[str]) -> Decimal:
            if resource_name in visiting:
                raise CircularRecipeReferenceError(
                    f"Circular reference detected for resource '{resource_name}'"
                )
            nested_recipe = await self.get_recipe(resource_name)
            if nested_recipe is not None:
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
            return Decimal(str(price))

        async def recipe_cost(recipe: dict[str, Any], visiting: set[str]) -> Decimal:
            total = Decimal("0")
            for component in recipe["components"]:
                component_quantity = Decimal(str(component["quantity"])) * multiplier
                component_cost = await resource_cost(component["resource_name"], visiting)
                total += component_quantity * component_cost
            return total

        total_run_cost = await recipe_cost(base_recipe, {recipe_name})
        output_quantity = Decimal(str(base_recipe["output_quantity"]))
        unit_cost = total_run_cost / output_quantity
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

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

database = Database()

__all__ = ["bot", "database", "intents"]

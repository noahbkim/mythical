import disnake
from disnake.ext import commands
from disnake.ext import tasks
import requests

import sqlite3


class FaceitCog(commands.Cog):
    """Commands for managing rating notifications."""

    # database: RaiderDatabase

    def __init__(self, bot: commands.Bot, key: str):
        """Initialize the notifications cog with a database and messager."""

        # self.database = database
        self.bot = bot
        self.key = key
        # self.loop_update.start()
        # self.loop_clean.start()

    @commands.command(name="faceit:rating", aliases=["faceit:elo", "faceit:r"])
    async def command_rating(self, context: commands.Context, username: str):
        """Get the current Faceit ELO via username."""

        response = requests.get(
            f"https://open.faceit.com/data/v4/players?nickname={username}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )

        if response.status_code != 200:
            await context.send(f"couldn't find player with nickname {username}!")
            return

        data = response.json()
        level = data["games"]["csgo"]["skill_level"]
        elo = data["games"]["csgo"]["faceit_elo"]
        await context.send(f"{username} is level {level} with {elo} elo")

import disnake
from disnake.ext import commands
from disnake.ext import tasks
import requests

import re
import sqlite3


class ValorantCog(commands.Cog):
    """Commands for managing rating notifications."""

    # database: RaiderDatabase

    def __init__(self, bot: commands.Bot, username: str, password: str):
        """Initialize the notifications cog with a database and messager."""

        # self.database = database
        self.bot = bot
        self.username = username
        self.password = password
        # self.loop_update.start()
        # self.loop_clean.start()

    @commands.command(name="valorant:rating", aliases=["valorant:elo", "valorant:r"])
    async def command_rating(self, context: commands.Context, region: str, username: str):
        """Get the current Faceit ELO via username."""

        if username.count("#") != 1:
            await context.send(f"invalid username {username}!")
            return

        nickname, tagline = username.split("#")
        response = requests.get(f"https://api.henrikdev.xyz/valorant/v1/mmr/{region}/{nickname}/{tagline}")

        if response.status_code != 200:
            await context.send(f"error making request!")
            return

        data = response.json()
        rank = data["data"]["currenttierpatched"]
        rr = data["data"]["ranking_in_tier"]
        if rank is None:
            await context.send(f"the API seems to be bugged, try again later!")
            return

        await context.send(f"{username} is {rank} with {rr} rr")

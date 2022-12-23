import disnake
from disnake.ext import tasks
import requests

import random
import datetime
import configparser
import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..tracker import Tracker, Player
from ..bot import BotPlugin, BotError, get_member


#
# @dataclass
# class ValorantPlayer(Player):
#     """This is a convenience class that mirrors the database record.
#
#     The dataclass decorator generates a constructor along with some
#     other object-related methods for convenience.
#     """
#
#     nickname: str
#     rank: int
#     elo: int
#
#     class Meta:
#         fields = ("nickname", "rank", "elo")
#         schema = (
#             " VARCHAR",
#             "rank VARCHAR",
#             "elo INTEGER",
#             "UNIQUE (nickname)",
#         )
#
#
# class FaceitTracker(Tracker[FaceitPlayer]):
#     """High level database access for raider.io commands.
#
#     Abstracts away all the SQL queries we need to persist the state of
#     our notification bot. Requires a sqlite3 connection; it's not our
#     responsibility to create the database file, only to use it.
#     """
#
#     class Meta:
#         model = FaceitPlayer
#
#     def set_level(self, player_id: int, level: int, elo: int):
#         """Update a player's rating."""
#
#         with self.connection:
#             cursor = self.connection.cursor()
#             cursor.execute(
#                 f"UPDATE {self._players} SET (level, elo)=(?, ?) WHERE id=?",
#                 (level, elo, player_id),
#             )


class ValorantPlugin(BotPlugin):
    """Provide subcommands related to Faceit API."""

    # tracker: FaceitTracker
    key: str

    def __init__(self, connection: sqlite3.Connection):
        """Set command handlers."""

        # self.tracker = FaceitTracker(connection, prefix="valorant")
        self.commands = {
            "r": self.command_rating,
            "rr": self.command_rating,
            "rating": self.command_rating,
            "elo": self.command_rating,
        }

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            region, username = parts
        else:
            raise BotError("expected `region` and `username`")  #, `username`, or `server member`!")

        if username.count("#") != 1:
            await message.reply(f"invalid username {username}!")
            return

        nickname, tagline = username.split("#")
        response = requests.get(f"https://api.henrikdev.xyz/valorant/v1/mmr/{region}/{nickname}/{tagline}")

        if response.status_code != 200:
            await message.channel.send(f"error making request!")
            return

        data = response.json()
        rank = data["data"]["currenttierpatched"]
        rr = data["data"]["elo"]
        rr_mod = data["data"]["ranking_in_tier"]

        if rank is None:
            await message.reply(f"the API seems to be bugged, try again later!")
            return

        await message.channel.send(f"{username} is {rank} with {rr_mod} rr")

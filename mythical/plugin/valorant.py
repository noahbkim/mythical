import disnake
from disnake.ext import tasks
import requests

import random
import datetime
import configparser
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple

from ..tracker import Tracker, Player
from ..bot import BotPlugin, BotError, get_member


def get_valorant_account(name: str, tag: str) -> dict:
    """Get by name and tag; has region and puid."""

    response = requests.get(f"https://api.henrikdev.xyz/valorant/v1/account/{name}/{tag}")
    if response.status_code != 200:
        raise BotError(f"couldn't find player {name}#{tag}")
    return response.json()


def get_valorant_rank(region: str, nickname: str, tag: str) -> dict:
    """Access free API."""

    response = requests.get(f"https://api.henrikdev.xyz/valorant/v1/mmr/{region}/{nickname}/{tag}")
    if response.status_code != 200:
        raise BotError(f"couldn't find player {nickname}#{tag}!")
    return response.json()


@dataclass
class ValorantPlayer(Player):
    """This is a convenience class that mirrors the database record.

    The dataclass decorator generates a constructor along with some
    other object-related methods for convenience.
    """

    region: str
    name: str
    tag: str
    riot_id: str
    rank: str
    rr: int
    rr_mod: int

    @property
    def username(self) -> str:
        return f"{self.name}#{self.tag}"

    class Meta:
        fields = ("region", "name", "tag", "riot_id", "rank", "rr", "rr_mod")
        schema = (
            "region VARCHAR NOT NULL",
            "name VARCHAR NOT NULL",
            "tag VARCHAR NOT NULL",
            "riot_id VARCHAR NOT NULL",
            "rank VARCHAR NOT NULL",
            "rr INTEGER NOT NULL",
            "rr_mod INTEGER NOT NULL",
            "UNIQUE (riot_id)",
        )


class ValorantTracker(Tracker[ValorantPlayer]):
    """High level database access for raider.io commands.

    Abstracts away all the SQL queries we need to persist the state of
    our notification bot. Requires a sqlite3 connection; it's not our
    responsibility to create the database file, only to use it.
    """

    class Meta:
        model = ValorantPlayer

    def set_level(self, player_id: int, rank: str, rr: int, rr_mod: int):
        """Update a player's rating."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                f"UPDATE {self._players} SET (rank, rr, rr_mod)=(?, ?, ?) WHERE id=?",
                (rank, rr, rr_mod, player_id),
            )


def parse_username(text: str) -> Tuple[str, str]:
    """Split into name and tag."""

    if text.count("#") != 1:
        raise BotError(f"invalid username {text}!")
    name, tag = text.split("#")
    return name, tag


class ValorantPlugin(BotPlugin):
    """Provide subcommands related to Faceit API."""

    tracker: ValorantTracker
    key: str

    def __init__(self, connection: sqlite3.Connection):
        """Set command handlers."""

        self.tracker = ValorantTracker(connection, prefix="valorant")
        self.commands = {
            "r": self.command_rating,
            "rr": self.command_rating,
            "rating": self.command_rating,
            "elo": self.command_rating,
            "add": self.command_add,
            "remove": self.command_remove,
            "l": self.command_leaderboard,
            "leaderboard": self.command_leaderboard,
            "here": self.command_here,
        }

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            region, username = parts
        else:
            raise BotError("expected `region` and `username`")  #, `username`, or `server member`!")

        name, tag = parse_username(username)
        data = get_valorant_rank(region, name, tag)
        rank = data["data"]["currenttierpatched"]
        rr = data["data"]["elo"]
        rr_mod = data["data"]["ranking_in_tier"]

        if rank is None:
            await message.reply(f"the API seems to be bugged, try again later!")
            return

        await message.channel.send(f"{username} is {rank} with {rr_mod} rr")

    async def command_add(self, text: str, message: disnake.Message):
        """Start spectating a user."""

        parts = text.split(maxsplit=3)
        if len(parts) == 1:
            username = parts[0]
            user_id = None
        elif len(parts) == 2:
            username, last = parts
            user_id = get_member(last, message).id
        else:
            raise BotError("expected `region`, `username`, and optional `server member`!")

        self.tracker.set_channel_if_unset(message.guild.id, message.channel.id)

        name, tag = parse_username(username)
        account_data = get_valorant_account(name, tag)
        region = account_data["data"]["region"]
        riot_id = account_data["data"]["puuid"]

        player = self.tracker.get_player(riot_id=riot_id)
        if player is None:
            data = get_valorant_rank(region, name, tag)
            rank = data["data"]["currenttierpatched"]
            rr = data["data"]["elo"]
            rr_mod = data["data"]["ranking_in_tier"]
            player = self.tracker.create_player(
                region=region,
                name=name,
                tag=tag,
                riot_id=riot_id,
                rank=rank,
                rr=rr,
                rr_mod=rr_mod
            )

        created = self.tracker.create_spectator(message.guild.id, player.id, user_id)
        action = "Started watching" if created else "Already watching"
        await message.channel.send(f"{action} {player.username} ({player.rank}, {player.rr_mod} rr)")

    async def command_remove(self, text: str, message: disnake.Message):
        """Stop spectating a user."""

        name, tag = parse_username(text)
        account_data = get_valorant_account(name, tag)
        riot_id = account_data["data"]["puuid"]

        player = self.tracker.get_player(riot_id=riot_id)
        if player is None:
            raise BotError(f"couldn't find player {text}!")

        deleted = self.tracker.delete_spectator(message.guild.id, player.id)
        action = "Stopped watching" if deleted else "Wasn't watching"
        await message.channel.send(f"{action} {player.name}")

    async def command_leaderboard(self, text: str, message: disnake.Message):
        """List players watched by the guild in order of rating."""

        players = list(self.tracker.get_players_spectated_by_guild(message.guild.id))
        players.sort(key=lambda item: item.player.rr, reverse=True)

        lines = []
        for i, item in enumerate(players, start=1):
            line = f"{i}. {item.player.username}, {item.player.rank}, {item.player.rr_mod} rr"
            if item.user_id is not None:
                member = message.guild.get_member(item.user_id)
                if member is not None:
                    line += f" ({member.name})"
            lines.append(line)

        embed = disnake.Embed(
            title="Valorant Leaderboard",
            description="\n".join(lines) or "It's a little bit empty in here...",
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        # Allow the footer to be empty, in which case we don't set it
        if players:
            first = players[0].player
            embed.set_footer(text=random.choice(
                (
                    f"{first.username} needs to go outside",
                    f"{first.username} should probably touch grass",
                    f"{first.username} might need to take a break",
                    f"{first.username} hasn't showered in days",
                    f"{first.username} is losing their grip",
                    f"{first.username} definitely isn't short",
                    f"Somebody should check on {first.username}",
                    f"I can smell {first.username} from here",
                )
            ))

        await message.channel.send(embed=embed)

    async def command_here(self, text: str, message: disnake.Message):
        """Set the notification channel for this plugin."""

        self.tracker.set_channel(message.guild.id, message.channel.id)
        await message.channel.send("Valorant notifications will be posted to this channel!")

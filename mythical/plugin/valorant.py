import disnake
from disnake.ext import tasks
import requests

import random
import datetime
import sqlite3
from dataclasses import dataclass
from typing import Tuple

from ..tracker import Tracker, Player
from ..bot import BotPlugin, BotError, get_member, try_get_member


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


def get_valorant_rank_by_riot_id(region: str, riot_id: str) -> dict:
    """Access free API."""

    response = requests.get(f"https://api.henrikdev.xyz/valorant/v1/by-puuid/mmr/{region}/{riot_id}")
    if response.status_code != 200:
        raise BotError(f"couldn't find player {riot_id}!")
    return response.json()


def get_valorant_match_history(region: str, riot_id: str) -> dict:
    """Get last 5 matches."""

    response = requests.get(
        f"https://api.henrikdev.xyz/valorant/v3/by-puuid/matches/{region}/{riot_id}?filter=competitive"
    )

    if response.status_code != 200:
        raise BotError(f"couldn't find player {riot_id}!")
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

    @tasks.loop(minutes=5)
    async def update(self):
        """Update all players, notify if new rating."""

        for player in self.tracker.get_spectated_players():
            data = get_valorant_rank_by_riot_id(player.region, player.riot_id)
            await self.update_player(player, data)

    async def ready(self, client: disnake.Client):
        """Start background tasks."""

        await super().ready(client)
        if not self.update.is_running():
            self.update.start()

    async def update_player(self, player: ValorantPlayer, data: dict):
        """Update a player's level and elo and notify."""

        new_rank = data["data"]["currenttierpatched"]
        new_rr = data["data"]["elo"]
        new_rr_mod = data["data"]["ranking_in_tier"]

        if new_rr != player.rr or True:
            self.tracker.set_level(player.id, new_rank, new_rr, new_rr_mod)

            account_data = get_valorant_account(data["data"]["name"], data["data"]["tag"])

            description = []
            last_matches = get_valorant_match_history(player.region, player.riot_id)
            last_match = last_matches["data"][0]
            map_name = last_match["metadata"]["map"]
            kills = 0
            deaths = 0
            assists = 0
            score = 0
            headshots = 0
            damage = 0
            team = "Red"
            character = ""
            for player_data in last_match["players"]["all_players"]:
                if player_data["puuid"] == player.riot_id:
                    team = player_data["team"].lower()
                    score = player_data["stats"]["score"]
                    kills = player_data["stats"]["kills"]
                    deaths = player_data["stats"]["deaths"]
                    assists = player_data["stats"]["assists"]
                    headshots = player_data["stats"]["headshots"]
                    damage = player_data["damage_made"]
                    character = player_data["character"]

            rounds_won = last_match["teams"][team]["rounds_won"]
            rounds_lost = last_match["teams"][team]["rounds_lost"]
            rounds = rounds_won + rounds_lost
            result = "won" if rounds_won > rounds_lost else "lost" if rounds_won < rounds_lost else "tied"

            description.append(
                f"Their {result} their last match {rounds_won}:{rounds_lost} on {map_name}."
                f" They had a {kills}/{assists}/{deaths} KAD with {round(headshots / kills * 100, 1)}% HS, "
                f" {round(score / rounds, 1)} ACS, and {round(damage / rounds, 1)} ADR."
            )

            if new_rank != player.rank:
                description.append(f"They are now {new_rank}.")

            reached = (
                f"gained {new_rr - player.rr}"
                if new_rr > player.rr else
                f"lost {player.rr - new_rr}"
            )

            for item in self.tracker.get_spectator_channels(player.id):
                channel = self.client.get_channel(item.channel_id)
                if channel is None:
                    print(f"invalid channel for guild {item.guild_id}: {item.channel_id}")
                    continue

                member_name = ""
                if item.user_id is not None:
                    member = channel.guild.get_member(item.user_id)
                    if member is not None:
                        member_name = f" ({member.name})"

                embed = disnake.Embed(
                    title=f"{player.username} {reached} rr",
                    description=" ".join(description),
                    color=disnake.Colour.brand_green() if new_rr > player.rr else disnake.Colour.brand_red(),
                    timestamp=datetime.datetime.now(),
                )

                sign = "+" if new_rr >= player.rr else "-"
                embed.add_field(name="Previous", value=str(round(player.rr, 1)), inline=True)
                embed.add_field(name="Change", value=f"{sign}{round(new_rr - player.rr, 1)}", inline=True)
                embed.add_field(name="Character", value=character)
                embed.set_thumbnail(account_data["data"]["card"]["small"])

                await channel.send(embed=embed)

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            member = try_get_member(parts[0], message)
            if member is not None:
                player = self.tracker.get_player_with_user_id(message.guild.id, member.id)
                username = player.username
                riot_id = player.riot_id
                region = player.region
            else:
                username = parts[0]
                name, tag = parse_username(username)
                account_data = get_valorant_account(name, tag)
                riot_id = account_data["data"]["puuid"]
                region = account_data["data"]["region"]
            data = get_valorant_rank_by_riot_id(region, riot_id)

        elif len(parts) == 2:
            region, username = parts
            name, tag = parse_username(username)
            data = get_valorant_rank(region, name, tag)

        else:
            raise BotError("expected `username`, `server member`, or `region` and `username`")

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

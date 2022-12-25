import disnake
from disnake.ext import tasks
import requests

import datetime
import random
import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..tracker import Tracker, Player
from ..bot import BotPlugin, BotError, get_member, try_get_member


def get_all_mythic_plus_best_runs(region: str, realm: str, name: str) -> dict:
    """Query the given player for their best runs.

    Retrieves both outright best and alternate best runs. This can be
    used to compute the raider.io rating, but also includes useful
    information about the player character including their race,
    class, spec, etc.
    """

    url = (
        f"https://raider.io/api/v1/characters/profile"
        f"?region={region}"
        f"&realm={realm}"
        f"&name={name}"
        f"&fields=mythic_plus_best_runs:all,mythic_plus_alternate_runs:all,mythic_plus_recent_runs"
    )

    response = requests.get(url, headers={"Accept": "application/json"})

    # An error code likely means a provided parameter is incorrect
    if response.status_code != 200:
        raise BotError(f"received {response.status_code} error from raider.io!")

    return response.json()


def compute_mythic_plus_rating(data: dict) -> float:
    """Compute the raider.io rating given best run data."""

    score = 0
    for run in data["mythic_plus_best_runs"]:
        score += 1.5 * run["score"]
    for run in data["mythic_plus_alternate_runs"]:
        score += 0.5 * run["score"]
    return score


def describe_recent_runs(data: dict) -> Optional[str]:
    """Describe their most recent run."""

    recent_runs = data.get("mythic_plus_recent_runs")
    if recent_runs:
        run = recent_runs[0]
        dungeon = run["dungeon"]
        mythic_level = run["mythic_level"]
        clear_time = _format_time(run["clear_time_ms"])
        affixes = ", ".join(affix["name"] for affix in run["affixes"])
        return (
            f"Their most recent run was {dungeon} +{mythic_level} in {clear_time}"
            f" with affixes {affixes}."
        )
    else:
        return None


@dataclass
class RaiderPlayer(Player):
    """This is a convenience class that mirrors the database record.

    The dataclass decorator generates a constructor along with some
    other object-related methods for convenience.
    """

    region: str
    realm: str
    name: str
    rating: float

    class Meta:
        fields = ("region", "realm", "name", "rating")
        schema = (
            "region VARCHAR COLLATE NOCASE",
            "realm VARCHAR COLLATE NOCASE",
            "name VARCHAR COLLATE NOCASE",
            "rating FLOAT",
            "UNIQUE (region, realm, name)",
        )


class RaiderTracker(Tracker[RaiderPlayer]):
    """High level database access for raider.io commands.

    Abstracts away all the SQL queries we need to persist the state of
    our notification bot. Requires a sqlite3 connection; it's not our
    responsibility to create the database file, only to use it.
    """

    class Meta:
        model = RaiderPlayer

    def set_rating(self, player_id: int, rating: float):
        """Update a player's rating."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                f"UPDATE {self._players} SET rating=? WHERE id=?",
                (rating, player_id),
            )


def _format_time(ms: int) -> str:
    """Return HH:MM:SS format."""

    s = ms / 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{int(h):02}:{int(m):02}:{round(s):02}" if h > 0 else f"{int(m):02}:{round(s):02}"


def create_rating_embed(data: dict, rating: float) -> disnake.Embed:
    """Used when rating command invoked."""

    name = data["name"]

    embed = disnake.Embed(
        title=f"{name} has mythic+ rating {round(rating, 1)}",
        description=describe_recent_runs(data),
        timestamp=datetime.datetime.now(),
    )

    character = " ".join((data["gender"], data["class"], data["race"])).lower().capitalize()
    embed.add_field("Character", character, inline=True)
    embed.add_field("Spec", data["active_spec_name"], inline=True)
    embed.add_field("Raider", data["profile_url"], inline=False)
    embed.set_thumbnail(url=data["thumbnail_url"])

    return embed


class RaiderPlugin(BotPlugin):
    """Provide subcommands related to raider.io API."""

    tracker: RaiderTracker

    def __init__(self, connection: sqlite3.Connection):
        """Set command handlers."""

        self.tracker = RaiderTracker(connection, prefix="raider")
        self.commands = {
            "r": self.command_rating,
            "rating": self.command_rating,
            "add": self.command_add,
            "remove": self.command_remove,
            "l": self.command_leaderboard,
            "leaderboard": self.command_leaderboard,
            "here": self.command_here,
        }

    async def ready(self, client: disnake.Client):
        """Start background tasks."""

        await super().ready(client)
        if not self.update.is_running():
            self.update.start()

    @tasks.loop(minutes=5)
    async def update(self):
        """Update all players, notify if new rating."""

        for player in self.tracker.get_spectated_players():
            try:
                data = get_all_mythic_plus_best_runs(player.region, player.realm, player.name)
                new_rating = compute_mythic_plus_rating(data)
            except BotError as error:
                print(f"error while retrieving data for {player}: {error}")
                continue

            await self.update_player(player, new_rating, data)

    async def update_player(self, player: RaiderPlayer, new_rating: float, data: dict):
        """Update a player's rating and print their"""

        if new_rating != player.rating:
            self.tracker.set_rating(player.id, new_rating)

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
                    title=f"{player.name} reached mythic+ rating {round(new_rating, 1)}",
                    description=describe_recent_runs(data),
                    color=0x77dd77,
                    timestamp=datetime.datetime.now(),
                )

                embed.add_field(name="Previous", value=str(round(player.rating, 1)), inline=True)
                embed.add_field(name="Current", value=str(round(new_rating, 1)), inline=True)
                embed.add_field(name="Gain", value=str(round(new_rating - player.rating, 1)), inline=True)

                await channel.send(embed=embed)

    @tasks.loop(hours=24)
    async def cleanup(self):
        """Remove players that aren't spectated."""

        self.tracker.delete_players_without_spectator()

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        parts = text.split(maxsplit=2)

        # Try accessing existing player by name or Discord ID
        if len(parts) == 1:
            player = self.tracker.get_player(name=parts[0])
            if player is None:
                member = try_get_member(parts[0], message)
                if member is not None:
                    player = self.tracker.get_player_with_user_id(message.guild.id, member.id)

            if player is None:
                raise BotError("Error: failed to find matching player!")

        # Go through raider.io identifier
        elif len(parts) == 3:
            player = self.tracker.get_player(region=parts[0], realm=parts[1], name=parts[1])

            if player is None:
                data = get_all_mythic_plus_best_runs(parts[0], parts[1], parts[2])
                rating = compute_mythic_plus_rating(data)
                await message.channel.send(embed=create_rating_embed(data, rating))
                return

        else:
            raise BotError("expected either a server member or region, realm, and name!")

        data = get_all_mythic_plus_best_runs(player.region, player.realm, player.name)
        rating = compute_mythic_plus_rating(data)

        await message.channel.send(embed=create_rating_embed(data, rating))
        await self.update_player(player, rating, data)

    async def command_add(self, text: str, message: disnake.Message):
        """Start spectating a user."""

        parts = text.split(maxsplit=3)
        if len(parts) == 3:
            region, realm, name = parts
            user_id = None
        elif len(parts) == 4:
            region, realm, name, last = parts
            user_id = get_member(last, message).id
        else:
            raise BotError("expected `region`, `realm`, `name`, and optional `server member`!")

        self.tracker.set_channel_if_unset(message.guild.id, message.channel.id)

        player = self.tracker.get_player(region=region, realm=realm, name=name)
        if player is None:
            data = get_all_mythic_plus_best_runs(region, realm, name)
            rating = compute_mythic_plus_rating(data)
            player = self.tracker.create_player(
                region=data["region"],
                realm=data["realm"],
                name=data["name"],
                rating=rating,
            )

        created = self.tracker.create_spectator(message.guild.id, player.id, user_id)
        action = "Started watching" if created else "Already watching"
        await message.channel.send(f"{action} {player.name} ({round(player.rating, 1)} rating)")

    async def command_remove(self, text: str, message: disnake.Message):
        """Stop spectating a user."""

        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            raise BotError("expected `region`, `realm`, and `name`!")

        player = self.tracker.get_player(region=parts[0], realm=parts[1], name=parts[2])
        if player is None:
            raise BotError(f"couldn't find player {parts[2]}!")

        deleted = self.tracker.delete_spectator(message.guild.id, player.id)
        action = "Stopped watching" if deleted else "Wasn't watching"
        await message.channel.send(f"{action} {player.name}")

    async def command_leaderboard(self, text: str, message: disnake.Message):
        """List players watched by the guild in order of rating."""

        players = list(self.tracker.get_players_spectated_by_guild(message.guild.id))
        players.sort(key=lambda item: item.player.rating, reverse=True)

        lines = []
        for i, item in enumerate(players, start=1):
            line = f"{i}. {item.player.name}, {round(item.player.rating, 1)}"
            if item.user_id is not None:
                member = message.guild.get_member(item.user_id)
                if member is not None:
                    line += f" ({member.name})"
            lines.append(line)

        embed = disnake.Embed(
            title="Mythic+ Leaderboard",
            description="\n".join(lines) or "It's a little bit empty in here...",
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        # Allow the footer to be empty, in which case we don't set it
        if players:
            first = players[0].player
            embed.set_footer(text=random.choice(
                (
                    f"{first.name} needs to go outside",
                    f"{first.name} should probably touch grass",
                    f"{first.name} might need to take a break",
                    f"{first.name} hasn't showered in days",
                    f"{first.name} is losing their grip",
                    f"{first.name} definitely isn't short",
                    f"Somebody should check on {first.name}",
                    f"I can smell {first.name} from here",
                )
            ))

        await message.channel.send(embed=embed)

    async def command_here(self, text: str, message: disnake.Message):
        """Set the notification channel for this plugin."""

        self.tracker.set_channel(message.guild.id, message.channel.id)
        await message.channel.send("Raider notifications will be posted to this channel!")

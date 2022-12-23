import random

import disnake
from disnake.ext import commands
from disnake.ext import tasks
import requests

import datetime
import traceback
from dataclasses import dataclass

from .base import Tracker, Player


class InternalError(Exception):
    """Throw this when a bot command can't be completed.

    We'll catch it in the bot logic and respond with the message
    included in the constructor. It's better to make our own exception
    to avoid accidentally hiding other ones thrown by bugs.
    """


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
        f"&fields=mythic_plus_best_runs:all,mythic_plus_alternate_runs:all"
    )

    response = requests.get(url, headers={"Accept": "application/json"})

    # An error code likely means a provided parameter is incorrect
    if response.status_code != 200:
        raise InternalError(
            f"received {response.status_code} error from raider.io!"
            " double check the provided arguments."
        )

    return response.json()


def compute_mythic_plus_rating(data: dict) -> float:
    """Compute the raider.io rating given best run data."""

    score = 0
    for run in data["mythic_plus_best_runs"]:
        score += 1.5 * run["score"]
    for run in data["mythic_plus_alternate_runs"]:
        score += 0.5 * run["score"]
    return score


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
        fields = Player.Meta.fields + (
            "region",
            "realm",
            "name",
            "rating",
        )
        schema = Player.Meta.schema + (
            "region VARCHAR COLLATE NOCASE",
            "realm VARCHAR COLLATE NOCASE",
            "name VARCHAR COLLATE NOCASE",
            "rating FLOAT",
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
                f"UPDATE {self._players_name} SET rating=? WHERE id=?",
                (rating, player_id),
            )


class RaiderCog(commands.Cog):
    """Commands for managing rating notifications."""

    tracker: RaiderTracker

    def __init__(self, bot: commands.Bot, tracker: RaiderTracker):
        """Initialize the notifications cog with a database and messager."""

        self.tracker = tracker
        self.bot = bot
        self.loop_update.start()
        self.loop_clean.start()

    def message_add(self, player: RaiderPlayer, added: bool) -> str:
        if added:
            return f"started watching {player.name} ({round(player.rating, 1)} rating)"
        else:
            return f"already watching {player.name} ({round(player.rating, 1)} rating)"

    def message_remove(self, player: RaiderPlayer, removed: bool) -> str:
        if removed:
            return f"stopped watching {player.name}"
        else:
            return f"was not watching {player.name}"

    def message_rating(self, region: str, realm: str, name: str, rating: float) -> str:
        return f"player {name} has mythic+ rating {round(rating, 1)}"

    def message_rating_change(self, player: RaiderPlayer, new_rating: float, data: dict) -> str:
        return f"player {player.name} has new mythic+ rating {round(player.rating, 1)} → {round(new_rating, 1)}"

    def message_leaderboard(self, players: list[RaiderPlayer]) -> str:
        if len(players) == 0:
            return ""

        first = players[0]
        return random.choice(
            (
                f"{first.name} needs to go outside",
                f"{first.name} should probably touch grass",
                f"{first.name} might need to take a break",
                f"{first.name} hasn't showered in days",
                f"{first.name} is losing their grip",
                f"{first.name} definitely isn't short",
                f"somebody should check on {first.name}",
                f"i can smell {first.name} from here",
            )
        )

    def message_leaderboard_empty(self) -> str:
        return "it's a little bit empty in here..."

    def message_here(self) -> str:
        return "rating notifications will be posted to this channel!"

    @commands.command(name="raider:rating")
    async def command_rating(self, context: commands.Context, region: str, realm: str, name: str):
        """Query the rating directly from raider.io"""

        try:
            data = get_all_mythic_plus_best_runs(region, realm, name)
            rating = compute_mythic_plus_rating(data)
        except InternalError as error:
            await context.send(f"error: {error}")
            return

        await context.send(self.message_rating(region, realm, name, rating))

    @commands.command(name="raider:leaderboard")
    async def command_leaderboard(self, context: commands.Context):
        """List players watched by the guild in order of rating."""

        players = list(self.tracker.get_players_watched_by_guild(context.guild.id))
        players.sort(key=lambda player: player.rating, reverse=True)
        leaderboard = (
            "\n".join(
                f"{i}. {player.name}: {round(player.rating, 1)}"
                for i, player in enumerate(players, start=1)
            )
        ) or self.message_leaderboard_empty()

        embed = disnake.Embed(
            title="Mythic+ Leaderboard",
            description=leaderboard,
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        # Allow the footer to be empty, in which case we don't set it
        footer = self.message_leaderboard(players)
        if footer:
            embed.set_footer(text=footer)

        await context.send(embed=embed)

    @commands.command(name="raider:add")
    async def command_add(self, context: commands.Context, region: str, realm: str, name: str):
        """Start watching a new player."""

        self.tracker.set_channel_if_unset(context.guild.id, context.channel.id)

        player = self.tracker.get_player(region=region, realm=realm, name=name)
        if player is None:
            try:
                data = get_all_mythic_plus_best_runs(region, realm, name)
                rating = compute_mythic_plus_rating(data)
            except InternalError as error:
                await context.send(f"error: {error}")
                return

            player = self.tracker.create_player(region=region, realm=realm, name=name, rating=rating)

        added = self.tracker.create_spectator(context.guild.id, player.id)
        await context.send(self.message_add(player, added))

    @commands.command(name="raider:remove")
    async def command_remove(self, context: commands.Context, region: str, realm: str, name: str):
        """Stop watching a player."""

        player = self.tracker.get_player(region=region, realm=realm, name=name)
        if player is None:
            await context.send(f"error: specified player {name} does not exist!")
            return

        removed = self.tracker.delete_spectator(context.guild.id, player.id)
        await context.send(self.message_remove(player, removed))

    @commands.command(name="raider:here")
    async def command_here(self, context: commands.Context):
        """Set notification channel."""

        self.tracker.set_channel(context.guild.id, context.channel.id)
        await context.send(self.message_here())

    async def cog_command_error(self, context: commands.Context, error: Exception):
        """Write argument and internal errors as message."""

        if isinstance(error, commands.UserInputError):
            await context.send(f"error: {error}")
            return

        else:
            formatted = "".join(traceback.format_exception(error))
            await context.send(f"```{formatted}```")

    @tasks.loop(minutes=5)
    async def loop_update(self):
        """Iterate each database entry and check if rating changed."""

        for player in self.tracker.get_watched_players():
            try:
                data = get_all_mythic_plus_best_runs(player.region, player.realm, player.name)
                new_rating = compute_mythic_plus_rating(data)
            except InternalError as error:
                print(error)
                return

            if new_rating != player.rating:
                self.tracker.set_rating(player.id, new_rating)
                message = self.message_rating_change(player, new_rating, data)
                for channel_id in self.tracker.get_channels(player.id):
                    channel = self.bot.get_channel(channel_id)
                    await channel.send(message)

    @loop_update.before_loop
    async def before_loop_update(self):
        """We don't have access to channels unless we do this."""

        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def loop_clean(self):
        """Remove players who are no longer watched by a guild."""

        self.tracker.delete_players_unwatched()

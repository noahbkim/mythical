import random

import disnake
from disnake.ext import commands
from disnake.ext import tasks
import requests

import configparser
import datetime
import sqlite3
import traceback
from dataclasses import dataclass
from typing import Optional, Iterator


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
class Player:
    id: int
    region: str
    realm: str
    name: str
    rating: float


class RaiderDatabase:
    """Wrapper around database connection."""

    connection: sqlite3.Connection

    def __init__(self, connection: sqlite3.Connection):
        """Initialize with database connection."""

        self.connection = connection
        self.populate()

    def populate(self):
        """Create necessary tables for operation."""

        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region VARCHAR COLLATE NOCASE,
                    realm VARCHAR COLLATE NOCASE,
                    name VARCHAR COLLATE NOCASE,
                    rating FLOAT,
                    UNIQUE (region, realm, name) ON CONFLICT REPLACE
                );
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS watching (
                    guild_id INTEGER,
                    player_id INTEGER,
                    FOREIGN KEY(player_id) REFERENCES players(id),
                    UNIQUE (guild_id, player_id)
                );
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    guild_id INTEGER,
                    channel_id INTEGER,
                    UNIQUE (guild_id)
                );
            """)

    def set_default_channel(self, guild_id: int, channel_id: int):
        """Set notification channel for a guild if unset.

        If a user never invokes the `here` command, the bot should
        send messages to the first channel they invoke a command in.
        """

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO channels (guild_id, channel_id) VALUES (?, ?)",
                (guild_id, channel_id),
            )

    def set_channel(self, guild_id: int, channel_id: int):
        """Set the notification channel for a guild."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                (
                    "INSERT INTO channels (guild_id, channel_id) VALUES (?, ?)"
                    "ON CONFLICT (guild_id) DO UPDATE SET channel_id=excluded.channel_id"
                ),
                (guild_id, channel_id),
            )

    def get_player(self, region: str, realm: str, name: str) -> Optional[Player]:
        """Add a user to a guild watch list.

        Creates the player record then creates the watching record.
        Returns True if both already existed.
        """

        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT id, region, realm, name, rating FROM players WHERE region=? AND realm=? AND name=?",
            (region, realm, name),
        )

        result = cursor.fetchone()
        return Player(*result) if result else None

    def get_watched_players_by_guild(self, guild_id: int = None) -> Iterator[Player]:
        """Iterate through all players watched by a guild."""

        cursor = self.connection.cursor()
        cursor.execute("""
            SELECT DISTINCT id, region, realm, name, rating
            FROM players INNER JOIN watching ON players.id=watching.player_id
            WHERE guild_id=?;
        """, (guild_id,))

        for row in cursor.fetchall():
            yield Player(*row)

    def get_watched_players(self) -> Iterator[Player]:
        """Iterate through all players being watched."""

        cursor = self.connection.cursor()
        cursor.execute("""
            SELECT DISTINCT id, region, realm, name, rating
            FROM players INNER JOIN watching ON watching.player_id=players.id;
        """)

        for row in cursor.fetchall():
            yield Player(*row)

    def get_channels(self, player_id: int) -> Iterator[int]:
        """Get all channels to notify if a player rating changes."""

        cursor = self.connection.cursor()
        cursor.execute("""
            SELECT channel_id
            FROM channels LEFT JOIN watching ON channels.guild_id=watching.guild_id 
            WHERE player_id=?;
        """, (player_id,))

        for row in cursor.fetchall():
            yield row[0]

    def add_player(self, region: str, realm: str, name: str, rating: float) -> Optional[Player]:
        """Add a player, returning the generated ID."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO players (region, realm, name, rating) VALUES (?, ?, ?, ?)",
                (region, realm, name, rating)
            )
            if cursor.rowcount > 0:
                cursor.execute("SELECT last_insert_rowid()")
                return Player(cursor.fetchone()[0], region, realm, name, rating)
            else:
                return None

    def set_rating(self, player_id: int, rating: float):
        """Update a player's rating."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "UPDATE players SET rating=? WHERE id=?",
                (rating, player_id),
            )

    def delete_unwatched_players(self) -> int:
        """Remove all player rows with no watching guilds.

        Should be called periodically to keep the size of the database
        down over the long term.
        """

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "DELETE FROM players WHERE id NOT IN (SELECT DISTINCT player_id FROM watching);"
            )
            return cursor.rowcount

    def watch_player(self, guild_id: int, player_id: int) -> bool:
        """Add a player to a guild watch list."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO watching (guild_id, player_id) VALUES (?, ?)",
                (guild_id, player_id),
            )
            return cursor.rowcount > 0

    def unwatch_player(self, guild_id: int, player_id: int) -> bool:
        """Remove a player from the guild watch list."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "DELETE FROM watching WHERE guild_id=? AND player_id=?",
                (guild_id, player_id),
            )
            return cursor.rowcount > 0


class Raider(commands.Cog):
    """Commands for managing rating notifications."""

    database: RaiderDatabase

    def __init__(self, bot: commands.Bot, database: RaiderDatabase):
        """Initialize the notifications cog with a database and messager."""

        self.database = database
        self.bot = bot
        self.loop_update.start()

    def message_add(self, player: Player, added: bool) -> str:
        if added:
            return f"started watching {player.name} ({round(player.rating, 1)} rating)"
        else:
            return f"already watching {player.name} ({round(player.rating, 1)} rating)"

    def message_remove(self, player: Player, removed: bool) -> str:
        if removed:
            return f"stopped watching {player.name}"
        else:
            return f"was not watching {player.name}"

    def message_rating(self, region: str, realm: str, name: str, rating: float) -> str:
        return f"player {name} has mythic+ rating {round(rating, 1)}"

    def message_rating_change(self, player: Player, new_rating: float, data: dict) -> str:
        return f"player {player.name} has new mythic+ rating {round(player.rating, 1)} → {round(new_rating, 1)}"

    def message_leaderboard(self, first: Player) -> str:
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

    @commands.command(name="rating")
    async def command_rating(self, context: commands.Context, region: str, realm: str, name: str):
        """Query the rating directly from raider.io"""

        try:
            data = get_all_mythic_plus_best_runs(region, realm, name)
            rating = compute_mythic_plus_rating(data)
        except InternalError as error:
            await context.send(f"error: {error}")
            return

        await context.send(self.message_rating(region, realm, name, rating))

    @commands.command(name="add")
    async def command_add(self, context: commands.Context, region: str, realm: str, name: str):
        """Start watching a new player."""

        self.database.set_default_channel(context.guild.id, context.channel.id)

        player = self.database.get_player(region, realm, name)
        if player is None:
            try:
                data = get_all_mythic_plus_best_runs(region, realm, name)
                rating = compute_mythic_plus_rating(data)
            except InternalError as error:
                await context.send(f"error: {error}")
                return

            player = self.database.add_player(region, realm, name, rating)

        added = self.database.watch_player(context.guild.id, player.id)
        await context.send(self.message_add(player, added))

    @commands.command(name="remove")
    async def command_remove(self, context: commands.Context, region: str, realm: str, name: str):
        """Stop watching a player."""

        self.database.set_default_channel(context.guild.id, context.channel.id)

        player = self.database.get_player(region, realm, name)
        if player is None:
            await context.send(f"error: specified player {name} does not exist!")
            return

        removed = self.database.unwatch_player(context.guild.id, player.id)
        await context.send(self.message_remove(player, removed))

    @commands.command(name="leaderboard")
    async def command_leaderboard(self, context: commands.Context):
        """List players watched by the guild in order of rating."""

        players = list(self.database.get_watched_players_by_guild(context.guild.id))
        players.sort(key=lambda player: player.rating, reverse=True)
        leaderboard = (
            "\n".join(
                f"{i}. {player.name}: {round(player.rating, 1)}"
                for i, player in enumerate(players, start=1)
            )
        ) or "It's a little bit empty in here..."

        embed = disnake.Embed(
            title="Server Mythic+ Leaderboard",
            description=leaderboard,
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        if len(players) > 0:
            embed.set_footer(text=self.message_leaderboard(players[0]))

        await context.send(embed=embed)

    @commands.command(name="here")
    async def command_here(self, context: commands.Context):
        """Set notification channel."""

        self.database.set_channel(context.guild.id, context.channel.id)
        await context.send("rating notifications will be posted to this channel!")

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

        for player in self.database.get_watched_players():
            try:
                data = get_all_mythic_plus_best_runs(player.region, player.realm, player.name)
                new_rating = compute_mythic_plus_rating(data)
            except InternalError as error:
                print(error)
                return

            if new_rating != player.rating:
                self.database.set_rating(player.id, new_rating)
                message = self.message_rating_change(player, new_rating, data)
                for channel_id in self.database.get_channels(player.id):
                    channel = self.bot.get_channel(channel_id)
                    await channel.send(message)

    @loop_update.before_loop
    async def before_loop_update(self):
        await self.bot.wait_until_ready()


def main():
    """Build the bot, register commands, configure, and run.

    We put this in a separate function to hide variables from the
    global scope; we call it in the __name__ check in order to prevent
    the bot from running if we're trying to import functionality from
    other Python files.
    """

    intents = disnake.Intents(messages=True, message_content=True, reactions=True, guilds=True)
    bot = commands.Bot(command_prefix="%", intents=intents)

    config = configparser.ConfigParser()
    config.read("mythical.conf")
    client_id = config["discord"]["client_id"]

    print(
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        "&permissions=274877909056"
        "&scope=bot"
    )

    bot.add_cog(Raider(bot, RaiderDatabase(sqlite3.connect("mythical.sqlite3"))))
    bot.run(config["discord"]["token"])


if __name__ == "__main__":
    main()

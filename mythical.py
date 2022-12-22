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
    """This is a convenience class that mirrors the database record.

    The dataclass decorator generates a constructor along with some
    other object-related methods for convenience.
    """

    id: int
    region: str
    realm: str
    name: str
    rating: float


class RaiderDatabase:
    """High level database access for raider.io commands.

    Abstracts away all the SQL queries we need to persist the state of
    our notification bot. Requires a sqlite3 connection; it's not our
    responsibility to create the database file, only to use it.
    """

    connection: sqlite3.Connection

    def __init__(self, connection: sqlite3.Connection):
        """Initialize with database connection and populate."""

        self.connection = connection
        self.populate()

    def populate(self):
        """Create necessary tables for operation.

        If these tables already exist, do nothing. If the tables exist
        but have an outdated schema, any resultant errors won't be
        thrown until runtime. If database migration is a concern, I'd
        recommend using something more sophisticated than sqlite3.
        """

        # This table describes each player anyone on any Discord
        # server might be watching. The region, realm, and name must
        # be unique to guarantee there are no repeats. Note that these
        # fields are also case-insensitive (per the raider.io API).
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                region VARCHAR COLLATE NOCASE,
                realm VARCHAR COLLATE NOCASE,
                name VARCHAR COLLATE NOCASE,
                rating FLOAT,
                UNIQUE (region, realm, name)
            )
        """)

        # This table links a guild (Discord server) to a player in our
        # players table, indicating that if the corresponding player
        # is updated, that guild should receive a notification. This
        # allows multiple guilds to watch a single player without
        # incurring redundant raider.io API queries.
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS watching (
                guild_id INTEGER,
                player_id INTEGER,
                FOREIGN KEY(player_id) REFERENCES players(id),
                UNIQUE (guild_id, player_id)
            )
        """)

        # This table links guilds to one of their text channels. We
        # use it to determine where player rating notifications should
        # be posted.
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                guild_id INTEGER,
                channel_id INTEGER,
                UNIQUE (guild_id)
            )
        """)

    def set_default_channel(self, guild_id: int, channel_id: int):
        """Set notification channel for a guild only if unset.

        If a user never invokes the `here` command, the bot should
        send notifications to the first channel a command is sent to.
        We can track this by calling this method every time the %add
        command is invoked; logically there will be no notifications
        until after that point.
        """

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO channels (guild_id, channel_id) VALUES (?, ?)",
                (guild_id, channel_id),
            )

    def set_channel(self, guild_id: int, channel_id: int):
        """Set the notification channel for a guild.

        This is the explicit alternative to the above which overwrites
        any previous channel.
        """

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                """
                INSERT INTO channels (guild_id, channel_id)
                VALUES (?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET channel_id=excluded.channel_id
                """,
                (guild_id, channel_id),
            )

    def get_player(self, region: str, realm: str, name: str) -> Optional[Player]:
        """Retrieve a player with their raider.io identifiers.

        As mentioned in the definition of the players table, the three
        parameter are case-insensitive.
        """

        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT id, region, realm, name, rating FROM players
            WHERE region=? AND realm=? AND name=?
            """,
            (region, realm, name),
        )

        result = cursor.fetchone()
        return Player(*result) if result else None

    def get_watched_players_by_guild(self, guild_id: int = None) -> Iterator[Player]:
        """Iterate through all players watched by a specified guild."""

        cursor = self.connection.cursor()

        # Select all players where there's at least one watch list
        # entry pointing to their id; we SELECT DISTINCT because JOIN
        # will yield a row for every watching guild.
        cursor.execute(
            """
            SELECT DISTINCT id, region, realm, name, rating
            FROM watching LEFT JOIN players ON watching.player_id=players.id
            WHERE guild_id=?;
            """,
            (guild_id,),
        )
        # For example, if you have player A watched by guilds X and Y,
        # and player B watched by nobody, you'll get two rows back:
        #
        #   A X
        #   A Y
        #
        # We only care about individual players that show up in the
        # results here.

        for row in cursor.fetchall():
            yield Player(*row)

    def get_watched_players(self) -> Iterator[Player]:
        """Iterate through all players watched by any guild."""

        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT DISTINCT id, region, realm, name, rating
            FROM watching LEFT JOIN players ON watching.player_id=players.id;
            """
        )

        for row in cursor.fetchall():
            yield Player(*row)

    def get_channels(self, player_id: int) -> Iterator[int]:
        """Get all channels we should notify of a player update."""

        cursor = self.connection.cursor()

        # Similar logic to the above, exercise for the reader.
        cursor.execute(
            """
            SELECT channel_id
            FROM channels LEFT JOIN watching ON channels.guild_id=watching.guild_id 
            WHERE player_id=?;
            """,
            (player_id,),
        )

        for row in cursor.fetchall():
            yield row[0]

    def add_player(self, region: str, realm: str, name: str, rating: float) -> Optional[Player]:
        """Add a player, returning the generated ID."""

        with self.connection:
            cursor = self.connection.cursor()

            # Update rating if we're trying to add a duplicate record
            cursor.execute(
                """
                INSERT INTO players (region, realm, name, rating)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (region, realm, name) DO UPDATE SET rating=excluded.rating
                """,
                (region, realm, name, rating),
            )

            # Get the player ID that was just inserted
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
                "DELETE FROM players WHERE id NOT IN (SELECT DISTINCT player_id FROM watching)"
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
        self.loop_clean.start()

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

    def message_leaderboard(self, players: list[Player]) -> str:
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

    @commands.command(name="here")
    async def command_here(self, context: commands.Context):
        """Set notification channel."""

        self.database.set_channel(context.guild.id, context.channel.id)
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
        """We don't have access to channels unless we do this."""

        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def loop_clean(self):
        """Remove players who are no longer watched by a guild."""

        self.database.delete_unwatched_players()


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

    # Print an add link based on configuration
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

import abc
import sqlite3
import dataclasses
from typing import Any, Generic, TypeVar, Iterator, List, Type, Tuple, Optional


@dataclasses.dataclass
class Player:
    """Base player object."""

    id: int
    discord_id: Optional[int]

    class Meta:
        fields: Tuple[str, ...] = ("id", "discord_id")
        schema: Tuple[str, ...] = (
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "discord_id INTEGER",
        )


T = TypeVar("T", bound=Player)


class Tracker(Generic[T], metaclass=abc.ABCMeta):
    """Base data for rank notifications."""

    class Meta:
        model: Type[T]

    connection: sqlite3.Connection
    prefix: str

    _players_name: str
    _spectators_name: str
    _channels_name: str

    def __init__(self, connection: sqlite3.Connection, prefix: str):
        """Set the table prefix for this app."""

        self.connection = connection
        self.prefix = prefix
        self._players_name = f"{self.prefix}_players"
        self._spectators_name = f"{self.prefix}_spectators"
        self._channels_name = f"{self.prefix}_channels"

        # Setup
        self.create_players_table()
        self.create_spectators_table()
        self.create_channels_table()

    def create_players_table(self):
        """Maintained by each subclass."""

        # This table describes each player anyone on any Discord
        # server might be watching. The region, realm, and name must
        # be unique to guarantee there are no repeats. Note that these
        # fields are also case-insensitive (per the raider.io API).
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._players_name} (
                {", ".join(self.Meta.model.Meta.schema)}
            )
            """
        )

    def create_player(self, **kwargs) -> Optional[T]:
        """Create a player, returning the generated ID."""

        with self.connection:
            cursor = self.connection.cursor()

            # id should always be first
            fields = self.Meta.model.Meta.fields[1:]
            values = tuple(kwargs[field] for field in fields)

            # Update rating if we're trying to add a duplicate record
            cursor.execute(
                f"""
                INSERT OR IGNORE INTO players ({", ".join(fields)})
                VALUES ({", ".join(("?",) * len(fields))})
                """,
                values,
            )

            # Get the player ID that was just inserted
            if cursor.rowcount > 0:
                cursor.execute("SELECT last_insert_rowid()")
                return Player(id=cursor.fetchone(), **kwargs)
            else:
                return None

    def get_player(self, **kwargs) -> Optional[T]:
        """Get the first player that matches the kwargs."""

        # Just in case we don't have ordered dictionaries
        keys, values = zip(*kwargs.items())

        cursor = self.connection.cursor()
        cursor.execute(
            f"""
            SELECT {self.Meta.model.Meta.fields} FROM {self._players_name}
            WHERE {" AND ".join(f"{key}=?" for key in keys)}
            """,
            values,
        )

        result = cursor.fetchone()
        return self.Meta.model(**dict(zip(self.Meta.model.Meta.fields, result))) if result else None

    def get_players_watched_by_guild(self, guild_id: int = None) -> Iterator[T]:
        """Iterate through all players watched by a specified guild."""

        cursor = self.connection.cursor()

        # Select all players where there's at least one watch list
        # entry pointing to their id; we SELECT DISTINCT because JOIN
        # will yield a row for every watching guild.
        cursor.execute(
            f"""
            SELECT DISTINCT {", ".join(self.Meta.model.Meta.fields)}
            FROM {self._spectators_name}
            LEFT JOIN {self._players_name} 
            ON {self._spectators_name}.player_id={self._players_name}.id
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
            yield self.Meta.model(**dict(zip(self.Meta.model.Meta.fields, row)))

    def get_watched_players(self) -> Iterator[Any]:
        """Iterate through all players watched by any guild."""

        cursor = self.connection.cursor()
        cursor.execute(
            f"""
            SELECT DISTINCT {", ".join(self.Meta.model.Meta.fields)}
            FROM {self._spectators_name} 
            LEFT JOIN {self._players_name} 
            ON {self._spectators_name}.player_id={self._players_name}.id;
            """
        )

        for row in cursor.fetchall():
            yield self.Meta.model(**dict(zip(self.Meta.model.Meta.fields, row)))

    def delete_players_unwatched(self) -> int:
        """Remove all player rows with no watching guilds.

        Should be called periodically to keep the size of the database
        down over the long term.
        """

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                f"""
                DELETE FROM {self._players_name} 
                WHERE id NOT IN (SELECT DISTINCT player_id FROM {self._spectators_name})
                """
            )
            return cursor.rowcount

    def create_spectators_table(self):
        """Matches guilds to players."""

        # This table links a guild (Discord server) to a player in our
        # players table, indicating that if the corresponding player
        # is updated, that guild should receive a notification. This
        # allows multiple guilds to watch a single player without
        # incurring redundant raider.io API queries.
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._spectators_name} (
                guild_id INTEGER,
                player_id INTEGER,
                FOREIGN KEY(player_id) REFERENCES {self._players_name}(id),
                UNIQUE (guild_id, player_id)
            )
            """
        )

    def create_spectator(self, guild_id: int, player_id: int) -> bool:
        """Add a player to a guild watch list."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                f"INSERT OR IGNORE INTO {self._spectators_name} (guild_id, player_id) VALUES (?, ?)",
                (guild_id, player_id),
            )
            return cursor.rowcount > 0

    def delete_spectator(self, guild_id: int, player_id: int) -> bool:
        """Remove a player from the guild watch list."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                f"DELETE FROM {self._spectators_name} WHERE guild_id=? AND player_id=?",
                (guild_id, player_id),
            )
            return cursor.rowcount > 0

    def create_channels_table(self):
        """Which channel to post notifications to per server."""

        # This table links guilds to one of their text channels. We
        # use it to determine where player rating notifications should
        # be posted.
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._channels_name} (
                guild_id INTEGER,
                channel_id INTEGER,
                UNIQUE (guild_id)
            )
            """
        )

    def set_channel_if_unset(self, guild_id: int, channel_id: int):
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
                f"INSERT OR IGNORE INTO {self._channels_name} (guild_id, channel_id) VALUES (?, ?)",
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
                f"""
                INSERT INTO {self._channels_name} (guild_id, channel_id)
                VALUES (?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET channel_id=excluded.channel_id
                """,
                (guild_id, channel_id),
            )

    def get_channels(self, player_id: int) -> Iterator[int]:
        """Get all channels we should notify of a player update."""

        cursor = self.connection.cursor()

        # Similar logic to the above, exercise for the reader.
        cursor.execute(
            f"""
            SELECT channel_id
            FROM {self._channels_name} 
            LEFT JOIN {self._spectators_name} 
            ON {self._channels_name}.guild_id={self._spectators_name}.guild_id 
            WHERE player_id=?;
            """,
            (player_id,),
        )

        for row in cursor.fetchall():
            yield row[0]

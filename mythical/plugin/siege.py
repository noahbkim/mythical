import configparser
import ipaddress
import sqlite3
import sys
from dataclasses import dataclass
from typing import Optional

import disnake
import siegeapi
from disnake.ext import tasks

from ..tracker import Player, Tracker
from ..bot import Bot, BotPlugin, BotError, handle_exception, get_member


@dataclass
class SiegePlayer(Player):
    """This is a convenience class that mirrors the database record.

    The dataclass decorator generates a constructor along with some
    other object-related methods for convenience.
    """

    uid: str
    name: str
    platform: str
    rank_name: str
    rank_points: int
    season_kills: int
    season_deaths: int

    class Meta:
        fields = ("uid", "name", "platform", "rank_name", "rank_points", "season_kills", "season_deaths")
        schema = (
            "uid VARCHAR COLLATE NOCASE",
            "name VARCHAR COLLATE NOCASE",
            "platform VARCHAR COLLATE NOCASE",
            "rank_name VARCHAR COLLATE NOCASE",
            "rank_points INTEGER",
            "season_kills INTEGER",
            "season_deaths INTEGER",
            "UNIQUE (uid)",
        )


class SiegeTracker(Tracker[SiegePlayer]):
    """High level database access for raider.io commands.

    Abstracts away all the SQL queries we need to persist the state of
    our notification bot. Requires a sqlite3 connection; it's not our
    responsibility to create the database file, only to use it.
    """

    class Meta:
        model = SiegePlayer


class SiegePlugin(BotPlugin):
    """Provide subcommands related to the Siege API."""

    tracker: SiegeTracker
    token: str

    def __init__(self, bot: Bot, connection: sqlite3.Connection):
        """Set command handlers."""

        super().__init__(bot)
        self.tracker = SiegeTracker(connection, prefix="siege")
        self.commands = {
            "r": self.command_rating,
            "rating": self.command_rating,
            "rank": self.command_rating,
            # "l": self.command_leaderboard,
            # "leaderboard": self.command_leaderboard,
            "add": self.command_add,
            "remove": self.command_remove,
            "here": self.command_here,
        }

    def configure(self, section: Optional[configparser.SectionProxy]) -> None:
        """Configure the plugin based on `mythical.conf`."""

        self.token = siegeapi.Auth.get_basic_token(section["email"], section["password"])

    async def on_ready(self):
        """Start background tasks."""

        if not self.update.is_running():
            self.update.start()

    @tasks.loop(minutes=15)
    @handle_exception
    async def update(self):
        """Update all players, notify if new rating."""

        auth = siegeapi.Auth(token=self.token)
        try:
            for player in self.tracker.get_spectated_players():
                player_data = await auth.get_player(uid=player.uid)
                await player_data.load_ranked_v2()
                await self.update_player(player, player_data)
        except ipaddress.AddressValueError as error:
            raise BotError(f"invalid request: {error}")
        except RecursionError as error:
            print(f"recursion error: {error}")
        finally:
            await auth.close()

    async def update_player(self, player: SiegePlayer, player_data: siegeapi.Player):
        """Update a player's level and elo and notify."""

        new_name = player_data.name
        new_rank_name = player_data.ranked_profile.rank
        new_rank_points = player_data.ranked_profile.rank_points
        new_season_kills = player_data.ranked_profile.kills
        new_season_deaths = player_data.ranked_profile.deaths

        if player.rank_points != new_rank_points:
            self.tracker.update_player(
                player.id,
                name=new_name,
                rank_name=new_rank_name,
                rank_points=new_rank_points,
                season_kills=new_season_kills,
                season_deaths=new_season_deaths,
            )

            for item in self.tracker.get_spectator_channels(player.id):
                channel = self.bot.get_channel(item.channel_id)
                if channel is None:
                    print(f"invalid channel for guild {item.guild_id}: {item.channel_id}", file=sys.stderr)
                    continue

                if item.user_id is not None:
                    member = channel.guild.get_member(item.user_id)
                    if member is not None:
                        member_name = f" ({member.name})"

                description = []

                sign = "+" if new_rank_points >= player.rank_points else "-"
                description.append(
                    f"Their current ELO is **{str(round(new_rank_points))}**"
                    f" ({sign}{round(abs(new_rank_points - player.rank_points))})."
                )

                kills = new_season_kills - player.season_kills
                deaths = new_season_deaths - player.season_deaths
                if kills >= 0 and deaths >= 0:
                    description.append(f"In their last game(s) they went {kills}/{deaths}.")

                description.append(f"They are {new_rank_name}.")

                reached = (
                    f"gained {new_rank_points - player.rank_points}"
                    if new_rank_points >= player.rank_points else
                    f"lost {player.rank_points - new_rank_points}"
                )
                embed = disnake.Embed(
                    title=f"{player.name} {reached} elo",
                    description=" ".join(description),
                    color=(
                        disnake.Colour.brand_green()
                        if new_rank_points >= player.rank_points
                        else disnake.Colour.brand_red()
                    ),
                )

                embed.set_thumbnail(player_data.profile_pic_url)
                await channel.send(embed=embed)

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        parts = text.strip().split(maxsplit=2)
        if len(parts) == 1:
            platform, name = "uplay", parts[0]
        elif len(parts) == 2:
            platform, name = parts
        else:
            raise BotError("expected `platform` and `username`!")

        auth = siegeapi.Auth(token=self.token)
        try:
            player_data = await auth.get_player(name, platform=platform)
            await player_data.load_ranked_v2()
            rank_name = player_data.ranked_profile.rank
            rank_points = player_data.ranked_profile.rank_points
        except siegeapi.InvalidRequest as error:
            raise BotError(f"invalid request: {error}")
        except ipaddress.AddressValueError as error:
            raise BotError(f"invalid request: {error}")
        finally:
            await auth.close()

        await message.channel.send(f"{player_data.name} is {rank_name} with {rank_points} elo")

        player = self.tracker.get_player(name=player_data.name)
        if player is not None:
            await self.update_player(player, player_data)

    async def command_add(self, text: str, message: disnake.Message):
        """Add a player."""

        parts = text.strip().split(maxsplit=3)
        if len(parts) == 1:
            platform, name = "uplay", parts[0]
            user_id = None
        elif len(parts) == 2:
            platform, name = parts
            user_id = None
        elif len(parts) == 3:
            platform, name = parts
            user_id = get_member(parts[2], message).id
        else:
            raise BotError("expected `platform`, `username`, and optional `server member`!")

        self.tracker.set_channel_if_unset(message.guild.id, message.channel.id)

        player = self.tracker.get_player(name=name, platform=platform)
        if player is None:
            auth = siegeapi.Auth(token=self.token)
            try:
                player_data = await auth.get_player(name, platform=platform)
                await player_data.load_ranked_v2()
                rank_name = player_data.ranked_profile.rank
                rank_points = player_data.ranked_profile.rank_points
                player = self.tracker.create_player(
                    uid=player_data.uid,
                    name=player_data.name,
                    platform=platform,
                    rank_name=rank_name,
                    rank_points=rank_points,
                    season_kills=player_data.ranked_profile.kills,
                    season_deaths=player_data.ranked_profile.deaths,
                )
            except siegeapi.InvalidRequest as error:
                raise BotError(f"invalid request: {error}")
            finally:
                await auth.close()

        created = self.tracker.create_spectator(message.guild.id, player.id, user_id)
        action = "Started watching" if created else "Already watching"
        await message.channel.send(f"{action} {player.name} ({player.rank_name}, {player.rank_points} elo)")

    async def command_remove(self, text: str, message: disnake.Message):
        """Stop spectating a user."""

        parts = text.strip().split(maxsplit=2)
        if len(parts) == 1:
            platform, name = "uplay", parts[0]
        elif len(parts) == 2:
            platform, name = parts
        else:
            raise BotError("expected `platform` and `username`!")

        player = self.tracker.get_player(platform=platform, name=name)
        if player is None:
            raise BotError(f"couldn't find player {text}!")

        deleted = self.tracker.delete_spectator(message.guild.id, player.id)
        action = "Stopped watching" if deleted else "Wasn't watching"
        await message.channel.send(f"{action} {player.name}")

    async def command_here(self, text: str, message: disnake.Message):
        """Set the notification channel for this plugin."""

        self.tracker.set_channel(message.guild.id, message.channel.id)
        await message.channel.send("Siege notifications will be posted to this channel!")

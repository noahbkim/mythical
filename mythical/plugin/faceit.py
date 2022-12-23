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


def get_faceit_elo(key: str, nickname: str) -> dict:
    """Get retrieve data with ELO and level."""

    response = requests.get(
        f"https://open.faceit.com/data/v4/players?nickname={nickname}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    if response.status_code != 200:
        raise BotError(f"couldn't find player with nickname {nickname}!")

    return response.json()


def get_faceit_last_match(key: str, player_id: str) -> Optional[dict]:
    """Get the most recent match."""

    response = requests.get(
        f"https://open.faceit.com/data/v4/players/{player_id}/history?game=csgo&offset=0&limit=1",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    if response.status_code != 200:
        raise BotError(f"invalid player id!")

    data = response.json()
    if not data["items"]:
        return None

    return data["items"][0]


def get_faceit_match_statistics(key: str, match_id: str) -> Optional[dict]:
    """Get detailed statistics from a match ID."""

    response = requests.get(
        f"https://open.faceit.com/data/v4/matches/{match_id}/stats",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    if response.status_code != 200:
        raise BotError(f"invalid match id!")

    return response.json()


@dataclass
class FaceitPlayerStatistics:
    kills: int
    assists: int
    deaths: int
    hsp: int
    rounds: int

    @property
    def kd(self) -> str:
        return str(round(self.kills / self.deaths, 2)) if self.deaths > 0 else "godmode"

    @property
    def kad(self) -> str:
        return f"{self.kills}/{self.assists}/{self.deaths}"

    @property
    def kpr(self) -> str:
        return str(round(self.kills / self.rounds, 2)) if self.rounds > 0 else "godmode"


def get_won(match: dict, nickname: str) -> bool:
    """Check if a player won."""

    winner = match["results"]["winner"]
    for player in match["teams"][winner]["players"]:
        if player["nickname"] == nickname:
            return True
    return False


def get_player_statistics(match_statistics: dict, nickname: str) -> Optional[FaceitPlayerStatistics]:
    """Get player statistics by nickname."""

    overview = match_statistics["rounds"][0]
    for team_data in overview["teams"]:
        for player_data in team_data["players"]:
            if player_data["nickname"] == nickname:
                return FaceitPlayerStatistics(
                    kills=int(player_data["player_stats"]["Kills"]),
                    assists=int(player_data["player_stats"]["Assists"]),
                    deaths=int(player_data["player_stats"]["Deaths"]),
                    hsp=int(player_data["player_stats"]["Headshots %"]),
                    rounds=int(overview["round_stats"]["Rounds"])
                )
    return None


def format_result(data: dict, won: bool) -> str:
    """Return win or loss and ordered score."""

    scores = map(int, data["rounds"][0]["round_stats"]["Score"].split(" / "))
    score = ":".join(map(str, sorted(scores, reverse=won)))
    return f"won {score}" if won else f"lost {score}"



@dataclass
class FaceitPlayer(Player):
    """This is a convenience class that mirrors the database record.

    The dataclass decorator generates a constructor along with some
    other object-related methods for convenience.
    """

    nickname: str
    level: int
    elo: int

    class Meta:
        fields = ("nickname", "level", "elo")
        schema = (
            "nickname VARCHAR",
            "level INTEGER",
            "elo INTEGER",
            "UNIQUE (nickname)",
        )


class FaceitTracker(Tracker[FaceitPlayer]):
    """High level database access for raider.io commands.

    Abstracts away all the SQL queries we need to persist the state of
    our notification bot. Requires a sqlite3 connection; it's not our
    responsibility to create the database file, only to use it.
    """

    class Meta:
        model = FaceitPlayer

    def set_level(self, player_id: int, level: int, elo: int):
        """Update a player's rating."""

        with self.connection:
            cursor = self.connection.cursor()
            cursor.execute(
                f"UPDATE {self._players} SET (level, elo)=(?, ?) WHERE id=?",
                (level, elo, player_id),
            )


class FaceitPlugin(BotPlugin):
    """Provide subcommands related to Faceit API."""

    tracker: FaceitTracker
    key: str

    def __init__(self, connection: sqlite3.Connection):
        """Set command handlers."""

        self.tracker = FaceitTracker(connection, prefix="faceit")
        self.commands = {
            "r": self.command_rating,
            "rating": self.command_rating,
            "elo": self.command_rating,
            "add": self.command_add,
            "remove": self.command_remove,
            "l": self.command_leaderboard,
            "leaderboard": self.command_leaderboard,
            "here": self.command_here,
        }

    def configure(self, section: Optional[configparser.SectionProxy]):
        """Set Faceit API key."""

        super().configure(section)
        if section is None:
            raise ValueError("Missing Faceit configuration in config!")

        self.key = section["key"]

    async def ready(self, client: disnake.Client):
        """Start background tasks."""

        await super().ready(client)
        if not self.update.is_running():
            self.update.start()

    @tasks.loop(minutes=5)
    async def update(self):
        """Update all players, notify if new rating."""

        for player in self.tracker.get_spectated_players():
            data = get_faceit_elo(self.key, player.nickname)
            level = data["games"]["csgo"]["skill_level"]
            elo = data["games"]["csgo"]["faceit_elo"]
            await self.update_player(player, level, elo, data)

    async def update_player(self, player: FaceitPlayer, new_level: int, new_elo: int, data: dict):
        """Update a player's level and elo and notify."""

        if new_elo != player.elo:
            self.tracker.set_level(player.id, new_level, new_elo)

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

                description = []
                match_url = None

                last_match = get_faceit_last_match(self.key, data["player_id"])
                if last_match:
                    match_id = last_match["match_id"]
                    match_url = last_match["faceit_url"].format(lang="en")
                    last_match_statistics = get_faceit_match_statistics(self.key, match_id)
                    match_map = last_match_statistics["rounds"][0]["round_stats"]["Map"]
                    player_statistics = get_player_statistics(last_match_statistics, player.nickname)
                    result = format_result(last_match_statistics, get_won(last_match, player.nickname))
                    description.append(
                        f"{player.nickname} {result} on {match_map}"
                        f" with a K/D of {player_statistics.kad} ({player_statistics.kd}, {player_statistics.kpr} KPR)"
                        f" and {player_statistics.hsp}% HS."
                    )

                if new_level > player.level:
                    description.append(f"They are now level {new_level}.")

                reached = (
                    f"gained {new_elo - player.elo}"
                    if new_elo > player.elo else
                    f"lost {player.elo - new_elo}"
                )
                embed = disnake.Embed(
                    title=f"{player.nickname} {reached} faceit elo",
                    description=" ".join(description),
                    color=disnake.Colour.brand_green() if new_elo > player.elo else disnake.Colour.brand_red(),
                    timestamp=datetime.datetime.now(),
                )

                embed.add_field(name="Previous", value=str(round(player.elo, 1)), inline=True)
                embed.add_field(name="Current", value=str(round(new_elo, 1)), inline=True)
                embed.add_field(name="Change", value=str(round(new_elo - player.elo, 1)), inline=True)
                if match_url is not None:
                    embed.add_field(name="Match room", value=match_url, inline=False)

                await channel.send(embed=embed)

    @tasks.loop(hours=24)
    async def cleanup(self):
        """Remove players that aren't spectated."""

        self.tracker.delete_players_without_spectator()

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        data = get_faceit_elo(self.key, text)
        nickname = data["nickname"]
        level = data["games"]["csgo"]["skill_level"]
        elo = data["games"]["csgo"]["faceit_elo"]
        await message.channel.send(f"{nickname} is level {level} ({elo} elo)")

        player = self.tracker.get_player(nickname=text)
        if player is not None:
            await self.update_player(player, level, elo, data)

    async def command_add(self, text: str, message: disnake.Message):
        """Start spectating a user."""

        parts = text.split(maxsplit=3)
        if len(parts) == 1:
            nickname = parts[0]
            user_id = None
        elif len(parts) == 2:
            nickname, last = parts
            user_id = get_member(last, message).id
        else:
            raise BotError("expected `nickname` and optional `server member`!")

        self.tracker.set_channel_if_unset(message.guild.id, message.channel.id)

        player = self.tracker.get_player(nickname=nickname)
        if player is None:
            data = get_faceit_elo(self.key, nickname)
            level = data["games"]["csgo"]["skill_level"]
            elo = data["games"]["csgo"]["faceit_elo"]
            player = self.tracker.create_player(nickname=nickname, level=level, elo=elo)

        created = self.tracker.create_spectator(message.guild.id, player.id, user_id)
        action = "Started watching" if created else "Already watching"
        await message.channel.send(f"{action} {player.nickname} ({round(player.elo, 1)} elo)")

    async def command_remove(self, text: str, message: disnake.Message):
        """Stop spectating a user."""

        player = self.tracker.get_player(nickname=text)
        if player is None:
            raise BotError(f"couldn't find player {text}!")

        deleted = self.tracker.delete_spectator(message.guild.id, player.id)
        action = "Stopped watching" if deleted else "Wasn't watching"
        await message.channel.send(f"{action} {player.nickname}")

    async def command_leaderboard(self, text: str, message: disnake.Message):
        """List players watched by the guild in order of rating."""

        players = list(self.tracker.get_players_spectated_by_guild(message.guild.id))
        players.sort(key=lambda item: item.player.elo, reverse=True)

        lines = []
        for i, item in enumerate(players, start=1):
            line = f"{i}. {item.player.nickname}, {round(item.player.elo, 1)}"
            if item.user_id is not None:
                member = message.guild.get_member(item.user_id)
                if member is not None:
                    line += f" ({member.name})"
            lines.append(line)

        embed = disnake.Embed(
            title="Faceit Leaderboard",
            description="\n".join(lines) or "It's a little bit empty in here...",
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        # Allow the footer to be empty, in which case we don't set it
        if players:
            first = players[0].player
            embed.set_footer(text=random.choice(
                (
                    f"{first.nickname} needs to go outside",
                    f"{first.nickname} should probably touch grass",
                    f"{first.nickname} might need to take a break",
                    f"{first.nickname} hasn't showered in days",
                    f"{first.nickname} is losing their grip",
                    f"{first.nickname} definitely isn't short",
                    f"Somebody should check on {first.nickname}",
                    f"I can smell {first.nickname} from here",
                )
            ))

        await message.channel.send(embed=embed)

    async def command_here(self, text: str, message: disnake.Message):
        """Set the notification channel for this plugin."""

        self.tracker.set_channel(message.guild.id, message.channel.id)
        await message.channel.send("Faceit notifications will be posted to this channel!")

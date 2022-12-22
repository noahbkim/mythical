import disnake
from disnake.ext import commands
from disnake.ext import tasks
import requests

import configparser
import hashlib
import sqlite3


class InternalError(Exception):
    """Throw this when a bot command can't be completed.

    We'll catch it in the bot logic and respond with the message
    included in the constructor. It's better to make our own exception
    to avoid accidentally hiding other ones thrown by bugs.
    """


def get_all_mythic_plus_best_runs(region: str, realm: str, name: str) -> requests.Response:
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
    if response.status_code == 404:
        raise InternalError("player unrecognized by raider.io!")
    elif response.status_code != 200:
        raise InternalError("error in response from raider.io!")

    return response


def compute_mythic_plus_rating(data: dict) -> float:
    """Compute the raider.io rating given best run data."""

    score = 0
    for run in data["mythic_plus_best_runs"]:
        score += 1.5 * run["score"]
    for run in data["mythic_plus_alternate_runs"]:
        score += 0.5 * run["score"]
    return score


def format_rating_message(data: dict, rating: float) -> float:
    """Compose a rating update message using best run data."""

    name = data["name"]
    class_ = data["class"]
    race = data["race"]
    role = data["active_spec_role"].lower()

    return f"{name} ({class_} {race}, {role}) has rating {round(rating, 1)}"


def main():
    """Build the bot, register commands, configure, and run.

    We put this in a separate function to hide variables from the
    global scope; we call it in the __name__ check in order to prevent
    the bot from running if we're trying to import functionality from
    other Python files.
    """

    intents = disnake.Intents(messages=True, message_content=True, reactions=True, guilds=True)
    bot = commands.Bot(command_prefix="%", intents=intents)

    database = sqlite3.connect("mythical.sqlite3")
    with database:
        database.execute("CREATE TABLE IF NOT EXISTS players (guild, region, realm, name, rating)")
        database.execute("CREATE TABLE IF NOT EXISTS channels (guild, channel)")

    @bot.command()
    async def rating(context: commands.Context, region: str, realm: str, name: str):
        """Query the rating of a player."""

        try:
            response = get_all_mythic_plus_best_runs(region, realm, name)
        except InternalError as error:
            await context.send(str(error))
            return

        data = response.json()
        rating = compute_mythic_plus_rating(data)
        await context.send(format_rating_message(data, rating))

    @bot.command()
    async def add(context: commands.Context, region: str, realm: str, name: str):
        """Add a user to watch to persistent storage."""

        with database:
            cursor = database.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM players WHERE region=? AND realm=? AND name=?",
                (region, realm, name),
            )
            if cursor.fetchone()[0] > 0:
                await context.send(f"already tracking {name}!")
                return

            # Insert channel if not exist; essentially a default
            cursor.execute(
                "INSERT OR IGNORE INTO channels (guild, channel) VALUES (?, ?)",
                (context.guild.id, context.channel.id),
            )

            try:
                response = get_all_mythic_plus_best_runs(region, realm, name)
            except InternalError as error:
                await context.send(f"failed to add {name}: {error}")
                return

            data = response.json()
            rating = compute_mythic_plus_rating(data)
            cursor.execute(
                "INSERT INTO players (guild, region, realm, name, rating) VALUES (?, ?, ?, ?, ?)",
                (context.guild.id, region, realm, name, rating),
            )

            await context.send(f"added {name} with current rating {round(rating, 1)}!")

    @bot.command()
    async def remove(context: commands.Context, region: str, realm: str, name: str):
        """Remove a user from persistent storage."""

        with database:
            cursor = database.cursor()
            cursor.execute(
                "DELETE FROM players WHERE region=? AND realm=? AND name=?",
                (region, realm, name),
            )

            await context.send(f"stopped watching {cursor.rowcount} players")

    @bot.command()
    async def leaderboard(context: commands.Context):
        """Show participating guild members in order of ratings."""

        cursor = database.cursor()
        cursor.execute("SELECT name, rating FROM players WHERE guild=?", (context.guild.id,))
        results = list(cursor.fetchall())
        results.sort(key=lambda name, rating: rating, reverse=True)
        lines = []
        for i, (name, rating) in enumerate(results):
            lines.append(f"{i + 1}. {name} ({rating})")
        await context.send("\n".join(lines))

    @bot.command()
    async def here(context: commands.Context):
        """Set the output channel for the bot."""

        with database:
            cursor = database.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO channels (guild, channel) VALUES (?, ?)",
                (context.guild.id, context.channel.id),
            )
            cursor.execute(
                "UPDATE channels SET channel=? WHERE guild=?",
                (context.guild.id, context.channel.id),
            )
        await context.send("rating notifications will be posted to this channel!")

    @tasks.loop(minutes=5)
    async def update():
        """Iterate each database entry and check if rating changed."""

        cursor = database.cursor()
        cursor.execute(
            "SELECT channel, region, realm, name, rating FROM"
            " players INNER JOIN channels ON players.guild = channels.guild"
        )

        for channel_id, region, realm, name, rating in cursor.fetchall():
            try:
                response = get_all_mythic_plus_best_runs(region, realm, name)
            except InternalError as error:
                await context.send(f"failed to add {name}: {error}")
                return

            data = response.json()
            new_rating = compute_mythic_plus_rating(data)

            print(data["name"], new_rating, rating)
            if new_rating != rating:
                channel = bot.get_channel(channel_id)
                await channel.send(format_rating_message(data, rating))

    @update.before_loop
    async def before_update():
        await bot.wait_until_ready()

    update.start()

    config = configparser.ConfigParser()
    config.read("mythical.conf")
    client_id = config["discord"]["client_id"]

    print(
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        "&permissions=274877909056"
        "&scope=bot"
    )

    bot.run(config["discord"]["token"])


if __name__ == "__main__":
    main()

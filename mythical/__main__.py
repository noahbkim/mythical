"""Build the bot, register commands, configure, and run.

We put this in a separate function to hide variables from the
global scope; we call it in the __name__ check in order to prevent
the bot from running if we're trying to import functionality from
other Python files.
"""

import disnake
from disnake.ext import commands

import configparser
import sqlite3

from mythical.raider import RaiderCog, RaiderDatabase
from mythical.faceit import FaceitCog


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

bot.add_cog(RaiderCog(bot, RaiderDatabase(sqlite3.connect("mythical.sqlite3"))))
bot.add_cog(FaceitCog(bot, config["faceit"]["api_key"]))
bot.run(config["discord"]["token"])

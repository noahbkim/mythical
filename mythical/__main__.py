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

from mythical.bot import Bot
from mythical.plugin.raider import RaiderPlugin
# from mythical.faceit import FaceitCog
# from mythical.valorant import ValorantCog

connection = sqlite3.connect("mythical.sqlite3")
intents = disnake.Intents(messages=True, message_content=True, reactions=True, guilds=True, members=True)
bot = Bot("%", intents=intents, plugins={
    "raider": RaiderPlugin(connection),
})

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

# bot.add_cog(RaiderCog(bot, RaiderTracker(sqlite3.connect("mythical.sqlite3"), prefix="raider")))
# bot.add_cog(FaceitCog(bot, config["faceit"]["api_key"]))
# bot.add_cog(ValorantCog(bot, config["valorant"]["username"], config["valorant"]["password"]))
bot.run(config["discord"]["token"])

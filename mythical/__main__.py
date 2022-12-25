"""Build the bot, register commands, configure, and run.

We put this in a separate function to hide variables from the
global scope; we call it in the __name__ check in order to prevent
the bot from running if we're trying to import functionality from
other Python files.
"""

import disnake

import configparser
import sqlite3

from mythical.bot import Bot
from mythical.plugin.raider import RaiderPlugin
from mythical.plugin.faceit import FaceitPlugin
from mythical.plugin.valorant import ValorantPlugin
from mythical.plugin.height import HeightPlugin

config = configparser.ConfigParser()
config.read("mythical.conf")

connection = sqlite3.connect("mythical.sqlite3")
intents = disnake.Intents(
    messages=True,
    message_content=True,
    reactions=True,
    guilds=True,
    members=True,
    presences=True,
)

bot = Bot(
    config["discord"].get("prefix", "%"),
    intents=intents,
    plugins={
        "raider": RaiderPlugin(connection),
        "faceit": FaceitPlugin(connection),
        "valorant": ValorantPlugin(connection),
        "height": HeightPlugin(),
    },
)

bot.configure(config)

# Print an add link based on configuration
client_id = config["discord"]["client_id"]
print(
    "https://discord.com/api/oauth2/authorize"
    f"?client_id={client_id}"
    "&permissions=274877909056"
    "&scope=bot"
)

bot.run()

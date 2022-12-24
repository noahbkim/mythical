import disnake

import datetime
import random

from ..bot import BotPlugin


def imperial(inches: int) -> str:
    f, i = divmod(inches, 12)
    return f"{f}' {i}\""


def get_height(name: str) -> int:
    if "arvid" in name.lower() or "moopey" in name.lower():
        return 4 * 12 + 10
    return random.randint(5 * 12, 10 * 12)


class HeightPlugin(BotPlugin):
    """Provide subcommands related to Faceit API."""

    def __init__(self):
        """Set command handlers."""

        self.commands = {
            "r": self.command_rating,
            "rating": self.command_rating,
            "l": self.command_leaderboard,
            "leaderboard": self.command_leaderboard,
        }

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        name = text if text.strip() else message.author.name
        await message.channel.send(f"{name} is {imperial(get_height(name))}")

    async def command_leaderboard(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        heights = []
        for member in message.channel.members:
            if member.status == disnake.Status.online:
                heights.append((member.name, get_height(member.name)))
        heights.sort(key=lambda pair: pair[1], reverse=True)

        lines = []
        for i, (name, height) in enumerate(heights, start=1):
            lines.append(f"{i}. {name}, {imperial(height)}")

        embed = disnake.Embed(
            title="Height Leaderboard",
            description="\n".join(lines) or "It's a little bit empty in here...",
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        await message.channel.send(embed=embed)

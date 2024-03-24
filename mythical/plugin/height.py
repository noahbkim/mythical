import disnake

import abc
import datetime
import random
import time
from typing import Generic, TypeVar

from ..bot import Bot, BotPlugin

T = TypeVar("T")


def imperial(inches: int) -> str:
    f, i = divmod(inches, 12)
    return f"{f}' {i}\""


def metric(inches: int) -> str:
    cm = int(inches * 2.54)
    return f"{cm} cm"


class MeasurePlugin(BotPlugin, Generic[T], abc.ABC):
    """Provide subcommands related to Faceit API."""

    cache: dict[str, tuple[T, float]]
    cache_time: float = 15 * 60

    def __init__(self, bot: Bot):
        """Set command handlers."""

        super().__init__(bot)
        self.cache = {}
        self.commands = {
            "r": self.command_rating,
            "rating": self.command_rating,
            "l": self.command_leaderboard,
            "leaderboard": self.command_leaderboard,
        }

    @abc.abstractmethod
    async def get_measure_name(self) -> str:
        pass

    @abc.abstractmethod
    async def get_measure(self, name: str) -> T:
        pass

    @abc.abstractmethod
    async def format_measure(self, name: str, measure: T) -> str:
        pass

    async def _get_measure(self, name: str) -> T:
        """Call `get_measure` and cache."""

        value = self.cache.get(name)
        if value is None or value[1] < time.time() - self.cache_time:
            value = self.cache[name] = await self.get_measure(name), time.time()
        return value[0]

    async def command_rating(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        name = text if text.strip() else message.author.name
        measure = await self._get_measure(name)
        await message.channel.send(f"{name} is {await self.format_measure(name, measure)}")

    async def command_leaderboard(self, text: str, message: disnake.Message):
        """Respond to rating request."""

        measures = []
        for member in message.channel.members:
            if member.status != disnake.Status.offline:
                measures.append((member.name, self._get_measure(member.name)))
        measures.sort(key=lambda pair: pair[1], reverse=True)

        lines = []
        for i, (name, measure) in enumerate(measures, start=1):
            lines.append(f"{i}. {name}, {await self.format_measure(name, measure)}")

        title = await self.get_measure_name()
        embed = disnake.Embed(
            title=f"{title.title()} Leaderboard",
            description="\n".join(lines) or "It's a little bit empty in here...",
            color=0xF0C43F,
            timestamp=datetime.datetime.now(),
        )

        await message.channel.send(embed=embed)


class HeightPlugin(MeasurePlugin[int]):
    """Leaderboard for user height."""

    async def get_measure_name(self) -> str:
        return "height"

    async def get_measure(self, name: str) -> int:
        return int(random.normalvariate(65, 3))

    async def format_measure(self, name: str, measure: int) -> str:
        return f"{imperial(measure)} ({metric(measure)})"


class LengthPlugin(MeasurePlugin[float]):
    """Leaderboard for user height."""

    async def get_measure_name(self) -> str:
        return "length"

    async def get_measure(self, name: str) -> float:
        return random.normalvariate(6, 1)

    async def format_measure(self, name: str, measure: float) -> str:
        inches = round(measure, 1)
        centimeters = round(measure * 2.54, 1)
        return f"{inches}\" ({centimeters} cm)"


from __future__ import annotations

import configparser
import pprint
import re
import sys
import traceback
from typing import Dict, Callable, Tuple, Iterable, Coroutine, Optional, Any, TypeVar

import disnake

T = TypeVar("T")


def split(text: str) -> Tuple[str, str]:
    """Split into subcommand, rest. Default empty string."""

    items = text.split(maxsplit=1)
    if len(items) == 0:
        return "", ""
    elif len(items) == 1:
        return items[0], ""
    else:
        return items[0], items[1]


def either(names: Iterable[str]) -> str:
    """Format a series of strings in code blocks."""

    return ", ".join(f"`{name}`" for name in names)


class BotError(Exception):
    """Throw to exit bot command handling."""


def handle_exception(f: T) -> T:
    """Wraps exceptions thrown on `Bot` member methods."""

    async def wrapper(self: BotPlugin, *args: Any, **kwargs: Any) -> Any:
        try:
            return await f(self, *args, **kwargs)
        except Exception as exception:
            await self.on_exception(exception)

    return wrapper


class BotPlugin:
    """Subcommand for the bot."""

    bot: Bot
    commands: Dict[str, Callable[[str, disnake.Message], Coroutine[None, None, None]]]

    def __init__(self, bot: Bot):
        """Save a reference to the parent bot."""

        self.bot = bot
        self.commands = {}

    def configure(self, section: Optional[configparser.SectionProxy]) -> None:
        """Take options from relevant section."""

    async def on_ready(self) -> None:
        """Called when the client is ready."""

    async def on_message(self, text: str, message: disnake.Message):
        """Handle subcommand from raw text."""

        command, rest = split(text)
        handler = self.commands.get(command)
        if handler is not None:
            await handler(rest, message)
        else:
            await message.channel.send(
                f"invalid subcommand `{command}`"
                if command else
                f"missing subcommand, try {either(self.commands)}"
            )

    async def on_exception(self, exception: Exception) -> None:
        """Pass the exception up to the bot for logging."""

        await self.bot.on_exception(exception)


class Bot(disnake.Client):
    """Custom implementation of a command handler."""

    prefix: str
    plugins: Dict[str, BotPlugin]
    debug_id: int | None

    def __init__(self, prefix: str, plugins: dict[str, Any], **kwargs):
        """Set plugins, start loops."""

        super().__init__(**kwargs)
        self.prefix = prefix
        self.plugins = {name: constructor(self) for name, constructor in plugins.items()}
        self.token = None
        self.debug_id = None

    def configure(self, config: configparser.ConfigParser):
        """Propagate config sections to plugins."""

        self.token = config["discord"]["token"]
        debug_id = config["discord"].get("debug_id")
        self.debug_id = int(debug_id) if debug_id is not None else None
        for name, plugin in self.plugins.items():
            plugin.configure(config[name] if config.has_section(name) else None)

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Pass token if it's been configured."""

        token = {"token": self.token} if self.token is not None else {}
        return super().run(*args, **kwargs, **token)

    async def on_ready(self):
        """Set up each plugin."""

        for plugin in self.plugins.values():
            await plugin.on_ready()

    @handle_exception
    async def on_message(self, message: disnake.Message):
        """Try to handle commands."""

        if message.author.id == self.user.id:
            return

        if not message.content.startswith(self.prefix):
            return

        command, rest = split(message.content[len(self.prefix):])
        plugin = self.plugins.get(command)
        if plugin is None:
            return

        try:
            await plugin.on_message(rest, message)
        except BotError as error:
            await message.channel.send(f"Error: {error}")

    async def on_exception(self, exception: Exception) -> None:
        """Called by methods wrapped with `handle_exception`."""

        formatted_exception = "".join(traceback.format_exception(exception)).rstrip()
        frame = sys.exc_info()[2]
        formatted_locals = pprint.pformat(frame.tb_next.tb_frame.f_locals)

        channel = self.get_channel(self.debug_id)
        if channel is not None:
            await channel.send(f"Error: {exception}\n```{formatted_exception}\nLocals: {formatted_locals}```")
        else:
            print(f"Could not find channel {self.debug_id}!")
            print(f"Error: {exception}")
            print(formatted_exception)
            print(f"Locals: {formatted_locals}")


def try_get_member(argument: str, message: disnake.Message) -> Optional[disnake.Member]:
    """Try to discern a member from an argument."""

    match = re.match(r"<@!?([0-9]{17,19})>$", argument)
    if match is not None:
        return message.guild.get_member(int(match.group(1)))
    else:
        return message.guild.get_member_named(argument)


def get_member(argument: str, message: disnake.Message) -> disnake.Member:
    """Throw if not found."""

    member = try_get_member(argument, message)
    if member is None:
        raise BotError(f"failed to resolve server member {argument}!")
    return member

import configparser
import re
from typing import Dict, Callable, Tuple, Iterable, Coroutine, Optional, Any

import disnake


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


class BotPlugin:
    """Subcommand for the bot."""

    commands: Dict[str, Callable[[str, disnake.Message], Coroutine[None, None, None]]]
    client: disnake.Client

    def configure(self, section: Optional[configparser.SectionProxy]):
        """Take options from relevant section."""

    async def ready(self, client: disnake.Client):
        """Called when the client is ready."""

        self.client = client

    async def handle(self, text: str, message: disnake.Message):
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


class BotError(Exception):
    """Throw to exit bot command handling."""


class Bot(disnake.Client):
    """Custom implementation of a command handler."""

    prefix: str
    plugins: Dict[str, BotPlugin]

    def __init__(self, prefix: str, plugins: Dict[str, BotPlugin], **kwargs):
        """Set plugins, start loops."""

        super().__init__(**kwargs)
        self.prefix = prefix
        self.plugins = plugins
        self.token = None

    def configure(self, config: configparser.ConfigParser):
        """Propagate config sections to plugins."""

        self.token = config["discord"]["token"]
        for name, plugin in self.plugins.items():
            plugin.configure(config[name] if config.has_section(name) else None)

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Pass token if it's been configured."""

        token = {"token": self.token} if self.token is not None else {}
        return super().run(*args, **kwargs, **token)

    async def on_ready(self):
        """Set up each plugin."""

        for plugin in self.plugins.values():
            await plugin.ready(self)

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
            await plugin.handle(rest, message)
        except BotError as error:
            await message.reply(f"Error: {error}")


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

# pylint: disable=too-many-arguments

import asyncio
import sys
from os import getenv
from typing import Optional

import click
import hupper
from dotenv import load_dotenv

from ._version import __version__
from .logs import configure_logging

# load .env environment variables as early as possible
if not getenv("PYTEST_CURRENT_TEST") and "pytest" not in sys.modules:  # pragma: no cover
    load_dotenv()


@click.command()
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]),
    default=None,
    help="INFO is not set, can also be set by the LOG_LEVEL environment variable.",
)
@click.option(
    "-d",
    "--dev-mode",
    default=False,
    is_flag=True,
    help="Development mode, automatically reload bot when source changes",
)
@click.option(
    "-m",
    "--mock-games",
    default=False,
    is_flag=True,
    help="Produce mock game urls instead of real ones",
)
@click.option(
    "-s",
    "--sync-commands",
    default=False,
    is_flag=True,
    help="Force sync all slash commands at startup",
)
@click.option(
    "-c",
    "--clean-commands",
    default=False,
    is_flag=True,
    help="Cleanup all unused slash commands at startup",
)
@click.option(
    "-a",
    "--api",
    default=False,
    is_flag=True,
    help="Start the API web server instead of the bot",
)
@click.option(
    "-p",
    "--port",
    type=click.INT,
    required=False,
    help="Use the given port number to serve the API",
)
@click.version_option(version=__version__)
def main(
    log_level: Optional[str],
    dev_mode: bool,
    mock_games: bool,
    sync_commands: bool,
    clean_commands: bool,
    api: bool,
    port: Optional[int] = None,
) -> None:
    if dev_mode:
        hupper.start_reloader("spellbot.main")

    from .client import build_bot
    from .settings import Settings
    from .web import launch_web_server

    settings = Settings()
    level = log_level if log_level is not None else (getenv("LOG_LEVEL") or "INFO")
    configure_logging(level)

    loop = asyncio.get_event_loop()
    if api:
        launch_web_server(settings, loop, port or settings.PORT)
        loop.run_forever()
    else:
        assert settings.BOT_TOKEN is not None
        bot = build_bot(
            loop=loop,
            mock_games=mock_games,
            force_sync_commands=sync_commands,
            clean_commands=clean_commands,
        )
        bot.run(settings.BOT_TOKEN)
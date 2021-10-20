import asyncio
import logging
import traceback
from asyncio import AbstractEventLoop as Loop
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from uuid import uuid4

import discord
from discord.ext.commands import Bot, errors
from discord_slash import SlashCommand, context
from expiringdict import ExpiringDict

from spellbot.database import get_legacy_prefixes, initialize_connection
from spellbot.errors import SpellbotAdminOnly, UserBannedError
from spellbot.models import create_all
from spellbot.operations import safe_send_channel
from spellbot.services.channels import ChannelsService
from spellbot.services.guilds import GuildsService
from spellbot.services.verifies import VerifiesService
from spellbot.settings import Settings
from spellbot.spelltable import generate_link
from spellbot.utils import user_can_moderate

logger = logging.getLogger(__name__)


class SpellBot(Bot):
    slash: SlashCommand

    def __init__(
        self,
        loop: Optional[Loop] = None,
        mock_games: bool = False,
    ):
        self.settings = Settings()
        intents = discord.Intents().default()
        intents.members = True
        intents.messages = True
        super().__init__(
            command_prefix="!",
            help_command=None,
            loop=loop,
            intents=intents,
        )
        create_all(self.settings.DATABASE_URL)
        self.mock_games = mock_games
        self.legacy_prefix_cache = defaultdict(lambda: "!")
        self.channel_locks = ExpiringDict(max_len=100, max_age_seconds=3600)  # 1 hr

    @asynccontextmanager
    async def channel_lock(self, channel_xid: int) -> AsyncGenerator[None, None]:
        if not self.channel_locks.get(channel_xid):
            self.channel_locks[channel_xid] = asyncio.Lock()
        async with self.channel_locks[channel_xid]:  # type: ignore
            yield

    async def on_ready(self) -> None:  # pragma: no cover
        logger.debug("logged in as %s", self.user)

    async def create_spelltable_link(self) -> Optional[str]:
        if self.mock_games:
            return f"http://exmaple.com/game/{uuid4()}"
        return await generate_link()

    async def handle_errors(self, ctx: context.InteractionContext, ex: Exception):
        if isinstance(ex, errors.NoPrivateMessage):
            return await safe_send_channel(
                ctx,
                "This command is not supported via Direct Message.",
                hidden=True,
            )
        if isinstance(ex, SpellbotAdminOnly):
            return await safe_send_channel(
                ctx,
                "You do not have permission to do that.",
                hidden=True,
            )
        if isinstance(ex, UserBannedError):
            return await safe_send_channel(
                ctx,
                "You have been banned from using SpellBot.",
                hidden=True,
            )

        ref = (
            f"command `{ctx.name}`"
            if isinstance(ctx, context.SlashContext)
            else f"component `{ctx.custom_id}`"
            if isinstance(ctx, context.ComponentContext)
            else f"interaction `{ctx.interaction_id}`"
        )
        logger.error(
            f"error: unhandled exception in {ref}: %s: %s",
            ex.__class__.__name__,
            ex,
        )
        traceback.print_tb(ex.__traceback__)

    async def on_component_callback_error(
        self,
        ctx: context.ComponentContext,
        ex: Exception,
    ):
        return await self.handle_errors(ctx, ex)

    async def on_slash_command_error(self, ctx: context.SlashContext, ex: Exception):
        return await self.handle_errors(ctx, ex)

    async def on_message(self, message: discord.Message):
        if not message.guild or not hasattr(message.guild, "id"):
            return await super().on_message(message)  # handle direct messages normally
        if (
            not hasattr(message.channel, "type")
            or message.channel.type != discord.ChannelType.text
        ):
            return  # ignore everything else, except messages in text channels...
        if message.flags.value & 64:
            return  # message is hidden, ignore it
        await self.handle_verification(message)
        guild_xid = message.guild.id  # type: ignore
        if message.content.startswith(self.legacy_prefix_cache[guild_xid]):
            try:
                await message.reply(
                    "SpellBot uses slash commands now."
                    " Just type / to see the list of supported commands!",
                    delete_after=7.0,
                )
            except Exception as ex:
                logger.warning("warning: %s", ex, exc_info=True)

    async def handle_verification(self, message: discord.Message):
        # To verify users we need their user id, so just give up if it's not available
        if not hasattr(message.author, "id"):
            return
        message_author_xid = message.author.id  # type: ignore
        verified: Optional[bool] = None
        guilds = GuildsService()
        await guilds.upsert(message.guild)
        channels = ChannelsService()
        await channels.upsert(message.channel)
        if await channels.should_auto_verify():
            verified = True
        verify = VerifiesService()
        assert message.guild
        guild: discord.Guild = message.guild  # type: ignore
        await verify.upsert(guild.id, message_author_xid, verified)
        if not user_can_moderate(message.author, guild, message.channel):
            user_is_verified = await verify.is_verified()
            if user_is_verified and await channels.unverified_only():
                await message.delete()
            if not user_is_verified and await channels.verified_only():
                await message.delete()


def build_bot(
    loop: Optional[Loop] = None,
    mock_games: bool = False,
    force_sync_commands: bool = False,
    clean_commands: bool = False,
) -> SpellBot:
    # setup bot client and run migrations
    bot = SpellBot(loop=loop, mock_games=mock_games)

    # setup slash commands extension
    debug_guild: Optional[int] = None
    if bot.settings.DEBUG_GUILD:  # pragma: no cover
        debug_guild = int(bot.settings.DEBUG_GUILD)
        logger.info(f"using debug guild: {debug_guild}")
    bot.slash = SlashCommand(
        bot,
        debug_guild=debug_guild,
        sync_commands=force_sync_commands,
        delete_from_unused_guilds=clean_commands,
    )

    # load all cog extensions
    from spellbot.cogs import load_all_cogs

    load_all_cogs(bot)
    commands = (key for key in bot.slash.commands.keys() if key != "context")
    logger.info(f"loaded commands: {', '.join(commands)}")

    # setup database connection
    if not bot.loop.is_running():  # pragma: no cover
        # In tests we will have to call initialize_connection manually due to
        # pytest-asyncio having already started the async loop before we can get here.
        logger.info("initializing database connection...")
        bot.loop.run_until_complete(initialize_connection("spellbot-bot"))

        logger.info("building legacy command prefix cache...")
        db_legacy_prefixes = bot.loop.run_until_complete(get_legacy_prefixes())
        bot.legacy_prefix_cache.update(db_legacy_prefixes)

    return bot

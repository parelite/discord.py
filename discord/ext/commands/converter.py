"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import datetime
import inspect
import logging
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    Iterable,
    List,
    Literal,
    Optional,
    overload,
    Protocol,
    Tuple,
    Type,
    TypeVar,
    Union,
    runtime_checkable,
    get_origin,
    get_args,
)
import types

import discord

from .errors import *
from .errors import BasicEmojiConversionFailure

if TYPE_CHECKING:
    from discord.state import Channel
    from discord.threads import Thread

    from .parameters import Parameter
    from ._types import BotT, _Bot
    from .context import Context

__all__ = (
    'Converter',
    'ObjectConverter',
    'MemberConverter',
    'UserConverter',
    'MessageConverter',
    'PartialMessageConverter',
    'TextChannelConverter',
    'InviteConverter',
    'GuildConverter',
    'RoleConverter',
    'GameConverter',
    'ColourConverter',
    'ColorConverter',
    'VoiceChannelConverter',
    'StageChannelConverter',
    'EmojiConverter',
    'PartialEmojiConverter',
    'CategoryChannelConverter',
    'ForumChannelConverter',
    'IDConverter',
    'ThreadConverter',
    'GuildChannelConverter',
    'GuildStickerConverter',
    'ScheduledEventConverter',
    'clean_content',
    'Greedy',
    'Range',
    'run_converters',
)

_log = logging.getLogger(__name__)


def _get_from_guilds(bot: _Bot, getter: str, argument: Any) -> Any:
    result = None
    for guild in bot.guilds:
        result = getattr(guild, getter)(argument)
        if result:
            return result
    return result


_utils_get = discord.utils.get
T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
CT = TypeVar('CT', bound=discord.abc.GuildChannel)
TT = TypeVar('TT', bound=discord.Thread)


@runtime_checkable
class Converter(Protocol[T_co]):
    """The base class of custom converters that require the :class:`.Context`
    to be passed to be useful.

    This allows you to implement converters that function similar to the
    special cased ``discord`` classes.

    Classes that derive from this should override the :meth:`~.Converter.convert`
    method to do its conversion logic. This method must be a :ref:`coroutine <coroutine>`.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> T_co:
        """|coro|

        The method to override to do conversion logic.

        If an error is found while converting, it is recommended to
        raise a :exc:`.CommandError` derived exception as it will
        properly propagate to the error handlers.

        Note that if this method is called manually, :exc:`Exception`
        should be caught to handle the cases where a subclass does
        not explicitly inherit from :exc:`.CommandError`.

        Parameters
        -----------
        ctx: :class:`.Context`
            The invocation context that the argument is being used in.
        argument: :class:`str`
            The argument that is being converted.

        Raises
        -------
        CommandError
            A generic exception occurred when converting the argument.
        BadArgument
            The converter failed to convert the argument.
        """
        raise NotImplementedError('Derived classes need to implement this.')


_ID_REGEX = re.compile(r'([0-9]{15,20})$')


class IDConverter(Converter[T_co]):
    @staticmethod
    def _get_id_match(argument):
        return _ID_REGEX.match(argument)


class ObjectConverter(IDConverter[discord.Object]):
    """Converts to a :class:`~discord.Object`.

    The argument must follow the valid ID or mention formats (e.g. ``<@80088516616269824>``).

    .. versionadded:: 2.0

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by member, role, or channel mention.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Object:
        match = self._get_id_match(argument) or re.match(r'<(?:@(?:!|&)?|#)([0-9]{15,20})>$', argument)

        if match is None:
            raise ObjectNotFound(argument)

        result = int(match.group(1))

        return discord.Object(id=result)


class MemberConverter(IDConverter[discord.Member]):
    """Converts to a :class:`~discord.Member`.

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by username#discriminator (deprecated).
    4. Lookup by username#0 (deprecated, only gets users that migrated from their discriminator).
    5. Lookup by user name.
    6. Lookup by global name.
    7. Lookup by guild nickname.

    .. versionchanged:: 1.5
         Raise :exc:`.MemberNotFound` instead of generic :exc:`.BadArgument`

    .. versionchanged:: 1.5.1
        This converter now lazily fetches members from the gateway and HTTP APIs,
        optionally caching the result if :attr:`.MemberCacheFlags.joined` is enabled.

    .. deprecated:: 2.3
        Looking up users by discriminator will be removed in a future version due to
        the removal of discriminators in an API change.
    """

    async def query_member_named(self, guild: discord.Guild, argument: str) -> Optional[discord.Member]:
        cache = guild._state.member_cache_flags.joined
        username, _, discriminator = argument.rpartition('#')

        # If # isn't found then "discriminator" actually has the username
        if not username:
            discriminator, username = username, discriminator

        if discriminator == '0' or (len(discriminator) == 4 and discriminator.isdigit()):
            lookup = username
            predicate = lambda m: m.name == username and m.discriminator == discriminator
        else:
            lookup = argument
            predicate = lambda m: m.name == argument or m.global_name == argument or m.nick == argument

        members = await guild.query_members(lookup, limit=100, cache=cache)
        return discord.utils.find(predicate, members)

    async def query_member_by_id(self, bot: _Bot, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        ws = bot._get_websocket(shard_id=guild.shard_id)
        cache = guild._state.member_cache_flags.joined
        if ws.is_ratelimited():
            # If we're being rate limited on the WS, then fall back to using the HTTP API
            # So we don't have to wait ~60 seconds for the query to finish
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return None

            if cache:
                guild._add_member(member)
            return member

        # If we're not being rate limited then we can use the websocket to actually query
        members = await guild.query_members(limit=1, user_ids=[user_id], cache=cache)
        if not members:
            return None
        return members[0]

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Member:
        bot = ctx.bot
        match = self._get_id_match(argument) or re.match(r'<@!?([0-9]{15,20})>$', argument)
        guild = ctx.guild
        result = None
        user_id = None

        if match is None:
            # not a mention...
            if guild:
                result = guild.get_member_named(argument)
            else:
                result = _get_from_guilds(bot, 'get_member_named', argument)
        else:
            user_id = int(match.group(1))
            if guild:
                result = guild.get_member(user_id) or _utils_get(ctx.message.mentions, id=user_id)
            else:
                result = _get_from_guilds(bot, 'get_member', user_id)

        if not isinstance(result, discord.Member):
            if guild is None:
                raise MemberNotFound(argument)

            if user_id is not None:
                result = await self.query_member_by_id(bot, guild, user_id)
            else:
                result = await self.query_member_named(guild, argument)

            if not result:
                raise MemberNotFound(argument)

        return result


class UserConverter(IDConverter[discord.User]):
    """Converts to a :class:`~discord.User`.

    All lookups are via the global user cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by username#discriminator (deprecated).
    4. Lookup by username#0 (deprecated, only gets users that migrated from their discriminator).
    5. Lookup by user name.
    6. Lookup by global name.

    .. versionchanged:: 1.5
         Raise :exc:`.UserNotFound` instead of generic :exc:`.BadArgument`

    .. versionchanged:: 1.6
        This converter now lazily fetches users from the HTTP APIs if an ID is passed
        and it's not available in cache.

    .. deprecated:: 2.3
        Looking up users by discriminator will be removed in a future version due to
        the removal of discriminators in an API change.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.User:
        match = self._get_id_match(argument) or re.match(r'<@!?([0-9]{15,20})>$', argument)
        result = None
        state = ctx._state

        if match is not None:
            user_id = int(match.group(1))
            result = ctx.bot.get_user(user_id) or _utils_get(ctx.message.mentions, id=user_id)
            if result is None:
                try:
                    result = await ctx.bot.fetch_user(user_id)
                except discord.HTTPException:
                    raise UserNotFound(argument) from None

            return result  # type: ignore

        username, _, discriminator = argument.rpartition('#')

        # If # isn't found then "discriminator" actually has the username
        if not username:
            discriminator, username = username, discriminator

        if discriminator == '0' or (len(discriminator) == 4 and discriminator.isdigit()):
            predicate = lambda u: u.name == username and u.discriminator == discriminator
        else:
            predicate = lambda u: u.name == argument or u.global_name == argument

        result = discord.utils.find(predicate, state._users.values())
        if result is None:
            raise UserNotFound(argument)

        return result


class PartialMessageConverter(Converter[discord.PartialMessage]):
    """Converts to a :class:`discord.PartialMessage`.

    .. versionadded:: 1.7

    The creation strategy is as follows (in order):

    1. By "{channel ID}-{message ID}" (retrieved by shift-clicking on "Copy ID")
    2. By message ID (The message is assumed to be in the context channel.)
    3. By message URL
    """

    @staticmethod
    def _get_id_matches(ctx: Context[BotT], argument: str) -> Tuple[Optional[int], int, int]:
        id_regex = re.compile(r'(?:(?P<channel_id>[0-9]{15,20})-)?(?P<message_id>[0-9]{15,20})$')
        link_regex = re.compile(
            r'https?://(?:(ptb|canary|www)\.)?discord(?:app)?\.com/channels/'
            r'(?P<guild_id>[0-9]{15,20}|@me)'
            r'/(?P<channel_id>[0-9]{15,20})/(?P<message_id>[0-9]{15,20})/?$'
        )
        match = id_regex.match(argument) or link_regex.match(argument)
        if not match:
            raise MessageNotFound(argument)
        data = match.groupdict()
        channel_id = discord.utils._get_as_snowflake(data, 'channel_id') or ctx.channel.id
        message_id = int(data['message_id'])
        guild_id = data.get('guild_id')
        if guild_id is None:
            guild_id = ctx.guild and ctx.guild.id
        elif guild_id == '@me':
            guild_id = None
        else:
            guild_id = int(guild_id)
        return guild_id, message_id, channel_id

    @staticmethod
    def _resolve_channel(
        ctx: Context[BotT], guild_id: Optional[int], channel_id: Optional[int]
    ) -> Optional[Union[Channel, Thread]]:
        if channel_id is None:
            # we were passed just a message id so we can assume the channel is the current context channel
            return ctx.channel

        if guild_id is not None:
            guild = ctx.bot.get_guild(guild_id)
            if guild is None:
                return None
            return guild._resolve_channel(channel_id)

        return ctx.bot.get_channel(channel_id)

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.PartialMessage:
        guild_id, message_id, channel_id = self._get_id_matches(ctx, argument)
        channel = self._resolve_channel(ctx, guild_id, channel_id)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            raise ChannelNotFound(channel_id)
        return discord.PartialMessage(channel=channel, id=message_id)


class MessageConverter(IDConverter[discord.Message]):
    """Converts to a :class:`discord.Message`.

    .. versionadded:: 1.1

    The lookup strategy is as follows (in order):

    1. Lookup by "{channel ID}-{message ID}" (retrieved by shift-clicking on "Copy ID")
    2. Lookup by message ID (the message **must** be in the context channel)
    3. Lookup by message URL

    .. versionchanged:: 1.5
         Raise :exc:`.ChannelNotFound`, :exc:`.MessageNotFound` or :exc:`.ChannelNotReadable` instead of generic :exc:`.BadArgument`
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Message:
        guild_id, message_id, channel_id = PartialMessageConverter._get_id_matches(ctx, argument)
        message = ctx.bot._connection._get_message(message_id)
        if message:
            return message
        channel = PartialMessageConverter._resolve_channel(ctx, guild_id, channel_id)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            raise ChannelNotFound(channel_id)
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            raise MessageNotFound(argument)
        except discord.Forbidden:
            raise ChannelNotReadable(channel)  # type: ignore # type-checker thinks channel could be a DMChannel at this point


class GuildChannelConverter(IDConverter[discord.abc.GuildChannel]):
    """Converts to a :class:`~discord.abc.GuildChannel`.

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name.

    .. versionadded:: 2.0

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.abc.GuildChannel:
        return self._resolve_channel(ctx, argument, 'channels', discord.abc.GuildChannel)

    @staticmethod
    def _parse_from_url(argument: str) -> Optional[re.Match[str]]:
        link_regex = re.compile(
            r'https?://(?:(?:ptb|canary|www)\.)?discord(?:app)?\.com/channels/'
            r'(?:[0-9]{15,20}|@me)'
            r'/([0-9]{15,20})(?:/(?:[0-9]{15,20})/?)?$'
        )
        return link_regex.match(argument)

    @staticmethod
    def _resolve_channel(ctx: Context[BotT], argument: str, attribute: str, type: Type[CT]) -> CT:
        bot = ctx.bot

        match = (
            IDConverter._get_id_match(argument)
            or re.match(r'<#([0-9]{15,20})>$', argument)
            or GuildChannelConverter._parse_from_url(argument)
        )
        result = None
        guild = ctx.guild

        if match is None:
            # not a mention
            if guild:
                iterable: Iterable[CT] = getattr(guild, attribute)
                result: Optional[CT] = discord.utils.get(iterable, name=argument)
            else:

                def check(c):
                    return isinstance(c, type) and c.name == argument

                result = discord.utils.find(check, bot.get_all_channels())  # type: ignore
        else:
            channel_id = int(match.group(1))
            if guild:
                # guild.get_channel returns an explicit union instead of the base class
                result = guild.get_channel(channel_id)  # type: ignore
            else:
                result = _get_from_guilds(bot, 'get_channel', channel_id)

        if not isinstance(result, type):
            raise ChannelNotFound(argument)

        return result

    @staticmethod
    def _resolve_thread(ctx: Context[BotT], argument: str, attribute: str, type: Type[TT]) -> TT:
        match = (
            IDConverter._get_id_match(argument)
            or re.match(r'<#([0-9]{15,20})>$', argument)
            or GuildChannelConverter._parse_from_url(argument)
        )
        result = None
        guild = ctx.guild

        if match is None:
            # not a mention
            if guild:
                iterable: Iterable[TT] = getattr(guild, attribute)
                result: Optional[TT] = discord.utils.get(iterable, name=argument)
        else:
            thread_id = int(match.group(1))
            if guild:
                result = guild.get_thread(thread_id)  # type: ignore

        if not result or not isinstance(result, type):
            raise ThreadNotFound(argument)

        return result


class TextChannelConverter(IDConverter[discord.TextChannel]):
    """Converts to a :class:`~discord.TextChannel`.

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name

    .. versionchanged:: 1.5
         Raise :exc:`.ChannelNotFound` instead of generic :exc:`.BadArgument`

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.TextChannel:
        return GuildChannelConverter._resolve_channel(ctx, argument, 'text_channels', discord.TextChannel)


class VoiceChannelConverter(IDConverter[discord.VoiceChannel]):
    """Converts to a :class:`~discord.VoiceChannel`.

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name

    .. versionchanged:: 1.5
         Raise :exc:`.ChannelNotFound` instead of generic :exc:`.BadArgument`

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.VoiceChannel:
        return GuildChannelConverter._resolve_channel(ctx, argument, 'voice_channels', discord.VoiceChannel)


class StageChannelConverter(IDConverter[discord.StageChannel]):
    """Converts to a :class:`~discord.StageChannel`.

    .. versionadded:: 1.7

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.StageChannel:
        return GuildChannelConverter._resolve_channel(ctx, argument, 'stage_channels', discord.StageChannel)


class CategoryChannelConverter(IDConverter[discord.CategoryChannel]):
    """Converts to a :class:`~discord.CategoryChannel`.

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.

    .. versionchanged:: 1.5
         Raise :exc:`.ChannelNotFound` instead of generic :exc:`.BadArgument`
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.CategoryChannel:
        return GuildChannelConverter._resolve_channel(ctx, argument, 'categories', discord.CategoryChannel)


class ThreadConverter(IDConverter[discord.Thread]):
    """Converts to a :class:`~discord.Thread`.

    All lookups are via the local guild.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name.

    .. versionadded: 2.0

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Thread:
        return GuildChannelConverter._resolve_thread(ctx, argument, 'threads', discord.Thread)


class ForumChannelConverter(IDConverter[discord.ForumChannel]):
    """Converts to a :class:`~discord.ForumChannel`.

    All lookups are via the local guild. If in a DM context, then the lookup
    is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by channel URL.
    4. Lookup by name

    .. versionadded:: 2.0

    .. versionchanged:: 2.4
        Add lookup by channel URL, accessed via "Copy Link" in the Discord client within channels.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.ForumChannel:
        return GuildChannelConverter._resolve_channel(ctx, argument, 'forums', discord.ForumChannel)


class ColourConverter(Converter[discord.Colour]):
    """Converts to a :class:`~discord.Colour`.

    .. versionchanged:: 1.5
        Add an alias named ColorConverter

    The following formats are accepted:

    - ``0x<hex>``
    - ``#<hex>``
    - ``0x#<hex>``
    - ``rgb(<number>, <number>, <number>)``
    - Any of the ``classmethod`` in :class:`~discord.Colour`

        - The ``_`` in the name can be optionally replaced with spaces.

    Like CSS, ``<number>`` can be either 0-255 or 0-100% and ``<hex>`` can be
    either a 6 digit hex number or a 3 digit hex shortcut (e.g. #fff).

    .. versionchanged:: 1.5
         Raise :exc:`.BadColourArgument` instead of generic :exc:`.BadArgument`

    .. versionchanged:: 1.7
        Added support for ``rgb`` function and 3-digit hex shortcuts
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Colour:
        try:
            return discord.Colour.from_str(argument)
        except ValueError:
            arg = argument.lower().replace(' ', '_')
            method = getattr(discord.Colour, arg, None)
            if arg.startswith('from_') or method is None or not inspect.ismethod(method):
                raise BadColourArgument(arg)
            return method()


ColorConverter = ColourConverter


class RoleConverter(IDConverter[discord.Role]):
    """Converts to a :class:`~discord.Role`.

    All lookups are via the local guild. If in a DM context, the converter raises
    :exc:`.NoPrivateMessage` exception.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by name with fuzzy search and ranking.

    .. versionchanged:: 1.5
         Raise :exc:`.RoleNotFound` instead of generic :exc:`.BadArgument`
    """

    def levenshtein_distance(self, s1: str, s2: str) -> int:
        """
        Calculate the Levenshtein distance between two strings.

        Args:
            s1 (str): The first string.
            s2 (str): The second string.

        Returns:
            int: The Levenshtein distance between the two strings.
        """
        if len(s1) < len(s2):
            return self.levenshtein_distance(s2, s1)

        if not s2:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    def normalized_levenshtein(self, s1: str, s2: str) -> float:
        """
        Calculate the normalized Levenshtein distance between two strings.

        Args:
            s1 (str): The first string.
            s2 (str): The second string.

        Returns:
            float: The normalized Levenshtein distance between the two strings.
        """
        distance = self.levenshtein_distance(s1, s2)
        max_len = max(len(s1), len(s2))
        return distance / max_len

    def word_match_score(self, query: str, choice: str) -> int:
        """
        Calculate the word match score between the query and a choice.

        Args:
            query (str): The search query string.
            choice (str): A possible match string.

        Returns:
            int: The number of words from the query that match the choice.
        """
        query_words = set(query.lower().split())
        choice_words = set(choice.lower().split())
        return len(query_words.intersection(choice_words))

    def fuzzy_search(self, query: str, choices: List[discord.Role], threshold: float = 3) -> Optional[discord.Role]:
        """
        Perform a fuzzy search to find the closest match to the query in a list of choices.
        Uses exact substring matching, word matching, and Levenshtein distance.

        Args:
            query (str): The search query string.
            choices (List[discord.Role]): A list of possible matches.
            threshold (float, optional): The maximum normalized Levenshtein distance allowed for a match.
                                         If the best match exceeds this distance, return None.

        Returns:
            Optional[discord.Role]: The closest match to the query from the list of choices.
                                    If no match is within the threshold, or the list is empty, returns None.
        """
        best_match: Optional[discord.Role] = None
        best_score = -1
        min_distance = float('inf')

        query_lower = query.lower()

        for choice in choices:
            choice_name = choice.name.lower()

            if query_lower == choice_name:
                return choice

            if query_lower in choice_name:
                return choice

            score = self.word_match_score(query_lower, choice_name)
            distance = self.normalized_levenshtein(query_lower, choice_name)

            if (score > best_score) or (score == best_score and distance < min_distance):
                best_score = score
                min_distance = distance
                best_match = choice

        if best_match is not None and min_distance <= threshold:
            return best_match

        return None

    async def convert(self, ctx: Context, argument: str) -> Union[List[discord.Role], discord.Role]:
        guild = ctx.guild
        if not guild:
            raise NoPrivateMessage()

        result = None

        match = self._get_id_match(argument) or re.match(r'<@&([0-9]{15,20})>$', argument)
        if match:
            result = guild.get_role(int(match.group(1)))

        if result is None:
            # If no exact match is found, attempt a fuzzy search with ranking
            result = self.fuzzy_search(argument, guild.roles, threshold=3)  # type: ignore

        if result is None:
            raise RoleNotFound(argument)

        return result


class RolesConverter(IDConverter[List[discord.Role]]):
    """Converts to a :class:`~List[discord.Role]`.

    All lookups are via the local guild. If in a DM context, the converter raises
    :exc:`.NoPrivateMessage` exception.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by mention.
    3. Lookup by name with fuzzy search and ranking.

    .. versionchanged:: 1.5
         Raise :exc:`.RoleNotFound` instead of generic :exc:`.BadArgument`
    """

    async def convert(self, ctx: Context, argument: str) -> List[discord.Role]:
        guild = ctx.guild
        if not guild:
            raise NoPrivateMessage()

        results: List[discord.Role] = []
        for arg in argument.split(','):
            role = await RoleConverter().convert(ctx, arg.strip())
            assert isinstance(role, discord.Role), "Role must be a discord.Role instance."
            results.append(role)

        if not results:
            raise RoleNotFound(argument)

        return results


class TimeDeltaConverter(Converter[datetime.timedelta]):
    """Converts to a :class:`~datetime.timedelta`.

    The following formats are accepted:

    - ``<number>[s|seconds|ms|milliseconds|m|minutes|h|hours|d|days|w|weeks|y|years]``

    The time units are case-insensitive and can be abbreviated (e.g. ``s`` for seconds, ``ms`` for milliseconds).

    Examples:

    - ``10s``
    - ``10 minutes``
    """

    async def convert(self, ctx: Context, argument: str) -> datetime.timedelta:
        """Converts a string into a timedelta object."""
        base_units = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'y': 31536000}

        time_units = {
            key: value
            for unit, value in base_units.items()
            for key in (
                unit,
                f'{unit}s',
                f'{unit}ec',
                f'{unit}ecs',
                f'{unit}in',
                f'{unit}ins',
                f'{unit}inute',
                f'{unit}inutes',
                f'{unit}our',
                f'{unit}ours',
                f'{unit}ay',
                unit + 'ays',
                unit + 'eek',
                unit + 'eeks',
                unit + 'ear',
                unit + 'ears',
                unit + 'illisecond',
                unit + 'illiseconds',
            )
        }

        matches = re.findall(r'(\d+\.?\d*)\s*([a-zA-Z]+)', argument)
        if not matches:
            raise BadArgument(f"Could not parse any time units from '{argument}'")

        time = datetime.timedelta(
            seconds=sum(float(value) * time_units[unit] for value, unit in matches if unit in time_units)
        )
        if time.total_seconds() == 0:
            raise BadArgument(f"Invalid time unit in '{argument}'")
        return time


class GameConverter(Converter[discord.Game]):
    """Converts to a :class:`~discord.Game`."""

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Game:
        return discord.Game(name=argument)


class InviteConverter(Converter[discord.Invite]):
    """Converts to a :class:`~discord.Invite`.

    This is done via an HTTP request using :meth:`.Bot.fetch_invite`.

    .. versionchanged:: 1.5
         Raise :exc:`.BadInviteArgument` instead of generic :exc:`.BadArgument`
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Invite:
        try:
            invite = await ctx.bot.fetch_invite(argument)
            return invite
        except Exception as exc:
            raise BadInviteArgument(argument) from exc


class GuildConverter(IDConverter[discord.Guild]):
    """Converts to a :class:`~discord.Guild`.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by name. (There is no disambiguation for Guilds with multiple matching names).

    .. versionadded:: 1.7
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Guild:
        match = self._get_id_match(argument)
        result = None

        if match is not None:
            guild_id = int(match.group(1))
            result = ctx.bot.get_guild(guild_id)

        if result is None:
            result = discord.utils.get(ctx.bot.guilds, name=argument)

            if result is None:
                raise GuildNotFound(argument)
        return result


class EmojiConverter(IDConverter[discord.Emoji]):
    """Converts to a :class:`~discord.Emoji`.

    All lookups are done for the local guild first, if available. If that lookup
    fails, then it checks the client's global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by extracting ID from the emoji.
    3. Lookup by name

    .. versionchanged:: 1.5
         Raise :exc:`.EmojiNotFound` instead of generic :exc:`.BadArgument`
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.Emoji:
        match = self._get_id_match(argument) or re.match(r'<a?:[a-zA-Z0-9\_]{1,32}:([0-9]{15,20})>$', argument)
        result = None
        bot = ctx.bot
        guild = ctx.guild

        if match is None:
            # Try to get the emoji by name. Try local guild first.
            if guild:
                result = discord.utils.get(guild.emojis, name=argument)

            if result is None:
                result = discord.utils.get(bot.emojis, name=argument)
        else:
            emoji_id = int(match.group(1))

            # Try to look up emoji by id.
            result = bot.get_emoji(emoji_id)

        if result is None:
            raise EmojiNotFound(argument)

        return result


class PartialEmojiConverter(Converter[discord.PartialEmoji]):
    """Converts to a :class:`~discord.PartialEmoji`.

    This is done by extracting the animated flag, name and ID from the emoji.

    .. versionchanged:: 1.5
         Raise :exc:`.PartialEmojiConversionFailure` instead of generic :exc:`.BadArgument`
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.PartialEmoji:
        match = re.match(r'<(a?):([a-zA-Z0-9\_]{1,32}):([0-9]{15,20})>$', argument)

        if match:
            emoji_animated = bool(match.group(1))
            emoji_name = match.group(2)
            emoji_id = int(match.group(3))

            return discord.PartialEmoji.with_state(
                ctx.bot._connection, animated=emoji_animated, name=emoji_name, id=emoji_id
            )

        raise PartialEmojiConversionFailure(argument)


class BasicEmojiConverter(IDConverter[discord.BasicEmoji]):
    """Converts to a :class:`~discord.BasicEmoji`.

    All lookups are done for the local guild first, if available. If that lookup
    fails, then it checks the client's global cache.

    If both the local guild and the global cache fail to find the emoji, the converter
    will attempt a unicode emoji lookup.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by extracting ID from the emoji.
    3. Lookup by name.
    4. Lookup by unicode emoji.

    If the emoji is not found in any of these places, the converter will raise
    :exc:`.BasicEmojiConversionFailure`.
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.BasicEmoji:
        match = self._get_id_match(argument) or re.match(r'<a?:[a-zA-Z0-9\_]{1,32}:([0-9]{15,20})>$', argument)
        result = None
        bot = ctx.bot
        guild = ctx.guild

        if match is None:
            if guild:
                result = discord.utils.get(guild.emojis, name=argument)

            if result is None:
                result = discord.utils.get(bot.emojis, name=argument)

        if guild and match is not None:
            # Try to look up emoji by id.
            result = guild.get_emoji(int(match.group(1)))

        # Unicode match
        unicode = re.match(
            r'(?:\U0001f1e6[\U0001f1e8-\U0001f1ec\U0001f1ee\U0001f1f1\U0001f1f2\U0001f1f4\U0001f1f6-\U0001f1fa\U0001f1fc\U0001f1fd\U0001f1ff])|(?:\U0001f1e7[\U0001f1e6\U0001f1e7\U0001f1e9-\U0001f1ef\U0001f1f1-\U0001f1f4\U0001f1f6-\U0001f1f9\U0001f1fb\U0001f1fc\U0001f1fe\U0001f1ff])|(?:\U0001f1e8[\U0001f1e6\U0001f1e8\U0001f1e9\U0001f1eb-\U0001f1ee\U0001f1f0-\U0001f1f5\U0001f1f7\U0001f1fa-\U0001f1ff])|(?:\U0001f1e9[\U0001f1ea\U0001f1ec\U0001f1ef\U0001f1f0\U0001f1f2\U0001f1f4\U0001f1ff])|(?:\U0001f1ea[\U0001f1e6\U0001f1e8\U0001f1ea\U0001f1ec\U0001f1ed\U0001f1f7-\U0001f1fa])|(?:\U0001f1eb[\U0001f1ee-\U0001f1f0\U0001f1f2\U0001f1f4\U0001f1f7])|(?:\U0001f1ec[\U0001f1e6\U0001f1e7\U0001f1e9-\U0001f1ee\U0001f1f1-\U0001f1f3\U0001f1f5-\U0001f1fa\U0001f1fc\U0001f1fe])|(?:\U0001f1ed[\U0001f1f0\U0001f1f2\U0001f1f3\U0001f1f7\U0001f1f9\U0001f1fa])|(?:\U0001f1ee[\U0001f1e8-\U0001f1ea\U0001f1f1-\U0001f1f4\U0001f1f6-\U0001f1f9])|(?:\U0001f1ef[\U0001f1ea\U0001f1f2\U0001f1f4\U0001f1f5])|(?:\U0001f1f0[\U0001f1ea\U0001f1ec-\U0001f1ee\U0001f1f2\U0001f1f3\U0001f1f5\U0001f1f7\U0001f1fc\U0001f1fe\U0001f1ff])|(?:\U0001f1f1[\U0001f1e6-\U0001f1e8\U0001f1ee\U0001f1f0\U0001f1f7-\U0001f1fb\U0001f1fe])|(?:\U0001f1f2[\U0001f1e6\U0001f1e8-\U0001f1ed\U0001f1f0-\U0001f1ff])|(?:\U0001f1f3[\U0001f1e6\U0001f1e8\U0001f1ea-\U0001f1ec\U0001f1ee\U0001f1f1\U0001f1f4\U0001f1f5\U0001f1f7\U0001f1fa\U0001f1ff])|\U0001f1f4\U0001f1f2|(?:\U0001f1f4[\U0001f1f2])|(?:\U0001f1f5[\U0001f1e6\U0001f1ea-\U0001f1ed\U0001f1f0-\U0001f1f3\U0001f1f7-\U0001f1f9\U0001f1fc\U0001f1fe])|\U0001f1f6\U0001f1e6|(?:\U0001f1f6[\U0001f1e6])|(?:\U0001f1f7[\U0001f1ea\U0001f1f4\U0001f1f8\U0001f1fa\U0001f1fc])|(?:\U0001f1f8[\U0001f1e6-\U0001f1ea\U0001f1ec-\U0001f1f4\U0001f1f7-\U0001f1f9\U0001f1fb\U0001f1fd-\U0001f1ff])|(?:\U0001f1f9[\U0001f1e6\U0001f1e8\U0001f1e9\U0001f1eb-\U0001f1ed\U0001f1ef-\U0001f1f4\U0001f1f7\U0001f1f9\U0001f1fb\U0001f1fc\U0001f1ff])|(?:\U0001f1fa[\U0001f1e6\U0001f1ec\U0001f1f2\U0001f1f8\U0001f1fe\U0001f1ff])|(?:\U0001f1fb[\U0001f1e6\U0001f1e8\U0001f1ea\U0001f1ec\U0001f1ee\U0001f1f3\U0001f1fa])|(?:\U0001f1fc[\U0001f1eb\U0001f1f8])|\U0001f1fd\U0001f1f0|(?:\U0001f1fd[\U0001f1f0])|(?:\U0001f1fe[\U0001f1ea\U0001f1f9])|(?:\U0001f1ff[\U0001f1e6\U0001f1f2\U0001f1fc])|(?:\U0001f3f3\ufe0f\u200d\U0001f308)|(?:\U0001f441\u200d\U0001f5e8)|(?:[\U0001f468\U0001f469]\u200d\u2764\ufe0f\u200d(?:\U0001f48b\u200d)?[\U0001f468\U0001f469])|(?:(?:(?:\U0001f468\u200d[\U0001f468\U0001f469])|(?:\U0001f469\u200d\U0001f469))(?:(?:\u200d\U0001f467(?:\u200d[\U0001f467\U0001f466])?)|(?:\u200d\U0001f466\u200d\U0001f466)))|(?:(?:(?:\U0001f468\u200d\U0001f468)|(?:\U0001f469\u200d\U0001f469))\u200d\U0001f466)|[\u2194-\u2199]|[\u23e9-\u23f3]|[\u23f8-\u23fa]|[\u25fb-\u25fe]|[\u2600-\u2604]|[\u2638-\u263a]|[\u2648-\u2653]|[\u2692-\u2694]|[\u26f0-\u26f5]|[\u26f7-\u26fa]|[\u2708-\u270d]|[\u2753-\u2755]|[\u2795-\u2797]|[\u2b05-\u2b07]|[\U0001f191-\U0001f19a]|[\U0001f1e6-\U0001f1ff]|[\U0001f232-\U0001f23a]|[\U0001f300-\U0001f321]|[\U0001f324-\U0001f393]|[\U0001f399-\U0001f39b]|[\U0001f39e-\U0001f3f0]|[\U0001f3f3-\U0001f3f5]|[\U0001f3f7-\U0001f3fa]|[\U0001f400-\U0001f4fd]|[\U0001f4ff-\U0001f53d]|[\U0001f549-\U0001f54e]|[\U0001f550-\U0001f567]|[\U0001f573-\U0001f57a]|[\U0001f58a-\U0001f58d]|[\U0001f5c2-\U0001f5c4]|[\U0001f5d1-\U0001f5d3]|[\U0001f5dc-\U0001f5de]|[\U0001f5fa-\U0001f64f]|[\U0001f680-\U0001f6c5]|[\U0001f6cb-\U0001f6d2]|[\U0001f6e0-\U0001f6e5]|[\U0001f6f3-\U0001f6f6]|[\U0001f910-\U0001f91e]|[\U0001f920-\U0001f927]|[\U0001f933-\U0001f93a]|[\U0001f93c-\U0001f93e]|[\U0001f940-\U0001f945]|[\U0001f947-\U0001f94b]|[\U0001f950-\U0001f95e]|[\U0001f980-\U0001f991]|\u00a9|\u00ae|\u203c|\u2049|\u2122|\u2139|\u21a9|\u21aa|\u231a|\u231b|\u2328|\u23cf|\u24c2|\u25aa|\u25ab|\u25b6|\u25c0|\u260e|\u2611|\u2614|\u2615|\u2618|\u261d|\u2620|\u2622|\u2623|\u2626|\u262a|\u262e|\u262f|\u2660|\u2663|\u2665|\u2666|\u2668|\u267b|\u267f|\u2696|\u2697|\u2699|\u269b|\u269c|\u26a0|\u26a1|\u26aa|\u26ab|\u26b0|\u26b1|\u26bd|\u26be|\u26c4|\u26c5|\u26c8|\u26ce|\u26cf|\u26d1|\u26d3|\u26d4|\u26e9|\u26ea|\u26fd|\u2702|\u2705|\u270f|\u2712|\u2714|\u2716|\u271d|\u2721|\u2728|\u2733|\u2734|\u2744|\u2747|\u274c|\u274e|\u2757|\u2763|\u2764|\u27a1|\u27b0|\u27bf|\u2934|\u2935|\u2b1b|\u2b1c|\u2b50|\u2b55|\u3030|\u303d|\u3297|\u3299|\U0001f004|\U0001f0cf|\U0001f170|\U0001f171|\U0001f17e|\U0001f17f|\U0001f18e|\U0001f201|\U0001f202|\U0001f21a|\U0001f22f|\U0001f250|\U0001f251|\U0001f396|\U0001f397|\U0001f56f|\U0001f570|\U0001f587|\U0001f590|\U0001f595|\U0001f596|\U0001f5a4|\U0001f5a5|\U0001f5a8|\U0001f5b1|\U0001f5b2|\U0001f5bc|\U0001f5e1|\U0001f5e3|\U0001f5e8|\U0001f5ef|\U0001f5f3|\U0001f6e9|\U0001f6eb|\U0001f6ec|\U0001f6f0|\U0001f930|\U0001f9c0|[#|0-9]\u20e3',
            argument,
        )

        if unicode:
            result = argument

        if result is None:
            raise BasicEmojiConversionFailure(argument)

        if isinstance(result, str):
            return discord.BasicEmoji(name=result)
        return discord.BasicEmoji(name=str(result), is_custom_emoji=True)


class GuildStickerConverter(IDConverter[discord.GuildSticker]):
    """Converts to a :class:`~discord.GuildSticker`.

    All lookups are done for the local guild first, if available. If that lookup
    fails, then it checks the client's global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by name.

    .. versionadded:: 2.0
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.GuildSticker:
        match = self._get_id_match(argument)
        result = None
        bot = ctx.bot
        guild = ctx.guild

        if match is None:
            # Try to get the sticker by name. Try local guild first.
            if guild:
                result = discord.utils.get(guild.stickers, name=argument)

            if result is None:
                result = discord.utils.get(bot.stickers, name=argument)
        else:
            sticker_id = int(match.group(1))

            # Try to look up sticker by id.
            result = bot.get_sticker(sticker_id)

        if result is None:
            raise GuildStickerNotFound(argument)

        return result


class ScheduledEventConverter(IDConverter[discord.ScheduledEvent]):
    """Converts to a :class:`~discord.ScheduledEvent`.

    Lookups are done for the local guild if available. Otherwise, for a DM context,
    lookup is done by the global cache.

    The lookup strategy is as follows (in order):

    1. Lookup by ID.
    2. Lookup by url.
    3. Lookup by name.

    .. versionadded:: 2.0
    """

    async def convert(self, ctx: Context[BotT], argument: str) -> discord.ScheduledEvent:
        guild = ctx.guild
        match = self._get_id_match(argument)
        result = None

        if match:
            # ID match
            event_id = int(match.group(1))
            if guild:
                result = guild.get_scheduled_event(event_id)
            else:
                for guild in ctx.bot.guilds:
                    result = guild.get_scheduled_event(event_id)
                    if result:
                        break
        else:
            pattern = (
                r'https?://(?:(ptb|canary|www)\.)?discord\.com/events/'
                r'(?P<guild_id>[0-9]{15,20})/'
                r'(?P<event_id>[0-9]{15,20})$'
            )
            match = re.match(pattern, argument, flags=re.I)
            if match:
                # URL match
                guild = ctx.bot.get_guild(int(match.group('guild_id')))

                if guild:
                    event_id = int(match.group('event_id'))
                    result = guild.get_scheduled_event(event_id)
            else:
                # lookup by name
                if guild:
                    result = discord.utils.get(guild.scheduled_events, name=argument)
                else:
                    for guild in ctx.bot.guilds:
                        result = discord.utils.get(guild.scheduled_events, name=argument)
                        if result:
                            break
        if result is None:
            raise ScheduledEventNotFound(argument)

        return result


class clean_content(Converter[str]):
    """Converts the argument to mention scrubbed version of
    said content.

    This behaves similarly to :attr:`~discord.Message.clean_content`.

    Attributes
    ------------
    fix_channel_mentions: :class:`bool`
        Whether to clean channel mentions.
    use_nicknames: :class:`bool`
        Whether to use nicknames when transforming mentions.
    escape_markdown: :class:`bool`
        Whether to also escape special markdown characters.
    remove_markdown: :class:`bool`
        Whether to also remove special markdown characters. This option is not supported with ``escape_markdown``

        .. versionadded:: 1.7
    """

    def __init__(
        self,
        *,
        fix_channel_mentions: bool = False,
        use_nicknames: bool = True,
        escape_markdown: bool = False,
        remove_markdown: bool = False,
    ) -> None:
        self.fix_channel_mentions = fix_channel_mentions
        self.use_nicknames = use_nicknames
        self.escape_markdown = escape_markdown
        self.remove_markdown = remove_markdown

    async def convert(self, ctx: Context[BotT], argument: str) -> str:
        msg = ctx.message

        if ctx.guild:

            def resolve_member(id: int) -> str:
                m = _utils_get(msg.mentions, id=id) or ctx.guild.get_member(id)  # type: ignore
                return f'@{m.display_name if self.use_nicknames else m.name}' if m else '@deleted-user'

            def resolve_role(id: int) -> str:
                r = _utils_get(msg.role_mentions, id=id) or ctx.guild.get_role(id)  # type: ignore
                return f'@{r.name}' if r else '@deleted-role'

        else:

            def resolve_member(id: int) -> str:
                m = _utils_get(msg.mentions, id=id) or ctx.bot.get_user(id)
                return f'@{m.display_name}' if m else '@deleted-user'

            def resolve_role(id: int) -> str:
                return '@deleted-role'

        if self.fix_channel_mentions and ctx.guild:

            def resolve_channel(id: int) -> str:
                c = ctx.guild._resolve_channel(id)  # type: ignore
                return f'#{c.name}' if c else '#deleted-channel'

        else:

            def resolve_channel(id: int) -> str:
                return f'<#{id}>'

        transforms = {
            '@': resolve_member,
            '@!': resolve_member,
            '#': resolve_channel,
            '@&': resolve_role,
        }

        def repl(match: re.Match) -> str:
            type = match[1]
            id = int(match[2])
            transformed = transforms[type](id)
            return transformed

        result = re.sub(r'<(@[!&]?|#)([0-9]{15,20})>', repl, argument)
        if self.escape_markdown:
            result = discord.utils.escape_markdown(result)
        elif self.remove_markdown:
            result = discord.utils.remove_markdown(result)

        # Completely ensure no mentions escape:
        return discord.utils.escape_mentions(result)


class Greedy(List[T]):
    r"""A special converter that greedily consumes arguments until it can't.
    As a consequence of this behaviour, most input errors are silently discarded,
    since it is used as an indicator of when to stop parsing.

    When a parser error is met the greedy converter stops converting, undoes the
    internal string parsing routine, and continues parsing regularly.

    For example, in the following code:

    .. code-block:: python3

        @commands.command()
        async def test(ctx, numbers: Greedy[int], reason: str):
            await ctx.send("numbers: {}, reason: {}".format(numbers, reason))

    An invocation of ``[p]test 1 2 3 4 5 6 hello`` would pass ``numbers`` with
    ``[1, 2, 3, 4, 5, 6]`` and ``reason`` with ``hello``\.

    For more information, check :ref:`ext_commands_special_converters`.

    .. note::

        For interaction based contexts the conversion error is propagated
        rather than swallowed due to the difference in user experience with
        application commands.
    """

    __slots__ = ('converter',)

    def __init__(self, *, converter: T) -> None:
        self.converter: T = converter

    def __repr__(self) -> str:
        converter = getattr(self.converter, '__name__', repr(self.converter))
        return f'Greedy[{converter}]'

    def __class_getitem__(cls, params: Union[Tuple[T], T]) -> Greedy[T]:
        if not isinstance(params, tuple):
            params = (params,)
        if len(params) != 1:
            raise TypeError('Greedy[...] only takes a single argument')
        converter = params[0]

        args = getattr(converter, '__args__', ())
        if discord.utils.PY_310 and converter.__class__ is types.UnionType:  # type: ignore
            converter = Union[args]  # type: ignore

        origin = getattr(converter, '__origin__', None)

        if not (callable(converter) or isinstance(converter, Converter) or origin is not None):
            raise TypeError('Greedy[...] expects a type or a Converter instance.')

        if converter in (str, type(None)) or origin is Greedy:
            raise TypeError(f'Greedy[{converter.__name__}] is invalid.')  # type: ignore

        if origin is Union and type(None) in args:
            raise TypeError(f'Greedy[{converter!r}] is invalid.')

        return cls(converter=converter)

    @property
    def constructed_converter(self) -> Any:
        # Only construct a converter once in order to maintain state between convert calls
        if (
            inspect.isclass(self.converter)
            and issubclass(self.converter, Converter)
            and not inspect.ismethod(self.converter.convert)
        ):
            return self.converter()
        return self.converter


if TYPE_CHECKING:
    from typing_extensions import Annotated as Range
else:

    class Range:
        """A special converter that can be applied to a parameter to require a numeric
        or string type to fit within the range provided.

        During type checking time this is equivalent to :obj:`typing.Annotated` so type checkers understand
        the intent of the code.

        Some example ranges:

        - ``Range[int, 10]`` means the minimum is 10 with no maximum.
        - ``Range[int, None, 10]`` means the maximum is 10 with no minimum.
        - ``Range[int, 1, 10]`` means the minimum is 1 and the maximum is 10.
        - ``Range[float, 1.0, 5.0]`` means the minimum is 1.0 and the maximum is 5.0.
        - ``Range[str, 1, 10]`` means the minimum length is 1 and the maximum length is 10.

        Inside a :class:`HybridCommand` this functions equivalently to :class:`discord.app_commands.Range`.

        If the value cannot be converted to the provided type or is outside the given range,
        :class:`~.ext.commands.BadArgument` or :class:`~.ext.commands.RangeError` is raised to
        the appropriate error handlers respectively.

        .. versionadded:: 2.0

        Examples
        ----------

        .. code-block:: python3

            @bot.command()
            async def range(ctx: commands.Context, value: commands.Range[int, 10, 12]):
                await ctx.send(f'Your value is {value}')
        """

        def __init__(
            self,
            *,
            annotation: Any,
            min: Optional[Union[int, float]] = None,
            max: Optional[Union[int, float]] = None,
        ) -> None:
            self.annotation: Any = annotation
            self.min: Optional[Union[int, float]] = min
            self.max: Optional[Union[int, float]] = max

            if min and max and min > max:
                raise TypeError('minimum cannot be larger than maximum')

        async def convert(self, ctx: Context[BotT], value: str) -> Union[int, float]:
            try:
                count = converted = self.annotation(value)
            except ValueError:
                raise BadArgument(
                    f'Converting to "{self.annotation.__name__}" failed for parameter "{ctx.current_parameter.name}".'
                )

            if self.annotation is str:
                count = len(value)

            if (self.min is not None and count < self.min) or (self.max is not None and count > self.max):
                raise RangeError(converted, minimum=self.min, maximum=self.max)

            return converted

        def __call__(self) -> None:
            # Trick to allow it inside typing.Union
            pass

        def __or__(self, rhs) -> Any:
            return Union[self, rhs]

        def __repr__(self) -> str:
            return f'{self.__class__.__name__}[{self.annotation.__name__}, {self.min}, {self.max}]'

        def __class_getitem__(cls, obj) -> Range:
            if not isinstance(obj, tuple):
                raise TypeError(f'expected tuple for arguments, received {obj.__class__.__name__} instead')

            if len(obj) == 2:
                obj = (*obj, None)
            elif len(obj) != 3:
                raise TypeError('Range accepts either two or three arguments with the first being the type of range.')

            annotation, min, max = obj

            if min is None and max is None:
                raise TypeError('Range must not be empty')

            if min is not None and max is not None:
                # At this point max and min are both not none
                if type(min) != type(max):
                    raise TypeError('Both min and max in Range must be the same type')

            if annotation not in (int, float, str):
                raise TypeError(f'expected int, float, or str as range type, received {annotation!r} instead')

            if annotation in (str, int):
                cast = int
            else:
                cast = float

            return cls(
                annotation=annotation,
                min=cast(min) if min is not None else None,
                max=cast(max) if max is not None else None,
            )


def _convert_to_bool(argument: str) -> bool:
    lowered = argument.lower()
    if lowered in ('yes', 'y', 'true', 't', '1', 'enable', 'on'):
        return True
    elif lowered in ('no', 'n', 'false', 'f', '0', 'disable', 'off'):
        return False
    else:
        raise BadBoolArgument(lowered)


_GenericAlias = type(List[T])  # type: ignore


def is_generic_type(tp: Any, *, _GenericAlias: type = _GenericAlias) -> bool:
    return isinstance(tp, type) and issubclass(tp, Generic) or isinstance(tp, _GenericAlias)


# TODO: List[discord.Role] should return a list of role from an example input like "admin, mod, team"
CONVERTER_MAPPING: Dict[type, Any] = {
    discord.Object: ObjectConverter,
    discord.Member: MemberConverter,
    discord.User: UserConverter,
    discord.Message: MessageConverter,
    discord.PartialMessage: PartialMessageConverter,
    discord.TextChannel: TextChannelConverter,
    discord.Invite: InviteConverter,
    discord.Guild: GuildConverter,
    discord.Role: RoleConverter,
    discord.Game: GameConverter,
    discord.Colour: ColourConverter,
    discord.VoiceChannel: VoiceChannelConverter,
    discord.StageChannel: StageChannelConverter,
    discord.Emoji: EmojiConverter,
    discord.BasicEmoji: BasicEmojiConverter,
    discord.PartialEmoji: PartialEmojiConverter,
    discord.CategoryChannel: CategoryChannelConverter,
    discord.Thread: ThreadConverter,
    discord.abc.GuildChannel: GuildChannelConverter,
    discord.GuildSticker: GuildStickerConverter,
    discord.ScheduledEvent: ScheduledEventConverter,
    discord.ForumChannel: ForumChannelConverter,
    datetime.timedelta: TimeDeltaConverter,
    List[discord.Role]: RolesConverter,
}


async def _actual_conversion(ctx: Context[BotT], converter: Any, argument: str, param: inspect.Parameter):
    if converter is bool:
        return _convert_to_bool(argument)

    try:
        module = converter.__module__
    except AttributeError:
        pass
    else:
        if module is not None and (module.startswith('discord.') and not module.endswith('converter')):
            converter = CONVERTER_MAPPING.get(converter, converter)

    origin = get_origin(converter)
    if origin and (mapped_converter := CONVERTER_MAPPING.get(converter)):
        converter = mapped_converter

    try:

        if inspect.isclass(converter) and issubclass(converter, Converter):
            if inspect.ismethod(converter.convert):
                return await converter.convert(ctx, argument)
            else:
                return await converter().convert(ctx, argument)
        elif isinstance(converter, Converter):
            return await converter.convert(ctx, argument)  # type: ignore
    except CommandError:
        raise
    except Exception as exc:
        raise ConversionError(converter, exc) from exc  # type: ignore

    try:
        return converter(argument)
    except CommandError:
        raise
    except Exception as exc:
        try:
            name = converter.__name__
        except AttributeError:
            name = converter.__class__.__name__

        raise BadArgument(f'Converting to "{name}" failed for parameter "{param.name}".') from exc


@overload
async def run_converters(
    ctx: Context[BotT], converter: Union[Type[Converter[T]], Converter[T]], argument: str, param: Parameter
) -> T: ...


@overload
async def run_converters(ctx: Context[BotT], converter: Any, argument: str, param: Parameter) -> Any: ...


async def run_converters(ctx: Context[BotT], converter: Any, argument: str, param: Parameter) -> Any:
    """|coro|

    Runs converters for a given converter, argument, and parameter.

    This function does the same work that the library does under the hood.

    .. versionadded:: 2.0

    Parameters
    ------------
    ctx: :class:`Context`
        The invocation context to run the converters under.
    converter: Any
        The converter to run, this corresponds to the annotation in the function.
    argument: :class:`str`
        The argument to convert to.
    param: :class:`Parameter`
        The parameter being converted. This is mainly for error reporting.

    Raises
    -------
    CommandError
        The converter failed to convert.

    Returns
    --------
    Any
        The resulting conversion.
    """
    origin = getattr(converter, '__origin__', None)

    if origin is Union:
        errors = []
        _NoneType = type(None)
        union_args = converter.__args__
        for conv in union_args:
            if conv is _NoneType and param.kind != param.VAR_POSITIONAL:
                ctx.view.undo()
                return None if param.required else await param.get_default(ctx)

            try:
                value = await run_converters(ctx, conv, argument, param)
            except CommandError as exc:
                errors.append(exc)
            else:
                return value

        raise BadUnionArgument(param, union_args, errors)

    if origin is Literal:
        errors = []
        conversions = {}
        literal_args = converter.__args__

        for literal in literal_args:
            literal_type = type(literal)
            try:
                value = conversions[literal_type]
            except KeyError:
                try:
                    value = await _actual_conversion(ctx, literal_type, argument, param)
                except CommandError as exc:
                    errors.append(exc)
                    conversions[literal_type] = object()
                    continue
                else:
                    conversions[literal_type] = value

            if isinstance(value, str) and isinstance(literal, str):
                if value.lower() == literal.lower():
                    return literal
            elif value == literal:
                return value

        raise BadLiteralArgument(param, literal_args, errors, argument)

    if origin and is_generic_type(converter):
        converter = CONVERTER_MAPPING.get(converter, origin)

    return await _actual_conversion(ctx, converter, argument, param)

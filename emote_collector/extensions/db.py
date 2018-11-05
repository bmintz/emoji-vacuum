#!/usr/bin/env python3
# encoding: utf-8

import asyncio
import contextlib
import datetime
import logging
import random
import re
import time

import asyncpg
import discord
from discord.ext import commands

from .. import utils
from ..utils import errors

logger = logging.getLogger(__name__)

class DatabaseEmote:
	__slots__ = frozenset((
		'name',
		'id',
		'author',
		'animated',
		'description',
		'created',
		'modified',
		'preserve',
		'guild',
		'nsfw',
		'usage'))

	def __init__(self, record):
		for column in self.__slots__:
			with contextlib.suppress(KeyError):
				setattr(self, column, record[column])

	def __hash__(self):
		return self.id >> 22

	def __str__(self):
		animated = 'a' if self.animated else ''
		return '<{0}:{1.name}:{1.id}>'.format(animated, self)

	def as_reaction(self):
		"""return this emote as a string suitable for passing to Message.add_reaction"""
		# apparently "a:" is not necessary for animated emote reactions
		return f':{self.name}:{self.id}'

	def escaped_name(self):
		"""return the emote's name in colons, suitable for displaying how to use the emote
		or when the emote no longer exists."""
		# \ in case they name an emote, e.g. :grinning:
		# we want to display :grinning:, not 😁
		return fr'\:{self.name}:'

	def with_name(self):
		"""return this emote as a string suitable for displaying in a list form or embed"""
		return f'{self} {self.escaped_name()}'

	def with_linked_name(self, *, separator='|'):
		"""return this emote as a string suitable for displaying in a list form or embed"""
		return f'{self} {separator} [{self.escaped_name()}]({self.url})'

	@property
	def url(self):
		return utils.emote.url(self.id, animated=self.animated)

	@property
	def is_nsfw(self):
		return self.nsfw.endswith('NSFW')

class Database:
	def __init__(self, bot):
		self.bot = bot
		self._process_decay_config()

		self.tasks = []
		# without backend guild enumeration, the bot will report all guilds being full
		self.tasks.append(self.bot.loop.create_task(self.find_backend_guilds()))
		self.tasks.append(self.bot.loop.create_task(self.update_emote_guilds()))
		self.tasks.append(self.bot.loop.create_task(self.decay_loop()))
		self.logger = self.bot.get_cog('Logger')

	def _process_decay_config(self):
		# example: {'enabled': True, 'cutoff': {'time': datetime.timedelta(...), 'usage': 3}}
		decay_settings = self.bot.config.get('decay', False)
		if isinstance(decay_settings, bool):
			# old schema: just a bool to indicate enabled
			self.bot.config['decay'] = decay_settings = {'enabled': decay_settings}

		decay_settings.setdefault('enabled', False)

		cutoff_settings = decay_settings.setdefault('cutoff', {})
		cutoff_settings.setdefault('time', datetime.timedelta(weeks=4))
		cutoff_settings.setdefault('usage', 2)

	def __unload(self):
		for task in self.tasks:
			task.cancel()

	async def find_backend_guilds(self):
		"""Find all the guilds used to store emotes"""

		if getattr(self, 'guilds', None):
			return

		await self.bot.wait_until_ready()

		guilds = {
			guild
			for guild in self.bot.guilds
			if guild.name.startswith(('EmojiBackend', 'EmoteBackend')) and await self.bot.is_owner(guild.owner)}

		await self.bot.pool.executemany("""
			INSERT INTO _guilds
			VALUES ($1)
			ON CONFLICT (id) DO NOTHING
		""", map(lambda x: (x.id,), guilds))

		self.guilds = frozenset(guilds)
		logger.info('In %s backend guilds.', len(self.guilds))

		# allow other cogs that depend on the list of backend guilds to know when they've been found
		self.bot.dispatch('backend_guild_enumeration', self.guilds)

	async def update_emote_guilds(self):
		"""update the guild column in the emotes table

		it's null in a former installation without the guild column
		"""

		emotes = []
		async for db_emote in self.all_emotes():
			discord_emote = self.bot.get_emoji(db_emote.id)
			if discord_emote is None:
				continue
			emotes.append((db_emote.id, discord_emote.guild_id))

		await self.bot.pool.executemany('UPDATE emotes SET guild = $2 WHERE id = $1', emotes)

	async def decay_loop(self):
		poll_interval = 60 * 10
		while True:
			if not self.bot.config['decay']['enabled']:
				# allow the user to enable the decay for next loop
				await asyncio.sleep(poll_interval)
				continue

			await self.bot.wait_until_ready()
			await self.bot.db_ready.wait()

			await self.decay()

			await asyncio.sleep(poll_interval)

	@commands.command(name='sql', aliases=['SQL'], hidden=True)
	@commands.is_owner()
	async def sql_command(self, context, *, query):
		"""Gets the rows of a SQL query. Prepared statements are not supported."""
		start = time.perf_counter()
		# XXX properly strip codeblocks
		try:
			results = await self.bot.pool.fetch(query.strip('`'))
		except asyncpg.PostgresError as exception:
			return await context.send(exception)
		elapsed = time.perf_counter() - start

		message = await utils.codeblock(str(utils.PrettyTable(results)))
		return await context.send(f'{message}*{len(results)} rows retrieved in {elapsed:.2f} seconds.*')

	async def free_guild(self, animated=False):
		"""Find a guild in the backend guilds suitable for storing an emote.

		As the number of emotes stored by the bot increases, the probability of finding a rate-limited
		guild approaches 1, but until then, this should work pretty well.
		"""

		# random() hopefully lets us bypass emote rate limits
		# otherwise if we always pick the first available gulid,
		# we might reuse it often and get rate limited.
		guild_id = await self.bot.pool.fetchval(f"""
			SELECT id
			FROM guilds
			WHERE {'animated' if animated else 'static'}_usage < 50
			ORDER BY random()
			LIMIT 1
		""")

		if guild_id is None:
			raise errors.NoMoreSlotsError

		return guild_id

	## Informational

	def count(self) -> asyncpg.Record:
		"""Return (not animated count, animated count, total)"""
		return self.bot.pool.fetchrow("""
			SELECT
				COUNT(*) FILTER (WHERE NOT animated) AS static,
				COUNT(*) FILTER (WHERE animated) AS animated,
				COUNT(*) AS total
			FROM emotes
		""")

	def capacity(self):
		"""return a three-tuple of static capacity, animated, total"""
		return (len(self.guilds) * 50,) * 2 + (len(self.guilds) * 50 * 2,)

	async def get_emote(self, name) -> DatabaseEmote:
		"""get an emote object by name"""
		# we use LOWER(name) = LOWER($1) instead of ILIKE because ILIKE has some wildcarding stuff
		# that we don't want
		# probably LOWER(name) = $1, name.lower() would also work, but this looks cleaner
		# and keeps the lowercasing behavior consistent
		result = await self.bot.pool.fetchrow('SELECT * FROM emotes WHERE LOWER(name) = LOWER($1)', name)
		if result:
			return DatabaseEmote(result)
		else:
			raise errors.EmoteNotFoundError(name)

	def get_emote_usage(self, emote) -> int:
		"""return how many times this emote was used"""
		cutoff_time = datetime.datetime.utcnow() - self.bot.config['decay']['cutoff']['time']
		return self.bot.pool.fetchval("""
			SELECT COUNT(*)
			FROM emote_usage_history
			WHERE id = $1
			  AND time > $2
		""", emote.id, cutoff_time)

	## Iterators

	def all_emotes(self, author_id=None, *, filter_nsfw_for: discord.abc.Messageable = None):
		"""return an async iterator that gets emotes from the database.
		If author id is provided, get only emotes from them."""
		query = 'SELECT * FROM emotes '
		args = []
		if author_id is not None:
			query += 'WHERE author = $1 '
			args.append(author_id)
		query += 'ORDER BY LOWER(name)'

		return self._database_emote_cursor(query, *args, filter_nsfw_for)

	def popular_emotes(self, *, limit=200, filter_nsfw_for: discord.abc.Messageable = None):
		"""return an async iterator that gets emotes from the db sorted by popularity"""
		cutoff_time = datetime.datetime.utcnow() - self.bot.config['decay']['cutoff']['time']

		return self._database_emote_cursor("""
			SELECT e.*, COUNT(euh.id) AS usage
			FROM emotes AS e
			LEFT JOIN emote_usage_history AS euh
			    ON euh.id = e.id
			   AND euh.time > $1
			GROUP BY e.id
			ORDER BY usage DESC, LOWER(e.name)
			LIMIT $2
		""", cutoff_time, limit, filter_nsfw_for)

	def search(self, query, *, filter_nsfw_for: discord.abc.Messageable = None):
		"""return an async iterator that gets emotes from the db whose name is similar to `query`."""

		return self._database_emote_cursor("""
			SELECT *
			FROM emotes
			WHERE name % $1
			ORDER BY similarity(name, $1) DESC, LOWER(name)
			LIMIT 100
		""", query, filter_nsfw_for)

	def decayable_emotes(self):
		"""emotes that should be removed due to inactivity.

		returns an async iterator over all emotes that:
			- were created before `cutoff`, and
			- have been used < `usage_threshold` between now and cutoff, and
			- are not preserved

		the cut off and usage threshold are specified in a dict at self.bot.config['decay'],
		under subkeys 'cutoff_time' and 'cutoff_usage', respectively.
		"""
		cutoff_time = datetime.datetime.utcnow() - self.bot.config['decay']['cutoff']['time']
		usage_threshold = self.bot.config['decay']['cutoff']['usage']

		return self._database_emote_cursor("""
			SELECT e.*, COUNT(euh.id) AS usage
			FROM emotes AS e
			LEFT JOIN emote_usage_history AS euh
			    ON euh.id = e.id
			   AND time > $1
			WHERE created < $1
			      AND NOT preserve
			GROUP BY e.id
			HAVING COUNT(euh.id) < $2
		""", cutoff_time, usage_threshold)

	async def _database_emote_cursor(self, query, *args):
		"""like _cursor, but wraps results in DatabaseEmote objects"""

		channel = None
		if isinstance(args[-1], discord.abc.Messageable):
			# we want to filter nsfw emotes in this channel
			channel = args[-1]
			args = args[:-1]

		async for row in self._cursor(query, *args):
			emote = DatabaseEmote(row)
			if not channel or not emote.is_nsfw or getattr(channel, 'nsfw', True):
				yield emote

	async def _cursor(self, query, *args):
		"""return an Async Generator over all records selected by the query and its args"""

		async with self.bot.pool.acquire() as connection:
			async with connection.transaction():
				async for row in connection.cursor(query, *args):
					# we can't just return connection.cursor(...)
					# because the connection would be closed by the time we returned
					# so we have to become a generator to keep the conn open
					yield row

	## Checks

	async def ensure_emote_does_not_exist(self, name):
		"""fail with an exception if an emote called `name` does not exist
		this is to reduce duplicated exception raising code."""

		try:
			emote = await self.get_emote(name)
		except errors.EmoteNotFoundError:
			pass
		else:
			raise errors.EmoteExistsError(emote)

	async def is_owner(self, emote, user_id):
		"""return whether the user has permissions to modify this emote"""

		if user_id is None:
			return True

		if not emote:  # you can't own an emote that doesn't exist
			raise errors.EmoteNotFoundError(emote.name)
		user = discord.Object(user_id)
		return await self.bot.is_owner(user) or emote.author == user.id

	async def owner_check(self, emote, user_id):
		"""like is_owner but fails with an exception if the user is not authorized.
		this is to reduce duplicated exception raising code."""
		if not await self.is_owner(emote, user_id):
			raise errors.PermissionDeniedError(emote.name)

	## Actions

	async def create_emote(self, name, author_id, animated, image_data: bytes):
		await self.ensure_emote_does_not_exist(name)

		guild_id = await self.free_guild(animated)

		image = discord.utils._bytes_to_base64_data(image_data)
		emote_data = await self.bot.http.create_custom_emoji(guild_id=guild_id, name=name, image=image)
		return DatabaseEmote(await self.bot.pool.fetchrow("""
			INSERT INTO emotes (name, id, author, animated, guild)
			VALUES ($1, $2, $3, $4, $5)
			RETURNING *
		""", name, int(emote_data['id']), author_id, animated, guild_id))

	async def remove_emote(self, emote, user_id):
		"""Remove an emote given by name or DatabaseEmote object.
		- user_id: the user trying to remove this emote,
		  or None if their ownership should not
		  be verified

		returns the emote that was deleted
		"""
		if isinstance(emote, str):
			emote = await self.get_emote(name=emote)

		await self.owner_check(emote, user_id)

		try:
			await self.bot.http.delete_custom_emoji(emote.guild, emote.id)
		except discord.NotFound:
			# sometimes the database and the backend get out of sync
			# but we don't really care if there's an entry in the database and not the backend
			logger.warn(f'emote {emote.name} found in the database but not the backend! removing anyway.')

		await self.bot.pool.execute('DELETE FROM emotes WHERE id = $1', emote.id)
		return emote

	async def rename_emote(self, old_name, new_name, user_id):
		"""rename an emote from old_name to new_name. user_id must be authorized."""

		# don't fail if new_name is a different capitalization of old_name
		if old_name.lower() != new_name.lower():
			await self.ensure_emote_does_not_exist(new_name)

		emote = await self.get_emote(old_name)
		await self.owner_check(emote, user_id)

		await self.bot.http.edit_custom_emoji(emote.guild, emote.id, name=new_name)
		return DatabaseEmote(await self.bot.pool.fetchrow("""
			UPDATE emotes
			SET name = $2
			WHERE id = $1
			RETURNING *
		""", emote.id, new_name))

	async def set_emote_creation(self, name, time: datetime):
		"""Set the creation time of an emote."""
		await self.bot.pool.execute("""
			UPDATE emotes
			SET created = $2
			WHERE LOWER(name) = LOWER($1)
		""", name, time)

	async def set_emote_description(self, name, user_id=None, description=None):
		"""Set an emote's description.

		If you leave out the description, it will be removed.
		You could use this to:
		- Detail where you got the image
		- Credit another author
		- Write about why you like the emote
		- Describe how it's used
		"""
		emote = await self.get_emote(name)
		await self.owner_check(emote, user_id)

		try:
			return DatabaseEmote(await self.bot.pool.fetchrow("""
				UPDATE emotes
				SET DESCRIPTION = $2
				WHERE id = $1
				RETURNING *
			""",emote.id, description))
		except asyncpg.StringDataRightTruncationError as exception:
			# XXX dumb way to do it but it's the only way i've got
			limit = int(re.search(r'character varying\((\d+)\)', exception.message)[1])
			raise errors.EmoteDescriptionTooLongError(emote.name, len(description), limit)

	async def set_emote_preservation(self, name, should_preserve: bool):
		"""change the preservation status of an emote.
		if an emote is preserved, it should not be decayed due to lack of use
		"""
		emote = await self.bot.pool.fetchrow("""
			UPDATE emotes
			SET preserve = $1
			WHERE LOWER(name) = LOWER($2)
			RETURNING *
		""", should_preserve, name)

		# why are we doing this "if not emote" checking, when we could just call get_emote
		# before update?
		# because that would constitute an extra database query which we don't need
		if not emote:
			raise errors.EmoteNotFoundError(name)
		else:
			return DatabaseEmote(emote)

	async def toggle_emote_nsfw(self, emote: DatabaseEmote, by_mod=False):
		"""Toggles the NSFW status of an emote."""
		# i probably could do this in one giant query, but i'm lazy
		new_status = self.new_nsfw_status(emote, by_mod)

		return DatabaseEmote(await self.bot.pool.fetchrow("""
			UPDATE emotes
			SET nsfw = $2
			WHERE id = $1
			RETURNING *
		""", emote.id, new_status))

	@staticmethod
	def new_nsfw_status(emote, by_mod):
		desired_status = not emote.is_nsfw

		if by_mod:
			# mods can do anything
			return 'MOD_NSFW' if desired_status else 'SFW'
		elif desired_status:
			return 'SELF_NSFW'

		# not by mod and SFW
		if emote.nsfw == 'MOD_NSFW':
			raise commands.BadArgument(
				_('You may not set this emote as SFW because it was set NSFW by an emote moderator.'))
		if emote.nsfw == 'SELF_NSFW':
			return 'SFW'

	async def delete_user_account(self, user_id):
		await self.delete_all_user_emotes(user_id)
		await self.delete_all_user_state(user_id)

	async def delete_all_user_emotes(self, user_id):
		async for emote in self.all_emotes(user_id):
			with contextlib.suppress(errors.EmoteError):
				# since we're only listing emotes by user_id,
				# we don't need to perform another ownership check
				await self.remove_emote(emote, user_id=None)

	async def log_emote_use(self, emote_id, user_id):
		await self.bot.pool.execute("""
			INSERT INTO emote_usage_history (id)
			-- this is SELECT ... WHERE NOT EXISTS, not INSERT INTO ... WHERE NOT EXISTS
			-- https://stackoverflow.com/a/15710598
			SELECT ($1)
			WHERE NOT EXISTS (
				-- restrict emote logging to non-owners
				-- this should reduce some spam and stats-inflation
				SELECT 1
				FROM emotes
				WHERE id = $1
				  AND author = $2)
		""", emote_id, user_id)

	async def decay(self):
		async for emote in self.decayable_emotes():
			logger.debug('decaying %s', emote.name)
			removal_message = await self.logger.on_emote_decay(emote)
			try:
				await self.remove_emote(emote, user_id=None)
			except (errors.ConnoisseurError, errors.DiscordError) as ex:
				logger.error('decaying %s failed due to %s', emote.name, ex)
				with contextlib.suppress(AttributeError):
					await removal_message.delete()

	## User / Guild Options

	async def delete_all_user_state(self, user_id):
		await self.bot.pool.execute('DELETE FROM user_opt WHERE id = $1', user_id)

	async def toggle_user_state(self, user_id, guild_id=None) -> bool:
		"""Toggle whether the user has opted to use the emote auto response.
		If the user does not have an entry already:
			If the guild_id is provided and not None, the user's state is set to the opposite of the guilds'
			Otherwise, the user's state is set to False, since the default state is True.
		Returns the new state."""
		default = False
		guild_state = await self.get_guild_state(guild_id)
		if guild_state is not None:
			# if the auto response is enabled for the guild then toggling the user state should opt out
			default = not guild_state
		return await self._toggle_state('user_opt', user_id, default)

	def _toggle_state(self, table_name, id, default):
		"""toggle the state for a user or guild. If there's no entry already, new state = default."""
		# see _get_state for why string formatting is OK here
		return self.bot.pool.fetchval(f"""
			INSERT INTO {table_name} (id, state)
			VALUES ($1, $2)
			ON CONFLICT (id) DO UPDATE
				SET state = NOT {table_name}.state
			RETURNING state
		""", id, default)

	def toggle_guild_state(self, guild_id):
		"""Togle whether this guild is opt out.
		If this guild is opt in, the emote auto response will be disabled
		except for users that have opted in to it using `toggle_user_state`.
		Otherwise, the response will be on for all users except those that have opted out.
		"""
		return self._toggle_state('guild_opt', guild_id, False)

	def _get_state(self, table_name, id):
		# unfortunately, using $1 for table_name is a syntax error
		# however, since table name is always hardcoded input from other functions in this module,
		# it's ok to use string formatting here
		return self.bot.pool.fetchval(f'SELECT state FROM {table_name} WHERE id = $1', id)

	def get_user_state(self, user_id):
		"""return this user's global preference for the emote auto response"""
		return self._get_state('user_opt', user_id)

	def get_guild_state(self, guild_id):
		"""return whether this guild is opt in"""
		return self._get_state('guild_opt', guild_id)

	def get_state(self, guild_id, user_id):
		# TODO investigate whether this obviates get_guild_state and get_user_state (probably does)
		return self.bot.pool.fetchval("""
			SELECT COALESCE(
				CASE WHEN (SELECT blacklist_reason FROM user_opt WHERE id = $2)
					IS NULL THEN NULL
					ELSE FALSE
				END,
				(SELECT state FROM user_opt  WHERE id = $2),
				(SELECT state FROM guild_opt WHERE id = $1),
				true
			)
		""", guild_id, user_id)

	## Blacklists

	def get_user_blacklist(self, user_id):
		"""return a reason for the user's blacklist, or None if not blacklisted"""
		return self.bot.pool.fetchval("""
			SELECT blacklist_reason
			FROM user_opt
			WHERE id = $1
		""", user_id)

	async def set_user_blacklist(self, user_id, reason=None):
		"""make user_id blacklisted
		setting reason to None removes the user's blacklist"""
		# insert regardless of whether it exists
		# and if it does exist, update
		await self.bot.pool.execute("""
			INSERT INTO user_opt (id, blacklist_reason)
			VALUES ($1, $2)
			ON CONFLICT (id) DO UPDATE
				SET blacklist_reason = EXCLUDED.blacklist_reason
		""", user_id, reason)

def setup(bot):
	bot.add_cog(Database(bot))

#!/usr/bin/env python3.6
# encoding: utf-8

import asyncio
from datetime import datetime
import logging
import random
import time

import aiofiles
import asyncpg
import discord
from discord.ext import commands

from utils import PrettyTable, errors


logger = logging.getLogger('cogs.db')


class DatabaseEmote(dict):
	def __init__(self, x, **kwargs):
		if x is not None or kwargs:
			super().__init__(x, **kwargs)

	def __getattr__(self, name):
		return self[name]

	def __setattr__(self, name, value):
		self[name] = value

	def __delattr__(self, name):
		del self[name]

	def __str__(self):
		animated = 'a' if self.animated else ''
		return '<{0}:{1.name}:{1.id}>'.format(animated, self)

	@classmethod
	async def convert(cls, context, name: str):
		name = name.strip().strip(':;')
		cog = context.bot.get_cog('Database')
		emote = await cog.get_emote(name)

		if not emote:
			raise errors.EmoteNotFoundError(name)
		else:
			return emote


class Database:
	def __init__(self, bot):
		self.bot = bot
		self.tasks = []
		self.tasks.append(self.bot.loop.create_task(self._get_db()))
		# without backend guild enumeration, the bot will report all guilds being full
		self.tasks.append(self.bot.loop.create_task(self.find_backend_guilds()))
		self.tasks.append(self.bot.loop.create_task(self.decay_loop()))
		self.utils_cog = self.bot.get_cog('Utils')

	def __unload(self):
		for task in self.tasks:
			task.cancel()

		try:
			self.bot.loop.create_task(self.db.close())
		except AttributeError:
			pass  # db has not been set yet

	async def decay_loop(self):
		while True:
			if not self.bot.config.get('decay', False):
				return

			await self.bot.wait_until_ready()

			cutoff = datetime.datetime.utcnow() - datetime.timedelta(weeks=4)
			await self.decay(cutoff, 10)

			await asyncio.sleep(60*10)

	@commands.command(name='sql', hidden=True)
	@commands.is_owner()
	async def sql_command(self, context, *, query):
		"""Gets the rows of a SQL query. Prepared statements are not supported."""
		start = time.monotonic()
		# XXX properly strip codeblocks
		results = await self.db.fetch(query.replace('`', ''))
		elapsed = time.monotonic() - start

		message = await self.utils_cog.codeblock(str(PrettyTable(results)))
		return await context.send(f'{message}*{len(results)} rows retrieved in {elapsed:.2f} seconds.*')

	@staticmethod
	def emote_url(emote_id, animated: bool = False):
		"""Convert an emote ID to the image URL for that emote."""
		return f'https://cdn.discordapp.com/emojis/{emote_id}{".gif" if animated else ".png"}?v=1'

	async def find_backend_guilds(self):
		"""Find all the guilds used to store emotes"""

		if hasattr(self, 'guilds') and self.guilds:  # pylint: disable=access-member-before-definition
			return

		await self.bot.wait_until_ready()

		guilds = []
		for guild in self.bot.guilds:
			if guild.name.startswith('EmojiBackend') and await self.bot.is_owner(guild.owner):
				guilds.append(guild)
		self.guilds = guilds
		logger.info('In %s backend guilds.', len(guilds))

		# allow other cogs that depend on the list of backend guilds to know when they've been found
		self.bot.dispatch('backend_guild_enumeration', self.guilds)

	def free_guild(self, animated=False):
		"""Find a guild in the backend guilds suitable for storing an emote.

		As the number of emotes stored by the bot increases, the probability of finding a rate-limited
		guild approaches 1, but until then, this should work pretty well.
		"""
		free_guilds = []
		for guild in self.guilds:
			if sum(animated == emote.animated for emote in guild.emojis) < 50:
				free_guilds.append(guild)

		if not free_guilds:
			raise errors.NoMoreSlotsError

		# hopefully this lets us bypass the rate limit more often, since emote rates are per-guild
		return random.choice(free_guilds)

	## Informational

	async def count(self) -> asyncpg.Record:
		"""Return (not animated count, animated count, total)"""
		return await self.db.fetchrow("""
			SELECT
				COUNT(*) FILTER (WHERE NOT animated) AS static,
				COUNT(*) FILTER (WHERE animated) AS animated,
				COUNT(*) AS total
			FROM emojis;""")

	async def get_emote(self, name) -> DatabaseEmote:
		"""get an emote object by name"""
		# we use LOWER(name) = LOWER($1) instead of ILIKE because ILIKE has some wildcarding stuff
		# that we don't want
		# probably LOWER(name) = $1, name.lower() would also work, but this looks cleaner
		# and keeps the lowercasing behavior consistent
		return DatabaseEmote(
			await self.db.fetchrow('SELECT * FROM emojis WHERE LOWER(name) = LOWER($1)', name))

	async def get_emote_usage(self, emote: asyncpg.Record) -> int:
		"""return how many times this emote was used"""
		return await self.db.fetchval(
			'SELECT COUNT(*) FROM emote_usage_history WHERE id = $1',
			emote['id'])

	## Iterators

	def all_emotes(self, author_id=None):
		"""return an async iterator that gets emotes from the database.
		If author id is provided, get only emotes from them."""
		query = 'SELECT * FROM emojis '
		args = []
		if author_id is not None:
			query += 'WHERE author = $1 '
			args.append(author_id)
		query += 'ORDER BY LOWER(name)'

		return self._database_emote_cursor(query)

	def popular_emotes(self):
		"""return an async iterator that gets emotes from the db sorted by popularity"""
		query = """
			SELECT *, (
				SELECT COUNT(*)
				FROM emote_usage_history
				WHERE id = emojis.id
			) AS usage
			FROM emojis
			ORDER BY usage DESC, LOWER("name")
		"""
		return self._database_emote_cursor(query)

	async def decayable_emotes(self, cutoff: datetime, usage_threshold):
		"""remove emotes that should be removed due to inactivity.

		returns an async iterator over all emotes that:
			- were created before `cutoff`, and
			- have been used < `usage_threshold` between now and cutoff, and
			- are not preserved
		"""

		return self._database_emote_cursor("""
			SELECT *
			FROM emojis
			WHERE (
				SELECT COUNT(*)
				FROM emote_usage_history
				WHERE
					id = emojis.id
					AND time > $1
			) < $2
				AND NOT preserve
				AND created < $1;
		""", cutoff, usage_threshold)

	async def _database_emote_cursor(self, query, *args):
		"""like _cursor, but wraps results in DatabaseEmote objects"""

		async for row in self._cursor(query, *args):
			yield DatabaseEmote(row)

	async def _cursor(self, query, *args):
		"""return an Async Generator over all records selected by the query and its args"""

		async with self.db.acquire() as connection:
			async with connection.transaction():
				async for row in connection.cursor(query, *args):
					# we can't just return connection.cursor(...)
					# because the connection would be closed by the time we returned
					# so we have to become a generator to keep the conn open
					yield row

	## Checks

	async def ensure_emote_exists(self, name):
		"""fail with an exception if an emote called `name` does not exist
		this is to reduce duplicated exception raising code."""
		if not await self.get_emote(name):
			raise errors.EmoteNotFoundError(name)

	async def ensure_emote_does_not_exist(self, name):
		"""fail with an exception if an emote called `name` does not exist
		this is to reduce duplicated exception raising code."""
		emote = await self.get_emote(name)
		if emote:
			# use the original capitalization of the name
			raise errors.EmoteExistsError(emote['name'])

	async def create_emote(self, name, author_id, animated, image_data: bytes):
		blacklist_reason = await self.get_user_blacklist(author_id)
		if blacklist_reason:
			raise errors.UserBlacklisted(blacklist_reason)
		await self.ensure_emote_does_not_exist(name)

		# checks passed
		guild = self.free_guild(animated)

		emote = await guild.create_custom_emoji(name=name, image=image_data)
		await self.db.execute(
			'INSERT INTO emojis(name, id, author, animated) VALUES ($1, $2, $3, $4)',
			name, emote.id, author_id, animated)

		return await self.get_emote(name)

	async def is_owner(self, name, user_id):
		"""return whether the user has permissions to modify this emote"""
		emote = await self.get_emote(name)
		if not emote:  # you can't own an emote that doesn't exist
			raise errors.EmoteNotFoundError(name)
		user = discord.Object(user_id)
		return await self.bot.is_owner(user) or emote['author'] == user.id

	async def owner_check(self, name, user_id):
		"""like is_owner but fails with an exception if the user is not authorized.
		this is to reduce duplicated exception raising code."""
		if not await self.is_owner(name, user_id):
			raise errors.PermissionDeniedError(name)

	## Actions

	async def decay(self):
		async for emote in self.decayable_emotes():
			logger.info('decaying %s', emote['name'])
			await self.remove_emote(emote['name'], user_id=None)

	async def remove_emote(self, name, user_id):
		"""Remove an emote given by name.
		- user_id: the user trying to remove this emote,
		  or None if their ownership should not
		  be verified
		"""
		if user_id is not None:
			await self.owner_check(name, user_id)

		db_emote = await self.get_emote(name)
		if not db_emote:
			raise errors.EmoteNotFoundError

		emote = self.bot.get_emoji(db_emote['id'])
		if emote is None:
			raise errors.DiscordError

		await emote.delete()
		await self.db.execute('DELETE FROM emote_usage_history WHERE id = $1', db_emote['id'])
		await self.db.execute('DELETE FROM emojis WHERE id = $1', db_emote['id'])

	async def rename_emote(self, old_name, new_name, user_id):
		"""rename an emote from old_name to new_name. user_id must be authorized."""
		await self.owner_check(old_name, user_id)
		# don't fail if new_name is a different capitalization of old_name
		if old_name.lower() != new_name.lower() and await self.get_emote(new_name):
			raise errors.EmoteExistsError(new_name)
		db_emote = await self.get_emote(old_name)
		emote = self.bot.get_emoji(db_emote['id'])
		await emote.edit(name=new_name)
		await self.db.execute('UPDATE emojis SET name = $2 where id = $1', emote.id, new_name)

	async def set_emote_description(self, name, user_id, description=None):
		"""Set an emote's description.

		If you leave out the description, it will be removed.
		You could use this to:
		- Detail where you got the image
		- Credit another author
		- Write about why you like the emote
		- Describe how it's used
		"""
		await self.owner_check(name, user_id)
		try:
			await self.db.execute(
				'UPDATE emojis SET DESCRIPTION = $2 WHERE LOWER(name) = LOWER($1)',
				name,
				description)
		# wowee that's a verbose exception name
		# like why not just call it "StringTooLongError"?
		except asyncpg.StringDataRightTruncationError as exception:
			raise errors.EmoteDescriptionTooLong from exception

	async def set_emote_preservation(self, name, should_preserve: bool):
		"""change the preservation status of an emote.
		if an emote is preserved, it should not be decayed due to lack of use
		"""
		await self.ensure_emote_exists(name)
		await self.db.execute(
			'UPDATE emojis SET preserve = $1 WHERE LOWER(name) = LOWER($2)',
			should_preserve, name)

	async def get_emote_preservation(self, name):
		"""return whether the emote should be prevented from being decayed"""
		result = await self.db.fetchval('SELECT preserve FROM emojis WHERE LOWER(name) = LOWER($1)', name)
		if result is None:
			raise errors.EmoteNotFoundError(name)
		return result

	async def log_emote_use(self, emote_id):
		await self.db.execute(
			'INSERT INTO emote_usage_history (id) VALUES ($1)',
			emote_id)

	## User / Guild Options

	async def _toggle_state(self, table_name, id, default):
		"""toggle the state for a user or guild. If there's no entry already, new state = default."""
		# see _get_state for why string formatting is OK here
		await self.db.execute(f"""
			INSERT INTO {table_name} (id, state) VALUES ($1, $2)
			ON CONFLICT (id) DO UPDATE SET state = NOT {table_name}.state
		""", id, default)

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
		await self._toggle_state('user_opt', user_id, default)
		return await self.get_user_state(user_id)

	async def toggle_guild_state(self, guild_id):
		"""Togle whether this guild is opt out.
		If this guild is opt in, the emote auto response will be disabled
		except for users that have opted in to it using `toggle_user_state`.
		Otherwise, the response will be on for all users except those that have opted out."""
		await self._toggle_state('guild_opt', guild_id, False)
		return await self.get_guild_state(guild_id)

	async def _get_state(self, table_name, id):
		# unfortunately, using $1 for table_name is a syntax error
		# however, since table name is always hardcoded input from other functions in this module,
		# it's ok to use string formatting here
		return await self.db.fetchval(f'SELECT state FROM {table_name} WHERE id = $1', id)

	async def get_user_state(self, user_id):
		"""return this user's global preference for the emoji auto response"""
		return await self._get_state('user_opt', user_id)

	async def get_guild_state(self, guild_id):
		"""return whether this guild is opt in"""
		return await self._get_state('guild_opt', guild_id)

	async def get_state(self, guild_id, user_id):
		state = True

		guild_state = await self.get_guild_state(guild_id)
		if guild_state is not None:
			state = guild_state

		user_state = await self.get_user_state(user_id)
		if user_state is not None:
			state = user_state  # user state overrides guild state

		return state

	## Blacklists

	async def get_user_blacklist(self, user_id):
		"""return a reason for the user's blacklist, or None if not blacklisted"""
		return await self.db.fetchval('SELECT blacklist_reason from user_opt WHERE id = $1', user_id)

	async def set_user_blacklist(self, user_id, reason=None):
		"""make user_id blacklisted
		setting reason to None removes the user's blacklist"""
		# insert regardless of whether it exists
		# and if it does exist, update
		await self.db.execute("""
			INSERT INTO user_opt (id, blacklist_reason) VALUES ($1, $2)
			ON CONFLICT (id) DO UPDATE SET blacklist_reason = EXCLUDED.blacklist_reason""", user_id, reason)

	##

	async def _get_db(self):
		credentials = self.bot.config['database']
		try:
			db = await asyncpg.create_pool(**credentials)  # pylint: disable=invalid-name
		except ConnectionRefusedError:
			logger.error('Failed to connect to the database!')
			await self.bot.logout()
			return

		async with aiofiles.open('data/schema.sql') as f:
			await db.execute(await f.read())

		self.db = db  # pylint: disable=invalid-name


def setup(bot):
	bot.add_cog(Database(bot))

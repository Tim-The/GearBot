import asyncio
import collections
import datetime
import time

import discord
from discord.embeds import EmptyEmbed
from discord.ext import commands
from discord.raw_models import RawMessageDeleteEvent, RawMessageUpdateEvent
from peewee import IntegrityError

from Util import GearbotLogging, Configuration, Utils, Archive, Emoji, Translator
from database.DatabaseConnector import LoggedMessage, LoggedAttachment


class ModLog:

    def __init__(self, bot):
        self.bot:commands.Bot = bot
        self.bot.loop.create_task(self.prep())
        self.bot.loop.create_task(cache_task(self))
        self.running = True

    def __unload(self):
        self.running = False


    async def buildCache(self, guild:discord.Guild, limit = 250):
        start = time.perf_counter()
        GearbotLogging.info(f"Populating modlog with missed messages during downtime for {guild.name} ({guild.id}).")
        newCount = 0
        editCount = 0
        count = 0
        for channel in guild.text_channels:
            if channel.permissions_for(guild.get_member(self.bot.user.id)).read_messages:
                logged_messages = LoggedMessage.select().where(LoggedMessage.channel == channel.id).order_by(
                    LoggedMessage.messageid.desc()).limit(limit*1.5)
                messages = dict()
                for message in logged_messages:
                    messages[message.messageid] = message
                async for message in channel.history(limit=limit, reverse=False):
                    if not self.running:
                        GearbotLogging.info("Cog unloaded while still building cache, aborting.")
                        return
                    if message.author == self.bot.user:
                        continue
                    if message.id not in messages.keys():
                        try:
                            LoggedMessage.create(messageid=message.id, author=message.author.id,
                                                                  content=message.content, timestamp = message.created_at.timestamp(), channel=channel.id, server=channel.guild.id)
                            for a in message.attachments:
                                LoggedAttachment.create(id=a.id, url=a.url, isImage=(a.width is not None or a.width is 0), messageid=message.id)
                            newCount = newCount + 1
                        except IntegrityError:
                            # somehow we didn't fetch enough messages, did someone set off a nuke in the channel?
                            logged = LoggedMessage.get(messageid=message.id)
                            if logged.content != message.content:
                                logged.content = message.content
                                logged.save()
                                editCount = editCount + 1
                    else:
                        logged = messages[message.id]
                        if logged.content != message.content:
                            logged.content = message.content
                            logged.save()
                            editCount = editCount + 1
                    count = count + 1
        GearbotLogging.info(f"Discovered {newCount} new messages and {editCount} edited in {guild.name} (checked {count}) in {time.perf_counter() - start }s.")

    async def prep(self):
        for guild in self.bot.guilds:
            if Configuration.getConfigVar(guild.id, "MINOR_LOGS") is not 0:
                await self.buildCache(guild)

    async def on_message(self, message: discord.Message):
        if not hasattr(message.channel, "guild") or message.channel.guild is None:
            return
        if Configuration.getConfigVar(message.guild.id, "MINOR_LOGS") is 0 or message.author == self.bot.user:
            return
        for a in message.attachments:
            LoggedAttachment.create(id=a.id, url=a.url, isImage=(a.width is not None or a.width is 0), messageid=message.id)
        LoggedMessage.create(messageid=message.id, author=message.author.id, content=message.content, timestamp=message.created_at.timestamp(), channel=message.channel.id, server=message.guild.id)


    async def on_raw_message_delete(self, data:RawMessageDeleteEvent):
        message = LoggedMessage.get_or_none(messageid=data.message_id)
        if message is not None:
            channel: discord.TextChannel = self.bot.get_channel(data.channel_id)
            user: discord.User = self.bot.get_user(message.author)
            hasUser = user is not None
            if hasUser and user.id in Configuration.getConfigVar(channel.guild.id, "IGNORED_USERS"):
                return
            channelid = Configuration.getConfigVar(channel.guild.id, "MINOR_LOGS")
            if channelid is not 0:
                logChannel:discord.TextChannel = self.bot.get_channel(channelid)
                if logChannel is not None and message.content != None and message.content != "":
                    embed = discord.Embed(timestamp=datetime.datetime.utcfromtimestamp(time.time()),
                                          description=message.content)
                    embed.set_author(name=user.name if hasUser else message.author, icon_url=user.avatar_url if hasUser else EmptyEmbed)
                    embed.set_footer(text=f"Sent in #{channel.name}")
                    name = Utils.clean_user(user) if hasUser else str(message.author)
                    await logChannel.send(f":wastebasket: {Translator.translate('message_removed', channel.guild.id, name=name, user_id=user.id if hasUser else 'WEBHOOK', channel=channel.mention)}", embed=embed)

    async def on_raw_message_edit(self, event:RawMessageUpdateEvent):
        if event.data["channel_id"] == Configuration.getMasterConfigVar("BOT_LOG_CHANNEL"):
            return
        message = LoggedMessage.get_or_none(messageid=event.message_id)
        if message is not None and "content" in event.data:
            channel: discord.TextChannel = self.bot.get_channel(int(event.data["channel_id"]))
            user: discord.User = self.bot.get_user(message.author)
            hasUser = user is not None
            channelid = Configuration.getConfigVar(channel.guild.id, "MINOR_LOGS")
            if channelid is not 0:
                logChannel: discord.TextChannel = self.bot.get_channel(channelid)
                if logChannel is not None:
                    if message.content == event.data["content"]:
                        #prob just pinned
                        return
                    if message.content is None or message.content == "":
                        message.content = f"<{Translator.translate('no_content', channel.guild.id)}>"
                    embed = discord.Embed(timestamp=datetime.datetime.utcfromtimestamp(time.time()))
                    embed.set_author(name=user.name if hasUser else message.author,
                                     icon_url=user.avatar_url if hasUser else EmptyEmbed)
                    embed.set_footer(text=Translator.translate('sent_in', channel.guild.id, channel= f"#{channel.name}"))
                    embed.add_field(name=Translator.translate('before', channel.guild.id), value=Utils.trim_message(message.content, 1024), inline=False)
                    embed.add_field(name=Translator.translate('after', channel.guild.id), value=Utils.trim_message(event.data["content"], 1024), inline=False)
                    if not (hasUser and user.id in Configuration.getConfigVar(channel.guild.id, "IGNORED_USERS")):
                        await logChannel.send(f":pencil: {Translator.translate('edit_logging', channel.guild.id, user=Utils.clean_user(user), user_id=user.id, channel=channel.mention)}",
                        embed=embed)
                    message.content = event.data["content"]
                    message.save()

    async def on_member_join(self, member:discord.Member):
        channelid = Configuration.getConfigVar(member.guild.id, "JOIN_LOGS")
        if channelid is not 0:
            logChannel:discord.TextChannel = self.bot.get_channel(channelid)
            if logChannel is not None:
                dif = (datetime.datetime.utcnow() - member.created_at)
                minutes, seconds = divmod(dif.days * 86400 + dif.seconds, 60)
                hours, minutes = divmod(minutes, 60)
                age = (Translator.translate('days', member.guild.id, days=dif.days)) if dif.days > 0 else Translator.translate('hours', member.guild.id, hours=hours, minutes=minutes)
                await logChannel.send(f"{Emoji.get_chat_emoji('JOIN')} {Translator.translate('join_logging', member.guild.id, user=Utils.clean_user(member), user_id=member.id, age=age)}")

    async def on_member_remove(self, member:discord.Member):
        exits = self.bot.data["forced_exits"]
        if member.id in exits:
            exits.remove(member.id)
            return
        channelid = Configuration.getConfigVar(member.guild.id, "JOIN_LOGS")
        if channelid is not 0:
            logChannel: discord.TextChannel = self.bot.get_channel(channelid)
            if logChannel is not None:
                await logChannel.send(f"{Emoji.get_chat_emoji ('LEAVE')} {Translator.translate('leave_logging', member.guild.id, user=Utils.clean_user(member), user_id=member.id)}")

    async def on_member_ban(self, guild, user):
        if user.id in self.bot.data["forced_exits"]:
            return
        channelid = Configuration.getConfigVar(guild.id, "MOD_LOGS")
        if channelid is not 0:
            logChannel: discord.TextChannel = self.bot.get_channel(channelid)
            if logChannel is not None:
                await logChannel.send(f":rotating_light: {user.name}#{user.discriminator} (`{user.id}`) has been banned from the server.")
                self.bot.data["forced_exits"].append(user.id)


    async def on_member_unban(self, guild, user):
        if user.id in self.bot.data["unbans"]:
            return
        channelid = Configuration.getConfigVar(guild.id, "MOD_LOGS")
        if channelid is not 0:
            logChannel: discord.TextChannel = self.bot.get_channel(channelid)
            if logChannel is not None:
                await logChannel.send(
                    f":rotating_light: {user.name}#{user.discriminator} (`{user.id}`) has been unbanned from the server.")
        
    async def on_member_update(self, before, after):
        channelid = Configuration.getConfigVar(after.guild.id, "MINOR_LOGS")
        if channelid is not 0:
            logChannel: discord.TextChannel = self.bot.get_channel(channelid)
            if logChannel is not None:
                if (before.nick != after.nick and
                    after.nick != before.nick):
                    after_clean_name = Utils.clean(after.name)
                    after_clean_display_name = Utils.clean(after.display_name)
                    before_clean_display_name = Utils.clean(before.display_name)
                    await logChannel.send(
                        f'{Emoji.get_chat_emoji("NICKTAG")} {after_clean_name}#{after.discriminator} (`{after.id}`) has changed nickname from **`\u200b{before_clean_display_name}`** to **`\u200b{after_clean_display_name}`**.'
                    )
                elif (before.name != after.name and
                    after.name != before.name):
                    after_clean_name = Utils.clean(after.name)
                    before_clean_name = Utils.clean(before.name)
                    await logChannel.send(
                        f'{Emoji.get_chat_emoji("NAMETAG")} {after_clean_name}#{after.discriminator} (`{after.id}`) has changed username from **`\u200b{before_clean_name}#{after.discriminator}`** to **`\u200b{after_clean_name}#{after.discriminator}`**.'
                    )

    async def on_raw_bulk_message_delete(self, event: discord.RawBulkMessageDeleteEvent):
        channel_id = Configuration.getConfigVar(event.guild_id, "MINOR_LOGS")
        if channel_id is not 0:
            message_list = dict()
            for mid in event.message_ids:
                message = LoggedMessage.get_or_none(LoggedMessage.messageid == mid)
                if message is not None:
                    message_list[mid] = message
            await Archive.archive(self.bot, event.guild_id, collections.OrderedDict(sorted(message_list.items())))


async def cache_task(modlog:ModLog):
    GearbotLogging.info("Started modlog background task.")
    while modlog.running:
        if len(modlog.bot.to_cache) > 0:
            ctx = modlog.bot.to_cache.pop(0)
            await modlog.buildCache(ctx.guild)
            await ctx.send("Caching complete.")
        await asyncio.sleep(1)
    GearbotLogging.info("modlog background task terminated.")



def setup(bot):
    bot.add_cog(ModLog(bot))

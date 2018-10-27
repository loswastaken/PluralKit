from io import BytesIO

import discord
import logging
import re
from typing import List, Optional

from pluralkit import db
from pluralkit.bot import utils, channel_logger
from pluralkit.bot.channel_logger import ChannelLogger

logger = logging.getLogger("pluralkit.bot.proxy")


def extract_leading_mentions(message_text):
    # This regex matches one or more mentions at the start of a message, separated by any amount of spaces
    match = re.match(r"^(<(@|@!|#|@&|a?:\w+:)\d+>\s*)+", message_text)
    if not match:
        return message_text, ""

    # Return the text after the mentions, and the mentions themselves
    return message_text[match.span(0)[1]:].strip(), match.group(0)


def match_member_proxy_tags(member: db.ProxyMember, message_text: str):
    # Skip members with no defined proxy tags
    if not member.prefix and not member.suffix:
        return None

    # DB defines empty prefix/suffixes as None, replace with empty strings to prevent errors
    prefix = member.prefix or ""
    suffix = member.suffix or ""

    # Ignore mentions at the very start of the message, and match proxy tags after those
    message_text, leading_mentions = extract_leading_mentions(message_text)

    logger.debug(
        "Matching text '{}' and leading mentions '{}' to proxy tags {}text{}".format(message_text, leading_mentions,
                                                                                     prefix, suffix))

    if message_text.startswith(member.prefix or "") and message_text.endswith(member.suffix or ""):
        prefix_length = len(prefix)
        suffix_length = len(suffix)

        # If suffix_length is 0, the last bit of the slice will be "-0", and the slice will fail
        if suffix_length > 0:
            inner_string = message_text[prefix_length:-suffix_length]
        else:
            inner_string = message_text[prefix_length:]

        # Add the mentions we stripped back
        inner_string = leading_mentions + inner_string
        return inner_string


def match_proxy_tags(members: List[db.ProxyMember], message_text: str):
    # Sort by specificity (members with both prefix and suffix go higher)
    # This will make sure more "precise" proxy tags get tried first
    members: List[db.ProxyMember] = sorted(members, key=lambda x: int(
        bool(x.prefix)) + int(bool(x.suffix)), reverse=True)

    for member in members:
        match = match_member_proxy_tags(member, message_text)
        if match is not None:  # Using "is not None" because an empty string is OK here too
            logger.debug("Matched member {} with inner text '{}'".format(member.hid, match))
            return member, match


async def get_or_create_webhook_for_channel(conn, channel: discord.TextChannel):
    # First, check if we have one saved in the DB
    webhook_from_db = await db.get_webhook(conn, channel.id)
    if webhook_from_db:
        webhook_id, webhook_token = webhook_from_db

        session = channel._state.http._session
        hook = discord.Webhook.partial(webhook_id, webhook_token, adapter=discord.AsyncWebhookAdapter(session))

        # Workaround for https://github.com/Rapptz/discord.py/issues/1242
        hook._adapter.store_user = hook._adapter._store_user
        return hook

    # If not, we create one and save it
    created_webhook = await channel.create_webhook(name="PluralKit Proxy Webhook")
    await db.add_webhook(conn, channel.id, created_webhook.id, created_webhook.token)
    return created_webhook


async def make_attachment_file(message: discord.Message):
    if not message.attachments:
        return None

    first_attachment = message.attachments[0]

    # Copy the file data to the buffer
    # TODO: do this without buffering... somehow
    bio = BytesIO()
    await first_attachment.save(bio)

    return discord.File(bio, first_attachment.filename)


async def do_proxy_message(conn, original_message: discord.Message, proxy_member: db.ProxyMember,
                           inner_text: str, logger: ChannelLogger):
    # Send the message through the webhook
    webhook = await get_or_create_webhook_for_channel(conn, original_message.channel)
    sent_message = await webhook.send(
        content=inner_text,
        username=proxy_member.name,
        avatar_url=proxy_member.avatar_url,
        file=await make_attachment_file(original_message),
        wait=True
    )

    # Save the proxied message in the database
    await db.add_message(conn, sent_message.id, original_message.channel.id, proxy_member.id,
                         original_message.author.id)

    await logger.log_message_proxied(
        conn,
        original_message.channel.guild.id,
        original_message.channel.name,
        original_message.channel.id,
        original_message.author.name,
        original_message.author.discriminator,
        original_message.author.id,
        proxy_member.name,
        proxy_member.hid,
        proxy_member.avatar_url,
        proxy_member.system_name,
        proxy_member.system_hid,
        inner_text,
        sent_message.attachments[0].url if sent_message.attachments else None,
        sent_message.created_at,
        sent_message.id
    )

    # And finally, gotta delete the original.
    await original_message.delete()


async def try_proxy_message(conn, message: discord.Message, logger: ChannelLogger) -> bool:
    # Don't bother proxying in DMs with the bot
    if isinstance(message.channel, discord.abc.PrivateChannel):
        return False

    # Return every member associated with the account
    members = await db.get_members_by_account(conn, message.author.id)
    proxy_match = match_proxy_tags(members, message.content)
    if not proxy_match:
        # No proxy tags match here, we done
        return False

    member, inner_text = proxy_match

    # Sanitize inner text for @everyones and such
    inner_text = utils.sanitize(inner_text)

    # If we don't have an inner text OR an attachment, we cancel because the hook can't send that
    if not inner_text and not message.attachments:
        return False

    # So, we now have enough information to successfully proxy a message
    async with conn.transaction():
        await do_proxy_message(conn, message, member, inner_text, logger)
    return True


async def handle_deleted_message(conn, client: discord.Client, message_id: int,
                                 message_content: Optional[str], logger: channel_logger.ChannelLogger) -> bool:
    msg = await db.get_message(conn, message_id)
    if not msg:
        return False

    channel = client.get_channel(msg.channel)
    if not channel:
        # Weird edge case, but channel *could* be deleted at this point (can't think of any scenarios it would be tho)
        return False

    await db.delete_message(conn, message_id)
    await logger.log_message_deleted(
        conn,
        channel.guild.id,
        channel.name,
        msg.name,
        msg.hid,
        msg.avatar_url,
        msg.system_name,
        msg.system_hid,
        message_content,
        message_id
    )
    return True


async def try_delete_by_reaction(conn, client: discord.Client, message_id: int, reaction_user: int,
                                 logger: channel_logger.ChannelLogger) -> bool:
    # Find the message by the given message id or reaction user
    msg = await db.get_message_by_sender_and_id(conn, message_id, reaction_user)
    if not msg:
        # Either the wrong user reacted or the message isn't a proxy message
        # In either case - not our problem
        return False

    # Find the original message
    original_message = await client.get_channel(msg.channel).get_message(message_id)
    if not original_message:
        # Message got deleted, possibly race condition, eh
        return False

    # Then delete the original message
    await original_message.delete()

    await handle_deleted_message(conn, client, message_id, original_message.content, logger)

# # TODO: possibly move this to bot __init__ so commands can access it too
# class WebhookPermissionError(Exception):
#     pass
#
#
# class DeletionPermissionError(Exception):
#     pass
#
#
# class Proxy:
#     def __init__(self, client: discord.Client, token: str, logger: channel_logger.ChannelLogger):
#         self.logger = logging.getLogger("pluralkit.bot.proxy")
#         self.session = aiohttp.ClientSession()
#         self.client = client
#         self.token = token
#         self.channel_logger = logger
#
#     async def save_channel_webhook(self, conn, channel: discord.Channel, id: str, token: str) -> (str, str):
#         await db.add_webhook(conn, channel.id, id, token)
#         return id, token
#
#     async def create_and_add_channel_webhook(self, conn, channel: discord.Channel) -> (str, str):
#         # This method is only called if there's no webhook found in the DB (and hopefully within a transaction)
#         # No need to worry about error handling if there's a DB conflict (which will throw an exception because DB constraints)
#         req_headers = {"Authorization": "Bot {}".format(self.token)}
#
#         # First, check if there's already a webhook belonging to the bot
#         async with self.session.get("https://discordapp.com/api/v6/channels/{}/webhooks".format(channel.id),
#                                     headers=req_headers) as resp:
#             if resp.status == 200:
#                 webhooks = await resp.json()
#                 for webhook in webhooks:
#                     if webhook["user"]["id"] == self.client.user.id:
#                         # This webhook belongs to us, we can use that, return it and save it
#                         return await self.save_channel_webhook(conn, channel, webhook["id"], webhook["token"])
#             elif resp.status == 403:
#                 self.logger.warning(
#                     "Did not have permission to fetch webhook list (server={}, channel={})".format(channel.server.id,
#                                                                                                    channel.id))
#                 raise WebhookPermissionError()
#             else:
#                 raise discord.HTTPException(resp, await resp.text())
#
#         # Then, try submitting a new one
#         req_data = {"name": "PluralKit Proxy Webhook"}
#         async with self.session.post("https://discordapp.com/api/v6/channels/{}/webhooks".format(channel.id),
#                                      json=req_data, headers=req_headers) as resp:
#             if resp.status == 200:
#                 webhook = await resp.json()
#                 return await self.save_channel_webhook(conn, channel, webhook["id"], webhook["token"])
#             elif resp.status == 403:
#                 self.logger.warning(
#                     "Did not have permission to create webhook (server={}, channel={})".format(channel.server.id,
#                                                                                                channel.id))
#                 raise WebhookPermissionError()
#             else:
#                 raise discord.HTTPException(resp, await resp.text())
#
#         # Should not be reached without an exception being thrown
#
#     async def get_webhook_for_channel(self, conn, channel: discord.Channel):
#         async with conn.transaction():
#             hook_match = await db.get_webhook(conn, channel.id)
#             if not hook_match:
#                 # We don't have a webhook, create/add one
#                 return await self.create_and_add_channel_webhook(conn, channel)
#             else:
#                 return hook_match
#
#     async def do_proxy_message(self, conn, member: db.ProxyMember, original_message: discord.Message, text: str,
#                                attachment_url: str, has_already_retried=False):
#         hook_id, hook_token = await self.get_webhook_for_channel(conn, original_message.channel)
#
#         form_data = aiohttp.FormData()
#         form_data.add_field("username", "{} {}".format(member.name, member.tag or "").strip())
#
#         if text:
#             form_data.add_field("content", text)
#
#         if attachment_url:
#             attachment_resp = await self.session.get(attachment_url)
#             form_data.add_field("file", attachment_resp.content, content_type=attachment_resp.content_type,
#                                 filename=attachment_resp.url.name)
#
#         if member.avatar_url:
#             form_data.add_field("avatar_url", member.avatar_url)
#
#         async with self.session.post(
#                 "https://discordapp.com/api/v6/webhooks/{}/{}?wait=true".format(hook_id, hook_token),
#                 data=form_data) as resp:
#             if resp.status == 200:
#                 message = await resp.json()
#
#                 await db.add_message(conn, message["id"], message["channel_id"], member.id, original_message.author.id)
#
#                 try:
#                     await self.client.delete_message(original_message)
#                 except discord.Forbidden:
#                     self.logger.warning(
#                         "Did not have permission to delete original message (server={}, channel={})".format(
#                             original_message.server.id, original_message.channel.id))
#                     raise DeletionPermissionError()
#                 except discord.NotFound:
#                     self.logger.warning("Tried to delete message when proxying, but message was already gone (server={}, channel={})".format(original_message.server.id, original_message.channel.id))
#
#                 message_image = None
#                 if message["attachments"]:
#                     first_attachment = message["attachments"][0]
#                     if "width" in first_attachment and "height" in first_attachment:
#                         # Only log attachments that are actually images
#                         message_image = first_attachment["url"]
#
#                 await self.channel_logger.log_message_proxied(conn,
#                                                               server_id=original_message.server.id,
#                                                               channel_name=original_message.channel.name,
#                                                               channel_id=original_message.channel.id,
#                                                               sender_name=original_message.author.name,
#                                                               sender_disc=original_message.author.discriminator,
#                                                               sender_id=original_message.author.id,
#                                                               member_name=member.name,
#                                                               member_hid=member.hid,
#                                                               member_avatar_url=member.avatar_url,
#                                                               system_name=member.system_name,
#                                                               system_hid=member.system_hid,
#                                                               message_text=text,
#                                                               message_image=message_image,
#                                                               message_timestamp=ciso8601.parse_datetime(
#                                                                   message["timestamp"]),
#                                                               message_id=message["id"])
#             elif resp.status == 404 and not has_already_retried:
#                 # Webhook doesn't exist. Delete it from the DB, create, and add a new one
#                 self.logger.warning("Webhook registered in DB doesn't exist, deleting hook from DB, re-adding, and trying again (channel={}, hook={})".format(original_message.channel.id, hook_id))
#                 await db.delete_webhook(conn, original_message.channel.id)
#                 await self.create_and_add_channel_webhook(conn, original_message.channel)
#
#                 # Then try again all over, making sure to not retry again and go in a loop should it continually fail
#                 return await self.do_proxy_message(conn, member, original_message, text, attachment_url, has_already_retried=True)
#             else:
#                 raise discord.HTTPException(resp, await resp.text())
#
#     async def try_proxy_message(self, conn, message: discord.Message):
#         # Can't proxy in DMs, webhook creation will explode
#         if message.channel.is_private:
#             return False
#
#         # Big fat query to find every member associated with this account
#         # Returned member object has a few more keys (system tag, for example)
#         members = await db.get_members_by_account(conn, account_id=message.author.id)
#
#         match = match_proxy_tags(members, message.content)
#         if not match:
#             return False
#
#         member, text = match
#         attachment_url = get_message_attachment_url(message)
#
#         # Can't proxy a message with no text AND no attachment
#         if not text and not attachment_url:
#             self.logger.debug("Skipping message because of no text and no attachment")
#             return False
#
#         # Remember to sanitize the text (remove @everyones and such)
#         text = utils.sanitize(text)
#
#         try:
#             async with conn.transaction():
#                 await self.do_proxy_message(conn, member, message, text=text, attachment_url=attachment_url)
#         except WebhookPermissionError:
#             embed = embeds.error("PluralKit does not have permission to manage webhooks for this channel. Contact your local server administrator to fix this.")
#             await self.client.send_message(message.channel, embed=embed)
#         except DeletionPermissionError:
#             embed = embeds.error("PluralKit does not have permission to delete messages in this channel. Contact your local server administrator to fix this.")
#             await self.client.send_message(message.channel, embed=embed)
#
#         return True
#
#     async def try_delete_message(self, conn, message_id: str, check_user_id: Optional[str], delete_message: bool, deleted_by_moderator: bool):
#         async with conn.transaction():
#             # Find the message in the DB, and make sure it's sent by the user (if we need to check)
#             if check_user_id:
#                 db_message = await db.get_message_by_sender_and_id(conn, message_id=message_id, sender_id=check_user_id)
#             else:
#                 db_message = await db.get_message(conn, message_id=message_id)
#
#             if db_message:
#                 self.logger.debug("Deleting message {}".format(message_id))
#                 channel = self.client.get_channel(str(db_message.channel))
#
#                 # Fetch the original message from the server
#                 try:
#                     original_message = await self.client.get_message(channel, message_id)
#                 except discord.NotFound:
#                     # Just in case it's already gone
#                     original_message = None
#
#                 # If we should also delete the actual message, do that
#                 if delete_message and original_message is not None:
#                     try:
#                         await self.client.delete_message(original_message)
#                     except discord.Forbidden:
#                         self.logger.warning(
#                             "Did not have permission to remove message, aborting deletion (server={}, channel={})".format(
#                                 channel.server.id, channel.id))
#                         return
#
#                 # Remove it from the DB
#                 await db.delete_message(conn, message_id)
#
#                 # Then log deletion to logging channel
#                 await self.channel_logger.log_message_deleted(conn,
#                                                         server_id=channel.server.id,
#                                                         channel_name=channel.name,
#                                                         member_name=db_message.name,
#                                                         member_hid=db_message.hid,
#                                                         member_avatar_url=db_message.avatar_url,
#                                                         system_name=db_message.system_name,
#                                                         system_hid=db_message.system_hid,
#                                                         message_text=original_message.content if original_message else "*(original not found)*",
#                                                         message_id=message_id,
#                                                         deleted_by_moderator=deleted_by_moderator)
#
#     async def handle_reaction(self, conn, user_id: str, message_id: str, emoji: str):
#         if emoji == "❌":
#             await self.try_delete_message(conn, message_id, check_user_id=user_id, delete_message=True, deleted_by_moderator=False)
#
#     async def handle_deletion(self, conn, message_id: str):
#         # Don't delete the message, it's already gone at this point, just handle DB deletion and logging
#         await self.try_delete_message(conn, message_id, check_user_id=None, delete_message=False, deleted_by_moderator=True)

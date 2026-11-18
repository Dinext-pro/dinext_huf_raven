"""Resolve an incoming Raven message and its current audience from site records."""

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import frappe
from bs4 import BeautifulSoup
from frappe import _


EVENT = "Dinext AI Event"
FILE_TYPES = {"File", "Image"}


def denied():
    raise frappe.PermissionError(_("Access to this assistant request has changed."))


def is_human_message(message, user):
    return bool(
        user and user not in {"Guest", "Administrator"} and message.owner == user
        and message.message_type in {"Text", *FILE_TYPES}
        and not message.is_bot_message and not message.bot
    )


def mentions_bot(message, bot_user):
    if not message.text or not any(row.user == bot_user for row in message.mentions or []):
        return False
    # Raven populates child rows from the composer span. Require the span as
    # well because a File message with no caption skips Raven's HTML parser.
    return BeautifulSoup(message.text, "html.parser").find(
        "span", attrs={"data-type": "userMention", "data-id": bot_user}
    ) is not None


def is_private_pair(channel, pair, members):
    if not channel.is_direct_message or channel.is_self_message or len(pair) != 2 or members != pair:
        return False
    peers = {channel.dm_user_1, channel.dm_user_2}
    # Older DMs can predate Raven's denormalized peer fields.
    return peers == pair or not any(peers)


def local_file_url(url):
    """Allow local Frappe attachments, including URL-encoded file names."""
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    return (
        not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment
        and path.startswith(("/private/files/", "/files/")) and "\\" not in path
        and not any(part in {".", ".."} for part in path.split("/"))
        and not any(ord(char) < 32 for char in path)
    )


def member_ids(channel_id, for_update=False):
    # Raven's per-channel cache is suitable for UI. Delivery needs the live audience.
    return set(frappe.db.get_values(
        "Raven Channel Member", {"channel_id": channel_id}, "user_id", pluck=True, limit=3, for_update=for_update,
    ))


def check_channel(channel, user, bot_user, for_update=False):
    if channel.is_archived or channel.is_ai_thread or not channel.has_permission("read", user=user):
        denied()
    pair = {user, bot_user}
    if channel.is_direct_message:
        if not is_private_pair(channel, pair, member_ids(channel.name, for_update=for_update)):
            denied()
        return
    for participant in pair:
        if not frappe.db.exists("Raven Channel Member", {"channel_id": channel.name, "user_id": participant}):
            denied()
    if channel.workspace and not frappe.db.exists(
        "Raven Workspace Member", {"workspace": channel.workspace, "user": user}
    ):
        denied()


@dataclass
class Route:
    message: object
    channel: object
    root_channel: str
    dialog_id: str
    bot: object
    agent: object
    direct: bool
    files: list = field(default_factory=list)


def _bound_thread(message, channel, parent, bot, agent):
    filters = {"root_channel": parent.name, "raven_bot": bot.name, "agent": agent,
               "status": ["not in", ["Denied", "Failed", "Uncertain"]]}
    dialog = frappe.db.get_value(EVENT, {**filters, "source_message": channel.name}, "dialog_id")
    if dialog:
        return dialog
    # A thread can also acquire its first explicit mention after its creation.
    # An event must never establish its own authority on recheck or on an edit.
    if frappe.db.exists(EVENT, {
        **filters, "channel_id": channel.name, "source_message": ["!=", message.name],
        "creation": ["<", message.creation],
    }):
        return channel.name
    return None


def route_message(message, settings):
    user = frappe.session.user
    if not is_human_message(message, user) or not settings.enabled or not settings.agent or not settings.raven_bot:
        denied()
    channel = frappe.get_doc("Raven Channel", message.channel_id)
    bot = frappe.get_doc("Raven Bot", settings.raven_bot)
    if bot.is_ai_bot or not bot.raven_user:
        denied()
    mentioned = mentions_bot(message, bot.raven_user)
    if not (channel.is_direct_message or channel.is_thread or mentioned):
        denied()
    if not frappe.db.exists("User", {"name": user, "enabled": 1}):
        denied()
    if not frappe.db.exists("Raven User", {"name": user, "user": user, "enabled": 1, "type": "User"}):
        denied()
    if not frappe.db.exists("Raven User", {
        "name": bot.raven_user, "bot": bot.name, "enabled": 1, "type": "Bot",
    }):
        denied()

    check_channel(channel, user, bot.raven_user)
    if not message.has_permission("read", user=user):
        denied()
    root = channel
    dialog = channel.name if channel.is_direct_message else message.name
    if channel.is_thread:
        origin = frappe.get_doc("Raven Message", channel.name)
        if not origin.is_thread or channel.channel_name != origin.name or not origin.has_permission("read", user=user):
            denied()
        root = frappe.get_doc("Raven Channel", origin.channel_id)
        if root.is_thread:
            denied()
        check_channel(root, user, bot.raven_user)
        bound = _bound_thread(message, channel, root, bot, settings.agent)
        if not (bound or mentioned):
            denied()
        dialog = bound or channel.name

    from huf.ai.agent_access import TRIGGER_DIRECT_API, resolve_run_identity_and_authorize

    agent = frappe.get_doc("Agent", settings.agent)
    identity = resolve_run_identity_and_authorize(agent, TRIGGER_DIRECT_API, {"user": user})
    if agent.disabled or not identity.authorized or identity.run_as_user != user:
        denied()
    return Route(message, channel, root.name, dialog, bot, agent, bool(channel.is_direct_message))


def _attached_file(message, user):
    if not local_file_url(message.file):
        denied()
    name = frappe.db.get_value("File", {
        "attached_to_doctype": "Raven Message", "attached_to_name": message.name,
        "attached_to_field": "file", "file_url": message.file, "owner": user,
    })
    if not name:
        denied()
    doc = frappe.get_doc("File", name)
    if doc.is_folder or not doc.has_permission("read", user=user):
        denied()
    return {
        "file_id": doc.name, "file_name": doc.file_name, "file_url": doc.file_url,
        "is_image": message.message_type == "Image", "content_hash": doc.content_hash,
    }


def source_digest(route):
    # A thread flag, reactions and read receipts change independently of the prompt.
    data = [
        route.message.content, route.message.message_type, route.message.link_doctype,
        route.message.link_document, route.files,
    ]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def authorize_event(event):
    """Recheck the persisted source, principal, agent, current membership and attachments."""
    if not event.requester_user or event.requester_user != frappe.session.user:
        denied()
    message = frappe.get_doc("Raven Message", event.source_message)
    route = route_message(message, frappe.get_single("Dinext AI Settings"))
    if (event.channel_id, event.root_channel, event.dialog_id, event.raven_bot, event.agent) != (
        route.channel.name, route.root_channel, route.dialog_id, route.bot.name, route.agent.name,
    ):
        denied()
    if message.link_doctype or message.link_document:
        if not (message.link_doctype and message.link_document):
            denied()
        frappe.get_doc(message.link_doctype, message.link_document).check_permission("read")
    parts = json.loads(event.source_parts or "[]")
    if message.message_type in FILE_TYPES and message.name not in parts:
        parts.append(message.name)
    for name in parts:
        part = message if name == message.name else frappe.get_doc("Raven Message", name)
        if not (
            is_human_message(part, event.requester_user) and part.message_type in FILE_TYPES
            and part.channel_id == event.channel_id and part.has_permission("read")
        ):
            denied()
        route.files.append(_attached_file(part, event.requester_user))
    checks = json.loads(event.access_checks or "{}")
    if checks.get("source") and checks["source"] != source_digest(route):
        denied()
    return route

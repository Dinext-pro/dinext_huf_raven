"""Private result delivery from a committed assistant event."""

import re
from html import escape
from urllib.parse import unquote, urlsplit

import frappe
from frappe import _

from dinext_huf_raven import bridge
from dinext_huf_raven.raven_context import EVENT, authorize_event, check_channel, denied


_MARKDOWN_LINK = re.compile(r"\[([^\]\n]{1,250})\]\((/app/[^\s()<>]+)\)")


def _desk_link(url):
    path = unquote(urlsplit(url).path)
    return (path.startswith("/app/") and "\\" not in path
            and not any(part in {".", ".."} for part in path.split("/"))
            and not any(ord(char) < 32 for char in url))


def response_html(response, files):
    """Escape model content; make local Desk links and checked output Files clickable."""
    text = response or ""
    parts = []
    start = 0
    for match in _MARKDOWN_LINK.finditer(text):
        if not _desk_link(match[2]):
            continue
        parts.extend((escape(text[start:match.start()]),
                      f'<a href="{escape(match[2], quote=True)}">{escape(match[1])}</a>'))
        start = match.end()
    parts.append(escape(text[start:]))
    body = "<p>" + "".join(parts).replace("\n", "<br>") + "</p>"
    for doc in files:
        body += f'<p><a href="{escape(doc.file_url, quote=True)}">{escape(doc.file_name)}</a></p>'
    return body


def _private_target(event, route):
    name = route.channel.name if route.direct else route.bot.create_direct_message_channel(event.requester_user)
    channel = frappe.get_doc("Raven Channel", name, for_update=True)
    if not channel.is_direct_message:
        denied()
    check_channel(channel, event.requester_user, route.bot.raven_user, for_update=True)
    return name


def _status_target(event, route):
    if route.channel.is_thread:
        return route.channel.name
    # A top-level mention becomes a Raven thread. Lock its source before testing
    # for an existing channel, including one opened manually by the employee.
    from raven.api.threads import create_thread

    source = frappe.get_doc("Raven Message", event.source_message, for_update=True)
    if not frappe.db.exists("Raven Channel", {"name": source.name, "is_thread": 1}):
        create_thread(source.name)
    channel = frappe.get_doc("Raven Channel", source.name)
    check_channel(channel, event.requester_user, route.bot.raven_user)
    return channel.name


def publish(event, route, files):
    """Return both Raven receipts; the caller records them in this same transaction."""
    target = _private_target(event, route)
    notice_target = None if route.direct else _status_target(event, route)
    reply = route.bot.send_message(target, text=response_html(event.response, files))
    notice = None
    if notice_target:
        notice = route.bot.send_message(notice_target, text=escape(_("I replied in your direct chat.")))
    return reply, notice


def deliver_event(event_name):
    """RQ entry point. A failed send rolls back messages and leaves the stored result for retry."""
    event = frappe.get_doc(EVENT, event_name, for_update=True)
    statuses = {"Ready", "Failed", "Uncertain"}
    if event.status not in statuses or event.reply_message:
        return
    try:
        with bridge.employee_context(event.requester_user):
            route = authorize_event(event)
            files = bridge.check_disclosure(event) if event.status == "Ready" else []
            reply, notice = publish(event, route, files)
            frappe.db.set_value(EVENT, event.name, {
                "reply_message": reply, "status_message": notice,
                "status": "Delivered" if event.status == "Ready" else event.status,
            })
    except (frappe.PermissionError, frappe.DoesNotExistError):
        # Roll back any partial Raven send or thread before withholding the result.
        frappe.db.rollback()
        current = frappe.get_doc(EVENT, event.name, for_update=True)
        if current.status in statuses and not current.reply_message:
            frappe.db.set_value(EVENT, current.name, {"status": "Denied", "response": None})

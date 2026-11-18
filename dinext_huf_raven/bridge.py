"""Durable Raven ingress, employee-scoped Huf execution and recovery."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from redis.exceptions import LockNotOwnedError, RedisError

from dinext_huf_raven.raven_context import (
    EVENT, FILE_TYPES, authorize_event, denied, is_human_message, local_file_url,
    route_message, source_digest,
)


JOB_TIMEOUT = 600
PRIVATE_CHANNEL = "raven"
MAX_HISTORY_EVENTS = 30


def dialog_key(event, site):
    parts = [site, event.agent, event.raven_bot, event.requester_user, event.root_channel, event.dialog_id]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _run_key(event):
    parts = [frappe.local.site, EVENT, event.name]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _queue(method, event_name, queue, timeout):
    try:
        frappe.enqueue(
            method, event_name=event_name, queue=queue, timeout=timeout, enqueue_after_commit=True,
            job_id=f"{method}-{event_name}", deduplicate=True,
        )
    except (RedisError, frappe.QueueOverloaded):
        # The scheduler can replay the durable outbox after Redis becomes available.
        frappe.logger("dinext_huf_raven").warning("Queue unavailable for assistant event %s", event_name)


def enqueue_delivery(event_name):
    _queue("dinext_huf_raven.delivery.deliver_event", event_name, "default", 300)


def enqueue_processing(event_name):
    _queue("dinext_huf_raven.bridge.process_event", event_name, "long", JOB_TIMEOUT)


def on_message(doc, method=None):
    """Register the final message of a human send, in the Raven insert transaction."""
    if not is_human_message(doc, frappe.session.user):
        return
    if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
        return
    settings = frappe.get_single("Dinext AI Settings")
    if not settings.enabled:
        return
    # Raven v3 stamps skip_channel_summary only on the non-final attachments.
    # Use server-observed siblings, including sends without a client batch ID.
    pending = frappe.flags.setdefault("dinext_ai_batch_parts", {})
    batch = (doc.owner, doc.channel_id, doc.message_batch_id)
    if doc.flags.get("skip_channel_summary"):
        if doc.message_type in FILE_TYPES:
            pending.setdefault(batch, []).append(doc.name)
        return
    parts = pending.pop(batch, [])
    if doc.message_type in FILE_TYPES:
        parts.append(doc.name)
    try:
        route = route_message(doc, settings)
    except (frappe.PermissionError, frappe.DoesNotExistError):
        return
    existing = frappe.db.get_value(EVENT, {"source_message": doc.name}, "name")
    if existing:
        return existing
    event = frappe.get_doc({
        "doctype": EVENT, "source_message": doc.name, "requester_user": doc.owner,
        "channel_id": doc.channel_id, "root_channel": route.root_channel, "dialog_id": route.dialog_id,
        "raven_bot": route.bot.name, "agent": route.agent.name, "source_parts": json.dumps(parts),
        "status": "Pending",
    })
    frappe.db.savepoint("dinext_ai_register")
    try:
        # Only this hook can create an event. Its identity fields come from verified site records.
        event.insert(ignore_permissions=True)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point="dinext_ai_register")
        duplicate = frappe.db.get_value(EVENT, {"source_message": doc.name}, "name", for_update=True)
        if not duplicate:
            raise
        return duplicate
    enqueue_processing(event.name)
    return event.name


@contextmanager
def employee_context(user):
    """Isolate Frappe's user-dependent session and permission caches in this worker."""
    local = frappe.local
    changed = ("session", "cache", "form_dict", "jenv_restricted", "jenv_unrestricted", "role_permissions",
               "new_doc_templates", "user_perms", "lang", "flags")
    original = {key: getattr(local, key) for key in changed}
    local.session = frappe._dict(original["session"])
    local.flags = frappe._dict(original["flags"])
    try:
        frappe.set_user(user)
        local.lang = frappe.db.get_value("User", user, "language") or original["lang"]
        yield
    finally:
        for key, value in original.items():
            setattr(local, key, value)


@contextmanager
def _event_context(event, checking=False):
    flags = frappe.flags
    previous = flags.get("dinext_ai_event"), flags.get("dinext_ai_checking")
    flags.dinext_ai_event = event.name
    flags.dinext_ai_checking = checking
    try:
        yield
    finally:
        flags.dinext_ai_event, flags.dinext_ai_checking = previous


def current_event():
    """Return the verified server-selected source to a native tool in this run."""
    name = frappe.flags.get("dinext_ai_event")
    if not name:
        denied()
    event = frappe.get_doc(EVENT, name)
    if event.status != "Running" and not frappe.flags.get("dinext_ai_checking"):
        denied()
    authorize_event(event)
    return event


def _append_check(key, value):
    if frappe.flags.get("dinext_ai_checking"):
        return
    event = current_event()
    checks = json.loads(frappe.db.get_value(EVENT, event.name, "access_checks", for_update=True) or "{}")
    values = checks.setdefault(key, [])
    if value not in values:
        values.append(value)
        frappe.db.set_value(EVENT, event.name, "access_checks", json.dumps(checks), update_modified=False)


def track_business_read(check):
    """Remember the permission-aware read that produced a model-visible result."""
    _append_check("reads", check)


def _private_file(name, requester):
    doc = frappe.get_doc("File", name)
    if not (doc.is_private and doc.owner == requester and local_file_url(doc.file_url)
            and doc.file_url.startswith("/private/files/") and doc.has_permission("read")):
        denied()
    return doc


def add_artifact(file_doc):
    """Register a generated employee-owned private file for deterministic DM delivery."""
    doc = _private_file(file_doc.name, frappe.session.user)
    _append_check("artifacts", doc.name)


def _check_reads(event):
    from dinext_huf_raven import analytics, business

    checks = json.loads(event.access_checks or "{}")
    with _event_context(event, checking=True):
        for check in checks.get("reads", []):
            recheck = analytics.recheck_disclosure if check.get("module") == "analytics" else business.recheck_disclosure
            if not recheck(check):
                denied()
        return [_private_file(name, event.requester_user) for name in checks.get("artifacts", [])]


def check_disclosure(event):
    """Check the current result and the earlier turns that could have informed it."""
    files = _check_reads(event)
    if event.huf_conversation:
        history = frappe.get_all(EVENT, filters={"huf_conversation": event.huf_conversation},
                                 fields=["name", "creation"], order_by="creation asc, name asc",
                                 limit=MAX_HISTORY_EVENTS + 2)
        for turn in history:
            if turn.name == event.name:
                break
            earlier = frappe.get_doc(EVENT, turn.name)
            if (earlier.status not in ("Ready", "Delivered")
                    or dialog_key(earlier, frappe.local.site) != dialog_key(event, frappe.local.site)):
                denied()
            authorize_event(earlier)
            _check_reads(earlier)
        else:
            denied()
    return files


def _conversation_is_current(event, conversation):
    if not (
        conversation.owner == event.requester_user and conversation.agent == event.agent
        and conversation.channel == PRIVATE_CHANNEL and conversation.is_active
        and conversation.execution_host == "server"
    ):
        return False
    previous = frappe.get_all(
        EVENT, filters={"huf_conversation": conversation.name}, pluck="name", limit=MAX_HISTORY_EVENTS + 1,
    )
    if len(previous) > MAX_HISTORY_EVENTS:
        return False
    # Huf keeps dialogue history. Rotate the conversation if a past source, File
    # or business result is no longer visible under this employee's current rights.
    for name in previous:
        past = frappe.get_doc(EVENT, name)
        if past.status not in ("Ready", "Delivered") or dialog_key(past, frappe.local.site) != dialog_key(event, frappe.local.site):
            return False
        try:
            authorize_event(past)
            _check_reads(past)
        except (frappe.PermissionError, frappe.DoesNotExistError):
            return False
    return True


def _conversation(event):
    from huf.ai.conversation_manager import ConversationManager

    previous = frappe.db.get_value(EVENT, {
        "requester_user": event.requester_user, "agent": event.agent, "raven_bot": event.raven_bot,
        "root_channel": event.root_channel, "dialog_id": event.dialog_id, "huf_conversation": ["is", "set"],
        "name": ["!=", event.name],
    }, "huf_conversation", order_by="creation desc, name desc")
    if previous and frappe.db.exists("Agent Conversation", previous):
        conversation = frappe.get_doc("Agent Conversation", previous)
        if _conversation_is_current(event, conversation):
            return conversation
    # Only reuse conversations recorded by this adapter. The upstream API also
    # accepts caller-supplied external_id; give each new session an opaque value.
    external_id = secrets.token_hex(32)
    return ConversationManager(event.agent, channel=PRIVATE_CHANNEL, external_id=external_id).create_new_conversation()


def _prompt(route):
    text = route.message.content or ""
    if route.files:
        files = [{"file_id": item["file_id"], "name": item["file_name"]} for item in route.files]
        text += "\n\n" + _("Attached files (use dinext_extract_file for document text)") + "\n" + json.dumps(files, ensure_ascii=False)
    return text


def _update(event, **values):
    frappe.db.set_value(EVENT, event.name, values)
    event.update(values)


def _finish(event, response):
    from dinext_huf_raven.actions import render_for_event

    canonical = render_for_event(event)
    result = "\n\n".join(part for part in (response, canonical) if part)
    _update(event, status="Ready", response=result or _("Request completed."))
    enqueue_delivery(event.name)


def _run_record(event):
    if not event.huf_conversation:
        return None
    return frappe.db.get_value("Agent Run", {
        "conversation": event.huf_conversation, "agent": event.agent, "owner": event.requester_user,
        "idempotency_key": _run_key(event),
    }, ["name", "status", "response"], as_dict=True)


def _reconcile(event, stale=False):
    """Use Huf's persisted outcome after a crash; never resubmit an ambiguous run."""
    run = _run_record(event)
    if run:
        if event.huf_run != run.name:
            _update(event, huf_run=run.name)
        if run.status == "Success":
            _finish(event, run.response)
            return
        if run.status in {"Failed", "Cancelled", "Canceled"}:
            _update(event, status="Failed", response=_("The assistant could not complete this request."))
            enqueue_delivery(event.name)
            return
    if stale:
        _update(event, status="Uncertain", response=_("The request needs review before it can be repeated."))
        enqueue_delivery(event.name)


def _execute(event):
    from dinext_huf_raven import actions, setup
    from huf.ai.agent_integration import run_agent_sync

    route = authorize_event(event)
    agent = setup.check_agent(event.agent)
    if not agent.persist_conversation:
        denied()
    checks = json.loads(event.access_checks or "{}")
    checks["source"] = source_digest(route)
    _update(event, status="Running", access_checks=json.dumps(checks))
    # A confirmation may roll back a rejected ERP operation independently of the ingress claim.
    frappe.db.commit()
    with _event_context(event):
        if route.message.text and not route.message.is_forwarded:
            result = actions.handle_command(event, route.message.content or "")
            if result is not None:
                # The proposal decision, ERP mutation and reply payload share this transaction.
                _finish(event, result)
                return
        conversation = _conversation(event)
        _update(event, huf_conversation=conversation.name)
        # Huf itself commits during a run. Persist its precise recovery key before it can start.
        frappe.db.commit()
        try:
            run_agent_sync(
                agent_name=event.agent, prompt=_prompt(route), channel_id=PRIVATE_CHANNEL,
                external_id=conversation.external_id, conversation_id=conversation.name, now=True,
                files=route.files, client_idempotency_key=_run_key(event),
            )
        except Exception as error:
            # Discard a half-written transaction, then inspect Huf's durable outcome.
            frappe.db.rollback()
            frappe.logger("dinext_huf_raven").warning("Huf raised %s for event %s", type(error).__name__, event.name)
            _reconcile(event, stale=True)
            return
        _reconcile(event, stale=True)


def _earlier_pending(event):
    rows = frappe.get_all(EVENT, filters={
        "requester_user": event.requester_user, "raven_bot": event.raven_bot, "agent": event.agent,
        "root_channel": event.root_channel, "dialog_id": event.dialog_id, "status": ["in", ["Pending", "Running"]],
    }, fields=["name"], order_by="creation asc, name asc", limit=1)
    return rows[0].name if rows and rows[0].name != event.name else None


def process_event(event_name):
    """RQ entry point. Only the event key crosses the queue boundary."""
    event = frappe.get_doc(EVENT, event_name)
    if event.status == "Ready":
        enqueue_delivery(event.name)
        return
    if event.status not in ("Pending", "Running"):
        return
    lock_name = frappe.cache.make_key("dinext-ai-dialog:" + dialog_key(event, frappe.local.site))
    lock = frappe.cache.lock(lock_name, timeout=JOB_TIMEOUT + 90, blocking=False)
    if not lock.acquire(blocking=False):
        return
    try:
        event = frappe.get_doc(EVENT, event_name, for_update=True)
        with employee_context(event.requester_user):
            if event.status == "Pending":
                earlier = _earlier_pending(event)
                if earlier:
                    enqueue_processing(earlier)
                    return
                _execute(event)
            elif event.status == "Running":
                authorize_event(event)
                stale = get_datetime(event.modified) < now_datetime() - timedelta(seconds=JOB_TIMEOUT + 90)
                _reconcile(event, stale=stale)
        # Release the per-dialogue lock only after the next worker can see the outcome.
        frappe.db.commit()
    except (frappe.PermissionError, frappe.DoesNotExistError):
        frappe.db.rollback()
        _update(event, status="Denied", response=None)
        frappe.db.commit()
    finally:
        try:
            lock.release()
        except LockNotOwnedError:
            pass


def recover():
    """Requeue pending runs and committed replies independently after a worker/broker restart."""
    if not frappe.get_single("Dinext AI Settings").enabled:
        return
    for status in ("Pending", "Running", "Ready", "Failed", "Uncertain"):
        filters = {"status": status}
        if status == "Running":
            filters["modified"] = ["<", now_datetime() - timedelta(seconds=JOB_TIMEOUT + 90)]
        if status in ("Failed", "Uncertain"):
            filters["reply_message"] = ["is", "not set"]
        for name in frappe.get_all(EVENT, filters=filters, pluck="name", order_by="modified asc", limit=100):
            if status in ("Pending", "Running"):
                enqueue_processing(name)
            else:
                enqueue_delivery(name)

"""Propose Huf business writes and settle an explicit decision from the employee's bot DM."""

import hmac
import json
import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.model import table_fields
from frappe.model.workflow import apply_workflow, get_workflow_name
from frappe.utils import get_datetime, now_datetime

from dinext_huf_raven.business import transitions_for
from dinext_huf_raven.business_fields import (
    STANDARD, apply_values, business_meta, can, canonical, checked_doc, checked_filters, deny,
    fingerprint, identifier, invalid, mapping, visible_fields,
)


ACTION = "Dinext AI Action"
PERMISSIONS = {"create": "create", "update": "write", "delete": "delete",
               "submit": "submit", "cancel": "cancel", "workflow": "read"}
COMMAND = re.compile(r"/?(confirm|reject|подтвердить|отклонить)\s+([a-f0-9]{32})", re.I)


class StaleProposal(Exception):
    pass


def _timestamp(value):
    return get_datetime(value).isoformat(timespec="microseconds") if value else ""


def _digest(action):
    return fingerprint({
        "site": frappe.local.site, "requester": action.requester_user, "source_event": action.source_event,
        "channel": action.channel_id, "thread": action.thread_id or "", "run": action.agent_run,
        "payload": action.payload_json, "version": action.target_version or "", "expiry": _timestamp(action.expires_at),
    })


def _actor(event):
    user = frappe.session.user
    if user in (None, "Administrator", "Guest") or user != event.requester_user:
        deny()
    return user


def _payload(operation, doctype, name, values, workflow_action):
    operation, doctype = identifier(operation), identifier(doctype)
    if operation not in PERMISSIONS or doctype == "User":
        deny()
    values = mapping(values)
    if operation != "create" and not name or operation in {"workflow", "submit", "delete", "cancel"} and values:
        invalid()
    if operation in {"create", "update"} and not values:
        invalid()
    if (operation == "workflow") != bool(workflow_action):
        invalid()
    return {"operation": operation, "doctype": doctype, "name": identifier(name) if name else None,
            "values": values, "workflow_action": identifier(workflow_action) if workflow_action else None}


def _dashboard(doc, values):
    if doc.get("is_standard") or "module" in values or values.get("is_standard"):
        deny()
    if doc.doctype == "Dashboard":
        if values.get("is_default") or doc.get("is_default"):
            deny()
        return
    if doc.get("source") or doc.chart_type == "Custom" or doc.get("dynamic_filters_json") not in (None, "", "[]", "{}"):
        invalid()
    if doc.chart_type == "Report":
        from dinext_huf_raven.business_reports import _report

        _report(doc.report_name)
        return
    target = business_meta(doc.document_type)
    if not can(target.name):
        deny()
    fields = set(STANDARD) | {k for k, df in visible_fields(target).items() if df.fieldtype not in table_fields}
    for key in ("based_on", "value_based_on", "group_by_based_on", "aggregate_function_based_on"):
        if doc.get(key) and doc.get(key) not in fields:
            deny()
    try:
        filters = json.loads(doc.filters_json or "[]")
    except (TypeError, ValueError):
        invalid()
    if isinstance(filters, dict):
        checked_filters(fields, filters)
    elif isinstance(filters, list):
        for condition in filters:
            if not isinstance(condition, list) or len(condition) not in {3, 4}:
                invalid()
            if len(condition) == 4 and condition[0] != target.name:
                deny()
            key, op, value = condition[-3:]
            checked_filters(fields, {identifier(key): [op, value]})
    else:
        invalid()


def _prepare(payload, *, lock=False, expected=None):
    op, dt, name, values = (payload[k] for k in ("operation", "doctype", "name", "values"))
    meta = business_meta(dt)
    if op == "create":
        if not can(dt, "create"):
            deny()
        if name and (meta.autoname or "").lower() != "prompt":
            invalid()
        doc = frappe.new_doc(dt)
        version = ""
    else:
        doc = checked_doc(dt, name, permission=PERMISSIONS[op], lock=lock)
        version = _timestamp(doc.modified)
    if expected is not None and version != expected:
        raise StaleProposal
    if op in {"submit", "cancel"}:
        if get_workflow_name(dt):
            frappe.throw(_("Use an available workflow action for this document."))
        if not meta.is_submittable or doc.docstatus != (0 if op == "submit" else 1):
            invalid()
    if op == "workflow" and payload["workflow_action"] not in transitions_for(doc):
        deny()
    if op == "delete" and doc.docstatus == 1:
        invalid()
    if op in {"create", "update"}:
        apply_values(doc, values, create=op == "create")
        if op == "create" and not can(dt, "create", doc):
            deny()
        if dt in {"Dashboard", "Dashboard Chart"}:
            if dt == "Dashboard Chart" and not doc.filters_json:
                doc.filters_json = "[]"
            _dashboard(doc, values)
    return doc, version


def _execute(payload, doc):
    operation = payload["operation"]
    if operation == "create":
        doc.insert(set_name=payload["name"])
    elif operation == "update":
        doc.save()
    elif operation == "delete":
        doc.delete()
    elif operation == "submit":
        doc.submit()
    elif operation == "cancel":
        doc.cancel()
    elif operation == "workflow":
        updated = apply_workflow({"doctype": doc.doctype, "name": doc.name}, payload["workflow_action"])
        if updated is None:
            return {"doctype": doc.doctype, "name": doc.name, "queued": True}
        doc = updated
    return {"doctype": doc.doctype, "name": doc.name, "docstatus": int(doc.docstatus)}


def propose(operation, doctype, name=None, values=None, workflow_action=None, **runtime):
    from dinext_huf_raven import bridge

    event = bridge.current_event()
    user = _actor(event)
    run = frappe.flags.get("huf_current_agent_run_id")
    if not run:
        deny()
    payload = _payload(operation, doctype, name, values, workflow_action)
    key = fingerprint([event.name, user, payload])[:32]
    # Source-event lock serializes identical model calls without taking a gap lock on a new action.
    frappe.db.get_value(event.doctype, event.name, "name", for_update=True)
    if frappe.db.exists(ACTION, key):
        action = frappe.get_doc(ACTION, key)
        if action.requester_user != user or action.source_event != event.name:
            deny()
        bridge.track_business_read({"kind": "proposal", "action": key})
        return {"action_id": key, "status": action.status}
    _, version = _prepare(payload)
    action = frappe.get_doc({
        "doctype": ACTION, "requester_user": user, "source_event": event.name, "agent_run": run,
        "channel_id": event.channel_id, "thread_id": event.get("thread_id") or "",
        "payload_json": canonical(payload), "target_version": version,
        "expires_at": (now_datetime() + timedelta(minutes=15)).replace(microsecond=0), "status": "Pending",
    })
    action.payload_hash = _digest(action)
    # Employees have no general create/write access to the private decision ledger.
    # This internal insert never grants permissions to the target business document.
    action.insert(ignore_permissions=True, set_name=key)
    bridge.track_business_read({"kind": "proposal", "action": key})
    return {"action_id": key, "status": "Pending", "expires_at": _timestamp(action.expires_at)}


def render_for_event(event):
    user = _actor(event)
    actions = frappe.get_all(ACTION, filters={"source_event": event.name, "requester_user": user, "status": "Pending"},
                             fields=["name"], order_by="creation asc")
    messages = []
    for row in actions:
        action = frappe.get_doc(ACTION, row.name)
        if not hmac.compare_digest(_digest(action), action.payload_hash or "") or get_datetime(action.expires_at) <= now_datetime():
            continue
        payload = json.loads(action.payload_json)
        try:
            _prepare(payload, expected=action.target_version or "")
        except (StaleProposal, frappe.DoesNotExistError, frappe.PermissionError, frappe.ValidationError):
            continue
        changes = json.dumps(payload, ensure_ascii=False, indent=2)
        messages.append(_("Proposed change {0}").format(row.name) + "\n" + changes)
        for key, val in payload["values"].items():
            if isinstance(val, list):
                mode = _("Update named rows and append new rows") if any("name" in item for item in val) else _("Replace all rows")
                messages.append(f"{key}: {mode}")
        if action.target_version:
            messages.append(_("Document version {0}").format(action.target_version))
        messages.append(_("Valid until {0}").format(action.expires_at))
        messages.append(_("Reply with “confirm {0}” or “reject {0}” in this bot DM.").format(row.name))
    return "\n".join(messages)


def _store(action, status, event, result=None):
    action.status = status
    action.result_json = canonical(result or {})
    action.decision_event = event.name
    frappe.db.set_value(ACTION, action.name, {
        "status": status, "decision_event": event.name, "result_json": action.result_json,
    })


def _receipt(action):
    if action.status == "Applied":
        result = json.loads(action.result_json or "{}")
        doc = result.get("name")
        if not doc:
            return _("The change was applied.")
        if result.get("queued"):
            return _("Frappe queued the approved workflow transition for {0}.").format(doc)
        return _("The change was applied to {0} {1}.").format(result["doctype"], doc)
    return {
        "Rejected": _("You declined this proposal."),
        "Expired": _("This proposal has expired."),
        "Stale": _("The document has changed. Ask for a new proposal."),
        "Failed": _("Frappe declined this change. Ask for a new proposal after checking the document and permissions."),
        "Applying": _("The decision was recorded. Its outcome is being verified."),
        "Uncertain": _("The outcome requires verification by an administrator."),
    }.get(action.status, _("This proposal is pending."))


def _authorize_decision(event, action):
    from dinext_huf_raven import bridge

    if action.requester_user != _actor(event) or not bridge.authorize_event(event).direct:
        deny()
    original = frappe.get_doc(event.doctype, action.source_event)
    if original.requester_user != event.requester_user or original.raven_bot != event.raven_bot:
        deny()
    bridge.authorize_event(original)
    if not hmac.compare_digest(_digest(action), action.payload_hash or ""):
        deny()
    display = json.dumps(json.loads(action.payload_json), ensure_ascii=False, indent=2)
    if (original.status != "Delivered" or not original.reply_message or action.name not in (original.response or "")
            or display not in (original.response or "")):
        deny()


def recheck_action(check):
    from dinext_huf_raven import bridge

    event = bridge.current_event()
    action = frappe.get_doc(ACTION, identifier(check["action"]))
    if action.requester_user != _actor(event) or not hmac.compare_digest(_digest(action), action.payload_hash or ""):
        return False
    payload = json.loads(action.payload_json)
    if check["kind"] == "proposal":
        if action.source_event != event.name or action.status != "Pending" or get_datetime(action.expires_at) <= now_datetime():
            return False
        try:
            _prepare(payload, expected=action.target_version or "")
        except StaleProposal:
            return False
    else:
        _authorize_decision(event, action)
        if action.status == "Applied" and payload["operation"] != "delete":
            result = json.loads(action.result_json)
            checked_doc(result["doctype"], result["name"])
    return True


def handle_command(event, text):
    from dinext_huf_raven import bridge

    match = COMMAND.fullmatch((text or "").strip())
    if not match:
        return None
    _actor(event)
    key = match[2].lower()
    if not frappe.db.exists(ACTION, key):
        deny()
    action = frappe.get_doc(ACTION, key, for_update=True)
    _authorize_decision(event, action)
    bridge.track_business_read({"kind": "receipt", "action": key})
    if action.status != "Pending":
        if action.status == "Applying" and now_datetime() - get_datetime(action.modified) > timedelta(minutes=15):
            _store(action, "Uncertain", event)
        return _receipt(action)
    if get_datetime(action.expires_at) <= now_datetime():
        _store(action, "Expired", event)
        return _receipt(action)
    if match[1].casefold() in {"reject", "отклонить"}:
        _store(action, "Rejected", event)
        return _receipt(action)

    # This entry point runs in a dedicated Raven command job. A durable claim survives a
    # crash during custom controller side effects. No replay may take over an Applying action.
    _store(action, "Applying", event)
    frappe.db.commit()
    action = frappe.get_doc(ACTION, key, for_update=True)
    if action.status != "Applying" or action.decision_event != event.name:
        return _receipt(action)
    savepoint = "dinext_ai_confirm"
    frappe.db.savepoint(savepoint)
    try:
        _authorize_decision(event, action)
        if get_datetime(action.expires_at) <= now_datetime():
            _store(action, "Expired", event)
            return _receipt(action)
        payload = json.loads(action.payload_json)
        doc, _ = _prepare(payload, lock=True, expected=action.target_version or "")
        result = _execute(payload, doc)
        _store(action, "Applied", event, result)
        return _receipt(action)
    except Exception as exc:
        status = "Stale" if isinstance(exc, (StaleProposal, frappe.DoesNotExistError)) else "Failed"
        if not isinstance(exc, (StaleProposal, frappe.DoesNotExistError, frappe.PermissionError, frappe.ValidationError)):
            status = "Uncertain"
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            # A controller may have committed its own transaction before reporting an error.
            status = "Uncertain"
        # A savepoint alone leaves Frappe's after_commit/after_rollback callbacks alive.
        frappe.db.rollback()
        action = frappe.get_doc(ACTION, key, for_update=True)
        if action.status == "Applying" and action.decision_event == event.name:
            _store(action, status, event)
        return _receipt(action)

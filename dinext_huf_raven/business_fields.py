"""Document and field boundaries shared by the Raven read and proposal tools."""

import hashlib
import json
import math
import re

import frappe
from frappe import _
from frappe.model import display_fieldtypes, table_fields
from frappe.model.workflow import get_workflow, get_workflow_name


STANDARD = ("name", "owner", "creation", "modified", "modified_by", "docstatus")
PRIVATE_APPS = {"dinext_huf_raven", "huf", "raven", "frappe_assistant_core"}
FRAMEWORK_RECORDS = {"Address", "Contact", "ToDo", "Event", "Note", "Blog Post", "User",
                     "Dashboard", "Dashboard Chart"}
SECRET_FIELD = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|credential|otp|api_key|private_key|"
    r"encryption_key|access_key|auth_key|session_id|signature_key)(?:s?_|s?$)", re.I
)
FILTER_OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in", "between", "is"}


def deny():
    frappe.throw(_("You do not have access to this assistant operation."), frappe.PermissionError)


def invalid():
    frappe.throw(_("Check the requested operation, fields and values."), frappe.ValidationError)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 140 or any(ord(c) < 32 for c in value):
        invalid()
    return value


def mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            invalid()
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 100 or len(canonical(value)) > 48000:
        invalid()
    return value


def bounded_int(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        invalid()
    return value


def can(doctype, permission="read", doc=None):
    return frappe.has_permission(doctype, permission, doc=doc)


def allowed_type(meta):
    app = frappe.local.module_app.get(frappe.scrub(meta.module))
    return not (
        meta.istable or meta.issingle or meta.get("is_virtual") or app in PRIVATE_APPS
        or (not meta.custom and (not app or app == "frappe") and meta.name not in FRAMEWORK_RECORDS)
    )


def business_meta(doctype):
    doctype = identifier(doctype)
    meta = frappe.get_meta(doctype)
    if not allowed_type(meta):
        deny()
    return meta


def visible_fields(meta, permission="read", parent=None):
    levels = {0, *meta.get_permlevel_access(permission, parenttype=parent, user=frappe.session.user)}
    result = {}
    for df in meta.fields:
        if meta.name == "User" and df.fieldname not in {"full_name", "first_name", "last_name", "enabled"}:
            continue
        if (not df.fieldname or df.fieldname.startswith("_") or SECRET_FIELD.search(df.fieldname)
                or df.hidden or df.get("is_virtual") or df.get("mask") or df.get("masked")
                or (df.permlevel or 0) not in levels or df.fieldtype in display_fieldtypes
                or df.fieldtype in {"Password", "Signature", "Attach", "Attach Image"}):
            continue
        if df.fieldtype == "Code" and not (meta.name in {"Dashboard", "Dashboard Chart"} and df.options == "JSON"):
            continue
        if permission == "write" and (df.read_only or df.fieldtype == "Read Only"):
            continue
        result[df.fieldname] = df
    return result


def checked_filters(fields, filters):
    filters = mapping(filters)
    for field, condition in filters.items():
        if field not in fields:
            deny()
        if isinstance(condition, list):
            if len(condition) != 2 or not isinstance(condition[0], str) or condition[0].lower() not in FILTER_OPERATORS:
                invalid()
            operator, value = condition[0].lower(), condition[1]
            if operator in {"in", "not in", "between"}:
                if (not isinstance(value, list) or len(value) > 100 or (operator == "between" and len(value) != 2)
                        or any(not scalar(item) for item in value)):
                    invalid()
            elif not scalar(value) or (operator == "is" and value not in ("set", "not set")):
                invalid()
        elif not scalar(condition):
            invalid()
    return filters


def scalar(value):
    return value is None or isinstance(value, (str, int, float)) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def checked_doc(doctype, name, *, permission="read", lock=False):
    business_meta(doctype)
    doc = frappe.get_doc(doctype, identifier(name), for_update=lock)
    if not can(doctype, "read", doc) or not can(doctype, permission, doc):
        deny()
    return doc


def field_description(df):
    result = {"fieldname": df.fieldname, "label": _(df.label or df.fieldname), "fieldtype": df.fieldtype,
              "required": bool(df.reqd), "read_only": bool(df.read_only), "custom": bool(df.get("is_custom_field"))}
    if df.fieldtype in {"Link", "Dynamic Link", "Select", *table_fields}:
        result["options"] = df.options
    return result


def check_link(target, value):
    if not value:
        return
    name = identifier(value)
    if target == "DocType":
        if not can(business_meta(name).name):
            deny()
        return
    if not isinstance(target, str) or not target:
        invalid()
    if not (can(target, "read", name) or can(target, "select", name)):
        deny()


def apply_values(doc, values, *, parent=None, create=False):
    """Validate all scalar and child fields before applying them to an in-memory document."""
    meta = doc.meta
    fields = visible_fields(meta, "write", parent)
    workflow_field = None
    if not parent and get_workflow_name(meta.name):
        workflow_field = get_workflow(meta.name).workflow_state_field
    for key, value in values.items():
        df = fields.get(key)
        if not df or key == workflow_field or key in STANDARD or key in {"parent", "parenttype", "parentfield", "idx"}:
            deny()
        if not create and df.get("set_only_once") and doc.get(key) not in (None, "", value):
            invalid()
        if df.fieldtype in table_fields:
            if parent:
                invalid()
            apply_rows(doc, df, value, create=create)
        else:
            if not scalar(value):
                invalid()
            if df.fieldtype == "Link":
                check_link(df.options, value)
            doc.set(key, value)
    # Changing a dynamic link's type also changes the meaning of an existing value.
    for df in fields.values():
        if df.fieldtype == "Dynamic Link" and (df.fieldname in values or df.options in values):
            check_link(doc.get(df.options), doc.get(df.fieldname))


def apply_rows(doc, field, rows, *, create):
    if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
        invalid()
    existing = {row.name: row for row in doc.get(field.fieldname) or []}
    patch = any("name" in row for row in rows)
    if create and patch:
        invalid()
    if not patch:
        doc.set(field.fieldname, [])
    seen = set()
    for data in rows:
        data = dict(data)
        row_name = data.pop("name", None)
        delete = data.pop("_delete", False)
        if not isinstance(delete, bool):
            invalid()
        if row_name is None:
            if delete:
                invalid()
            row = doc.append(field.fieldname, {})
        else:
            row_name = identifier(row_name)
            if row_name in seen or row_name not in existing:
                invalid()
            seen.add(row_name)
            row = existing[row_name]
        if delete:
            if data:
                invalid()
            doc.remove(row)
        else:
            apply_values(row, data, parent=doc.doctype, create=row_name is None)

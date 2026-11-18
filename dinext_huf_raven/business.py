"""Native business reads for the employee running the verified Raven event."""

import inspect
import json

import frappe
from frappe.model import table_fields
from frappe.model.workflow import get_transitions, get_workflow_name, has_approval_access

from dinext_huf_raven import business_reports
from dinext_huf_raven.business_fields import (
    STANDARD, allowed_type, bounded_int, business_meta, can, canonical, checked_doc,
    checked_filters, deny, field_description, fingerprint, identifier, invalid, mapping, visible_fields,
)


def _page(limit, start):
    return bounded_int(limit, 1, 100), bounded_int(start, 0, 100000)


def _paged(rows, limit, start):
    return {"rows": rows[:limit], "next_start": start + limit if len(rows) > limit else None}


def _readable_meta(doctype):
    meta = business_meta(doctype)
    if not can(doctype):
        deny()
    return meta


def list_doctypes(query="", limit=20, start=0):
    limit, start = _page(limit, start)
    if not isinstance(query, str) or len(query) > 140:
        invalid()
    rows = []
    for name in sorted(frappe.get_user().get_can_read()):
        if query.casefold() not in name.casefold():
            continue
        meta = frappe.get_meta(name)
        if allowed_type(meta) and can(name):
            rows.append({"name": name, "module": meta.module})
            if len(rows) > start + limit:
                break
    return _paged(rows[start:], limit, start)


def get_doctype_info(doctype):
    meta = _readable_meta(doctype)
    descriptions = []
    for df in visible_fields(meta).values():
        description = field_description(df)
        if df.fieldtype in table_fields:
            child = frappe.get_meta(df.options)
            description["child_fields"] = [
                field_description(item) for item in visible_fields(child, parent=meta.name).values()
                if item.fieldtype not in table_fields
            ]
        descriptions.append(description)
    return {"doctype": doctype, "module": meta.module, "is_submittable": bool(meta.is_submittable),
            "fields": descriptions}


def _select_fields(fields, permitted, defaults):
    if fields is None:
        return list(dict.fromkeys(defaults))
    if not isinstance(fields, list) or not fields or len(fields) > 100 or any(not isinstance(f, str) for f in fields):
        invalid()
    if any(field not in permitted for field in fields):
        deny()
    return list(dict.fromkeys(fields))


def get_document(doctype, name, fields=None):
    doc = checked_doc(doctype, name)
    readable = visible_fields(doc.meta)
    columns = _select_fields(fields, set(STANDARD) | readable.keys(), (*STANDARD, *readable))
    data = {}
    table_counts = {}
    for column in columns:
        df = readable.get(column)
        if df and df.fieldtype in table_fields:
            row_fields = visible_fields(frappe.get_meta(df.options), parent=doctype)
            keys = ["name", "idx", *(k for k, f in row_fields.items() if f.fieldtype not in table_fields)]
            rows = doc.get(column) or []
            data[column] = [{key: row.get(key) for key in keys} for row in rows[:100]]
            table_counts[column] = len(rows)
        else:
            data[column] = doc.get(column)
    return {"doctype": doctype, "document": data, "table_counts": table_counts}


def _search_fields(meta, readable):
    keys = ["name", meta.title_field, *meta.get_search_fields()]
    keys.extend(k for k, df in readable.items() if df.in_global_search or k in {"title", "subject", "description"})
    return list(dict.fromkeys(k for k in keys if k == "name" or (k in readable and readable[k].fieldtype not in table_fields)))[:16]


def _record_list(meta, fields, filters, limit, start, query=None):
    readable = visible_fields(meta)
    scalar_fields = {k for k, df in readable.items() if df.fieldtype not in table_fields} | set(STANDARD)
    columns = _select_fields(fields, scalar_fields, [k for k in ("name", meta.title_field) if k in scalar_fields])
    filters = checked_filters(scalar_fields, filters)
    or_filters = {}
    if query is not None:
        query = identifier(query)
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        or_filters = {field: ["like", "%" + escaped + "%"] for field in _search_fields(meta, readable)}
    # get_list enforces permission-query conditions. Some controllers additionally use has_permission.
    rows = frappe.get_list(meta.name, fields=list(dict.fromkeys(["name", *columns])), filters=filters,
                           or_filters=or_filters, order_by="name asc", limit_start=start, limit_page_length=limit + 1)
    result = [{key: row.get(key) for key in columns} for row in rows[:limit] if can(meta.name, "read", row.name)]
    return {"rows": result, "next_start": start + limit if len(rows) > limit else None}


def list_documents(doctype, fields=None, filters=None, limit=20, start=0):
    limit, start = _page(limit, start)
    return _record_list(_readable_meta(doctype), fields, filters, limit, start)


def search_doctype(doctype, query, limit=20, start=0):
    if doctype == "DocType":
        return list_doctypes(query, limit, start)
    limit, start = _page(limit, start)
    return _record_list(_readable_meta(doctype), None, {}, limit, start, query=query)


def search_documents(query, limit=20, start=0):
    from frappe.desk.doctype.global_search_settings.global_search_settings import get_doctypes_for_global_search

    identifier(query)
    limit, start = _page(limit, start)
    # Search only readable fields. The global index includes raw concatenated contents,
    # including child records, with no field-level visibility information.
    candidates = dict.fromkeys([*get_doctypes_for_global_search(), "User", "Contact", "Customer", "Supplier",
                                "Item", "Company", "Employee", "Task", "Project", "ToDo"])
    allowed = set(frappe.get_user().get_can_read())
    rows = []
    for name in candidates:
        if name not in allowed:
            continue
        meta = frappe.get_meta(name)
        if not allowed_type(meta) or not can(name):
            continue
        page = _record_list(meta, None, {}, start + limit + 1, 0, query=query)
        rows.extend({"doctype": name, **row} for row in page["rows"])
        if len(rows) > start + limit:
            break
    return _paged(rows[start:], limit, start)


def search_link(doctype, query, filters=None, limit=10):
    from frappe.desk.search import search_link as autocomplete

    meta = business_meta(doctype)
    read = can(doctype)
    if not read and not can(doctype, "select"):
        deny()
    limit, _ = _page(limit, 0)
    if not isinstance(query, str) or len(query) > 140:
        invalid()
    readable = visible_fields(meta) if read else {}
    search_fields = _search_fields(meta, readable)
    filters = checked_filters({"name", *(k for k, df in readable.items() if df.fieldtype not in table_fields)}, filters)
    matches = []
    for hit in autocomplete(doctype, query, filters=filters, page_length=limit):
        name = hit.get("value")
        if not isinstance(name, str) or not (can(doctype, "read", name) or can(doctype, "select", name)):
            continue
        values = [name]
        if read and can(doctype, "read", name):
            doc = frappe.get_doc(doctype, name)
            values += [str(doc.get(key) or "") for key in search_fields if key != "name"]
        if not query or any(query.casefold() in v.casefold() for v in values):
            matches.append({"value": name, "label": next((v for v in values[1:] if v), name)})
    return {"rows": matches}


def transitions_for(doc):
    if not get_workflow_name(doc.doctype):
        return []
    return [t["action"] for t in get_transitions(doc) if has_approval_access(frappe.session.user, doc, t)]


def get_workflow_actions(doctype, name):
    return {"actions": transitions_for(checked_doc(doctype, name))}


def get_pending_approvals(doctype=None, limit=20, start=0):
    limit, start = _page(limit, start)
    filters = {"status": "Open"}
    if doctype:
        filters["reference_doctype"] = _readable_meta(doctype).name
    rows = frappe.get_list("Workflow Action", filters=filters, fields=["name", "reference_doctype", "reference_name"],
                           order_by="creation desc, name asc", limit_start=start, limit_page_length=limit + 1)
    result = []
    for row in rows[:limit]:
        dt, name = row.reference_doctype, row.reference_name
        if not allowed_type(frappe.get_meta(dt)) or not can(dt, "read", name):
            continue
        actions = transitions_for(frappe.get_doc(dt, name))
        if actions:
            result.append({"doctype": dt, "name": name, "actions": actions})
    return {"rows": result, "next_start": start + limit if len(rows) > limit else None}


def list_user_dashboards(query="", limit=20, start=0):
    limit, start = _page(limit, start)
    dashboards = _record_list(_readable_meta("Dashboard"), ["name"], {}, limit, start, query=query or None)
    for row in dashboards["rows"]:
        doc = checked_doc("Dashboard", row["name"])
        fields = visible_fields(doc.meta)
        visible = "charts" in fields and "chart" in visible_fields(frappe.get_meta("Dashboard Chart Link"), parent=doc.doctype)
        row["charts"] = [c.chart for c in doc.charts[:100] if can("Dashboard Chart", "read", c.chart)] if visible else []
    return dashboards


READS = {func.__name__: func for func in (
    list_doctypes, get_doctype_info, get_document, list_documents, search_doctype, search_documents,
    search_link, get_workflow_actions, get_pending_approvals, list_user_dashboards,
    business_reports.report_list, business_reports.report_requirements, business_reports.generate_report,
)}


def _run(operation, parameters, *, replay):
    func = READS.get(identifier(operation))
    if not func:
        invalid()
    params = mapping(parameters).copy()
    if operation == "generate_report":
        if "replay" in params:
            invalid()
        params["replay"] = replay
    try:
        inspect.signature(func).bind(**params)
    except TypeError:
        invalid()
    return func(**params)


def query(operation, parameters=None, **runtime):
    from dinext_huf_raven import bridge

    bridge.current_event()
    if frappe.session.user in (None, "Guest", "Administrator"):
        deny()
    params = mapping(parameters)
    result = _run(operation, params, replay=False)
    bridge.track_business_read({"operation": operation, "parameters": params, "fingerprint": fingerprint(result)})
    if operation == "generate_report" and params.get("format", "json") != "json" and result["status"] == "Completed":
        file_doc = business_reports.export_report(result, params["format"])
        bridge.add_artifact(file_doc)
        return {"status": "Completed", "file_name": file_doc.file_name, "file_id": file_doc.name,
                "rows": len(result["rows"]), "next_start": result["next_start"]}
    return json.loads(canonical(result))


def recheck_disclosure(check):
    """Repeat an authorized read without creating jobs or files. Changed output cannot be replayed."""
    if frappe.session.user in (None, "Guest", "Administrator"):
        return False
    try:
        if check.get("kind") in {"proposal", "receipt"}:
            from dinext_huf_raven.actions import recheck_action

            return recheck_action(check)
        actual = _run(check["operation"], check["parameters"], replay=True)
        return fingerprint(actual) == check["fingerprint"]
    except (frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError, TypeError, ValueError, KeyError):
        return False

"""Discover and execute the site's reports with the current employee's permissions."""

import ast
import csv
import io
import re

import frappe
from frappe import _
from frappe.desk import query_report
from frappe.model import table_fields

from dinext_huf_raven.business_fields import (
    SECRET_FIELD, bounded_int, business_meta, can, check_link, deny, identifier, invalid,
    mapping, scalar, visible_fields,
)


def _report(name):
    name = identifier(name)
    own = frappe.get_doc("Report", name)
    if own.disabled or not own.is_permitted():
        deny()
    report = query_report.get_report_doc(name)
    business_meta(report.ref_doctype)
    if not can(report.ref_doctype, "read") or not can(report.ref_doctype, "report"):
        deny()
    return report


def report_list(query="", module=None, report_type=None, limit=20, start=0):
    from frappe.boot import get_allowed_reports
    from dinext_huf_raven.business_fields import allowed_type

    bounded_int(limit, 1, 100)
    bounded_int(start, 0, 100000)
    if not isinstance(query, str) or len(query) > 140:
        invalid()
    rows = []
    for name, info in sorted(get_allowed_reports().items()):
        if (query.casefold() not in name.casefold() or module and module != info.get("module")):
            continue
        own = frappe.get_doc("Report", name)
        if (own.disabled or not own.is_permitted() or not allowed_type(frappe.get_meta(own.ref_doctype))
                or not can(own.ref_doctype) or not can(own.ref_doctype, "report")
                or report_type and report_type != own.report_type):
            continue
        if own.report_type == "Custom Report":
            try:
                _report(name)
            except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError):
                continue
        rows.append({"name": name, "module": own.module, "report_type": own.report_type, "doctype": own.ref_doctype})
        if len(rows) > start + limit:
            break
    return {"rows": rows[start:start + limit], "next_start": start + limit if len(rows) > start + limit else None}


_JS_TOKEN = re.compile(r"""//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|""" +
                       r"\x60(?:\\.|[^\x60\\])*\x60|[\w$]+|[^\s]")


def _literal(token):
    if token[:1] in {"'", '"', chr(96)}:
        try:
            return ast.literal_eval(token) if token[0] != chr(96) else token[1:-1]
        except (ValueError, SyntaxError):
            return None
    return {"true": True, "false": False, "1": 1, "0": 0}.get(token)


def script_filters(script):
    """Read literal filter properties from Desk's JS metadata without running report code."""
    tokens = [t for t in _JS_TOKEN.findall(script[:500000]) if not t.startswith(("//", "/*"))]
    start = next((i + 3 for i in range(len(tokens) - 2)
                  if tokens[i].strip("'\"") == "filters" and tokens[i + 1] in (":", "=") and tokens[i + 2] == "["), None)
    if start is None:
        return []
    filters, field, stack = [], {}, ["["]
    for index in range(start, len(tokens)):
        t = tokens[index]
        if t in ("[", "{", "("):
            stack.append(t)
            if stack == ["[", "{"]:
                field = {}
            continue
        if t in ("]", "}", ")"):
            if t == "}" and stack == ["[", "{"] and field.get("fieldname"):
                filters.append(field)
            stack.pop()
            if not stack:
                break
            continue
        key = t.strip("'\"")
        if stack != ["[", "{"] or key not in {"fieldname", "label", "fieldtype", "options", "reqd", "default"}:
            continue
        if tokens[index + 1:index + 2] != [":"]:
            continue
        value_index = index + 2
        if tokens[value_index:value_index + 2] == ["__", "("]:
            value_index += 2
        if value_index >= len(tokens):
            continue
        value = _literal(tokens[value_index])
        if key == "options" and tokens[value_index] == "[":
            opts = []
            for option in tokens[value_index + 1:]:
                if option == "]":
                    break
                item = _literal(option)
                if isinstance(item, str):
                    opts.append(item)
            value = opts
        if value is not None:
            field[key] = value
    return filters


def _declarations(report, name):
    fields = {}
    for df in script_filters(query_report.get_script(name)["script"] or ""):
        fields[df["fieldname"]] = df
    for df in report.get("filters") or []:
        fields[df.fieldname] = {k: df.get(k) for k in ("fieldname", "label", "fieldtype", "reqd", "options", "default")}
    for key in re.findall(r"%\(([A-Za-z_]\w*)\)s", report.get("query") or ""):
        fields.setdefault(key, {"fieldname": key, "fieldtype": "Data", "reqd": 1})
    return {key: df for key, df in fields.items() if not key.startswith("_") and not SECRET_FIELD.search(key)
            and df.get("fieldtype") not in {"Password", "Code"}}


def _columns(columns, meta):
    permitted = visible_fields(meta)
    result = []
    for value in columns or []:
        if not isinstance(value, (str, dict)):
            value = value.as_dict()
        column = query_report.get_column_as_dict(value)
        key = column.get("fieldname")
        if (not key or key.startswith("_") or SECRET_FIELD.search(key) or column.get("hidden")
                or column.get("fieldtype") in {"Password", "Signature", "Attach", "Attach Image", *table_fields}
                or meta.get_field(key) and key not in permitted):
            continue
        result.append({k: column[k] for k in ("fieldname", "label", "fieldtype", "options") if column.get(k)})
    return result


def report_requirements(report_name):
    report = _report(report_name)
    fields = _declarations(report, report_name)
    return {"name": report_name, "doctype": report.ref_doctype, "report_type": report.report_type,
            "prepared_report": bool(report.prepared_report), "filters": list(fields.values()),
            "required": [k for k, df in fields.items() if df.get("reqd")],
            "columns": _columns(report.get("columns"), frappe.get_meta(report.ref_doctype))}


def _filters(report, name, values):
    filters = mapping(values)
    declared = _declarations(report, name)
    missing = [key for key, df in declared.items() if df.get("reqd") and filters.get(key) in (None, "", [])]
    if missing:
        frappe.throw(_("Supply the required report filters: {0}.").format(", ".join(missing)))
    for key, value in filters.items():
        if key not in declared or key in {"prepared_report_name", "ignore_permissions", "ignore_prepared_report"}:
            invalid()
        df = declared[key]
        values = value if isinstance(value, list) else [value]
        if len(values) > 100 or any(not scalar(item) for item in values):
            invalid()
        if df.get("fieldtype") in {"Link", "MultiSelectList"} and isinstance(df.get("options"), str):
            for item in values:
                check_link(df["options"], item)
    return filters


def generate_report(report_name, filters=None, limit=50, start=0, format="json", *, replay=False):
    from frappe.core.doctype.prepared_report.prepared_report import (
        get_reports_in_queued_state, make_prepared_report,
    )

    bounded_int(limit, 1, 1000 if format in ("csv", "xlsx", "excel") else 100)
    bounded_int(start, 0, 100000)
    if format not in {"json", "csv", "xlsx", "excel"}:
        invalid()
    report = _report(report_name)
    if format != "json" and not can(report.ref_doctype, "export"):
        deny()
    filters = _filters(report, report_name, filters)
    if report.report_type == "Report Builder":
        columns, values = report.get_data(filters=filters, limit=start + limit + 1, as_dict=True)
        result = {"columns": columns, "result": values}
    else:
        result = query_report.run(report_name, filters=filters.copy(), are_default_filters=False)
        if result.get("prepared_report"):
            if not result.get("doc"):
                queued = get_reports_in_queued_state(report_name, filters)
                if not queued and not replay:
                    make_prepared_report(report_name, filters)
                return {"report_name": report_name, "status": "Queued", "rows": [], "columns": [], "next_start": None}
            # Prepared Reports are result-file snapshots. Refresh under today's permissions
            # before any cached aggregate or row is sent to the employee or model.
            result = query_report.run(report_name, filters=filters.copy(), ignore_prepared_report=True, are_default_filters=False)
    columns = _columns(result.get("columns"), frappe.get_meta(report.ref_doctype))
    data = result.get("result") or []
    rows = []
    for row in data[start:start + limit]:
        if not isinstance(row, dict):
            invalid()
        blocked = any(col.get("fieldtype") == "Link" and col.get("options") == report.ref_doctype
                      and row.get(col["fieldname"]) and not can(report.ref_doctype, "read", row[col["fieldname"]])
                      for col in columns)
        if not blocked:
            rows.append({col["fieldname"]: row.get(col["fieldname"]) for col in columns})
    return {"report_name": report_name, "status": "Completed", "columns": columns, "rows": rows,
            "next_start": start + limit if len(data) > start + limit else None}


def export_report(result, format):
    from openpyxl import Workbook

    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
            return "'" + value
        return value

    columns = result["columns"]
    values = [[safe(col.get("label") or col["fieldname"]) for col in columns]]
    values.extend([safe(row.get(col["fieldname"])) for col in columns] for row in result["rows"])
    if format == "csv":
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows(values)
        content = buffer.getvalue().encode("utf-8-sig")
        extension = "csv"
    else:
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Report")
        for row in values:
            sheet.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        content = buffer.getvalue()
        extension = "xlsx"
    filename = f"report-{frappe.generate_hash(length=16)}.{extension}"
    return frappe.get_doc({"doctype": "File", "file_name": filename, "is_private": 1, "content": content}).insert()

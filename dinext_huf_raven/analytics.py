"""Analyze explicitly selected Frappe records, local files and visible Huf instructions."""

import hashlib
import io
import json
import math
import os
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path

import frappe
from frappe import _

from dinext_huf_raven import bridge, business, tabular
from dinext_huf_raven.business_fields import bounded_int, can, checked_doc, deny, fingerprint, identifier, invalid, mapping
from dinext_huf_raven.file_extractors import MAX_BYTES
from dinext_huf_raven.raven_context import local_file_url


def _event():
    event = bridge.current_event()
    if not event.requester_user or frappe.session.user != event.requester_user or event.requester_user in {"Guest", "Administrator"}:
        deny()
    return event


def _project(sources):
    sources = mapping(sources)
    if not 1 <= len(sources) <= 4:
        invalid()
    tables, scope = {}, {}
    budget = tabular.MAX_ROWS
    for table, source in sources.items():
        source = mapping(source)
        if not tabular.IDENTIFIER.fullmatch(table) or table.lower().startswith("sqlite_"):
            invalid()
        if source.keys() - {"doctype", "fields", "filters", "limit", "start"}:
            invalid()
        dt = identifier(source.get("doctype"))
        columns = source.get("fields")
        if (not isinstance(columns, list) or not 1 <= len(columns) <= tabular.MAX_COLUMNS
                or not all(isinstance(c, str) and tabular.IDENTIFIER.fullmatch(c) for c in columns)
                or len(set(c.casefold() for c in columns)) != len(columns)):
            invalid()
        count = bounded_int(source.get("limit", 100), 1, budget)
        budget -= count
        offset = bounded_int(source.get("start", 0), 0, 100000)
        filters = mapping(source.get("filters"))
        rows = []
        for consumed in range(0, count, 100):
            page = business.query("list_documents", {"doctype": dt, "fields": columns, "filters": filters,
                                  "start": offset, "limit": min(100, count - consumed)})
            rows.extend({k: tabular.scalar(row.get(k)) for k in columns} for row in page["rows"])
            offset = page["next_start"]
            if offset is None:
                break
        tables[table] = (columns, rows)
        scope[table] = {"rows": len(rows), "next_start": offset}
    return tables, scope


def _summary(tables):
    result = {}
    for table, (columns, rows) in tables.items():
        stats = {}
        for name in columns:
            values = [r[name] for r in rows if r[name] is not None and r[name] != ""]
            data = {"count": len(values), "missing": len(rows) - len(values)}
            if values and all(type(v) in (int, float) and math.isfinite(v) for v in values):
                data.update(min=min(values), max=max(values), sum=sum(values), mean=sum(values) / len(values))
            else:
                counts = Counter(values)
                data.update(distinct=len(counts), most_common=counts.most_common(5))
            stats[name] = data
        result[table] = stats
    return result


def _chart(tables, expression):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    config = mapping(expression)
    if config.keys() - {"table", "x", "y", "kind", "title"}:
        invalid()
    table, x, y = (identifier(config.get(key)) for key in ("table", "x", "y"))
    kind = config.get("kind", "bar")
    title = identifier(config["title"]) if config.get("title") else y
    if table not in tables or kind not in {"bar", "line", "scatter"}:
        invalid()
    columns, rows = tables[table]
    if x not in columns or y not in columns or not 1 <= len(rows) <= 60:
        invalid()
    values = [row[y] for row in rows]
    if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
        invalid()
    labels = [str(row[x])[:36] for row in rows]
    positions = range(len(labels))
    figure = Figure(figsize=(8, 4), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    getattr(axes, kind)(positions, values)
    step = max(1, (len(labels) + 9) // 10)
    axes.set_xticks(positions[::step], labels[::step], rotation=30, ha="right", parse_math=False)
    axes.set_title(title, parse_math=False)
    axes.set_xlabel(x, parse_math=False)
    axes.set_ylabel(y, parse_math=False)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=135)
    doc = frappe.get_doc({"doctype": "File", "file_name": f"chart-{frappe.generate_hash(length=16)}.png",
                          "is_private": 1, "content": buffer.getvalue()}).insert()
    bridge.add_artifact(doc)
    return {"file_id": doc.name, "file_name": doc.file_name, "rows": len(rows)}


def analyze(mode, sources, expression=None, **runtime):
    _event()
    if mode not in {"sql", "python", "summary", "chart"}:
        invalid()
    tables, scope = _project(sources)
    if mode == "sql":
        result = tabular.sql(tables, expression)
    elif mode == "python":
        result = tabular.python(tables, expression)
    elif mode == "chart":
        result = _chart(tables, expression)
    else:
        result = _summary(tables)
    return {"mode": mode, "scope": scope, "result": result}


def _file_bytes(event, file_id):
    doc = frappe.get_doc("File", identifier(file_id))
    if doc.is_folder or not local_file_url(doc.file_url) or not doc.has_permission("read"):
        deny()
    if doc.attached_to_doctype == "Raven Message":
        if doc.name not in {row["file_id"] for row in bridge.authorize_event(event).files}:
            deny()
    elif doc.attached_to_doctype:
        target = checked_doc(doc.attached_to_doctype, doc.attached_to_name)
        if doc.attached_to_field:
            df = target.meta.get_field(doc.attached_to_field)
            levels = {0, *target.meta.get_permlevel_access("read", user=event.requester_user)}
            if (not df or df.hidden or df.get("mask") or df.get("masked") or df.permlevel not in levels
                    or df.fieldtype not in {"Attach", "Attach Image"} or target.get(df.fieldname) != doc.file_url):
                deny()
    elif doc.owner != event.requester_user:
        deny()
    private = doc.file_url.startswith("/private/files/")
    if private != bool(doc.is_private):
        deny()
    root = Path(frappe.get_site_path("private" if private else "public", "files")).resolve()
    path = Path(doc.get_full_path()).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        deny()
    with path.open("rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            deny()
        content = source.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        invalid()
    signature = fingerprint([doc.name, doc.file_name, doc.attached_to_doctype, doc.attached_to_name,
                             doc.attached_to_field, private, hashlib.sha256(content).hexdigest()])
    return doc, content, signature


def _parse_file(extension, content, operation):
    try:
        command = [sys.executable, "-I", str(Path(__file__).with_name("file_extractors.py")), extension, operation]
        output = subprocess.run(command, input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=22, check=True, cwd="/", env={
                                    "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                                    "OPENBLAS_NUM_THREADS": "1", "OMP_THREAD_LIMIT": "1",
                                }).stdout
        return json.loads(output)
    except (subprocess.SubprocessError, ValueError):
        frappe.throw(_("This file format, size or content could not be processed."))


def extract_file(file_id, operation="extract", **runtime):
    event = _event()
    doc, content, signature = _file_bytes(event, file_id)
    extension = Path(doc.file_name).suffix.lower().lstrip(".")
    result = _parse_file(extension, content, identifier(operation))
    bridge.track_business_read({"module": "analytics", "kind": "file", "file_id": doc.name, "fingerprint": signature})
    return {"file_id": doc.name, "file_name": doc.file_name, **result}


def _template(body, parameters):
    """Only plain variables and conditional blocks have meaning in an employee prompt."""
    from jinja2 import Environment, nodes
    from jinja2.exceptions import TemplateSyntaxError

    try:
        tree = Environment().parse(body)
    except TemplateSyntaxError:
        if parameters:
            invalid()
        return {"text": body, "parameters": []}
    keys = sorted({n.name for n in tree.find_all(nodes.Name)})
    if not parameters:
        return {"text": body, "parameters": keys}
    params = mapping(parameters)
    if set(params) - set(keys) or any(type(v) not in (bool, float, int, str) for v in params.values()):
        invalid()
    def render(items, depth=0):
        if depth > 12:
            invalid()
        out = []
        for node in items:
            if isinstance(node, nodes.TemplateData):
                out.append(node.data)
            elif isinstance(node, nodes.Name) and node.name in params:
                out.append(str(params[node.name]))
            elif isinstance(node, nodes.Output):
                out.append(render(node.nodes, depth + 1))
            elif isinstance(node, nodes.If) and isinstance(node.test, nodes.Name) and not node.elif_:
                out.append(render(node.body if params.get(node.test.name) else node.else_, depth + 1))
            else:
                invalid()
        text = "".join(out)
        if len(text) > 32000:
            invalid()
        return text
    return {"text": render(tree.body), "parameters": keys}


def _instructions(kind, name=None, query=None, parameters=None):
    types = {"skill": "Skill", "prompt": "Agent Prompt"}
    if kind not in types:
        invalid()
    dt = types[kind]
    query = identifier(query) if query else ""
    if parameters and (not name or kind != "prompt"):
        invalid()
    filters = {"status": "Active"} if kind == "skill" else {"is_active": 1, "is_latest": 1}
    if name:
        filters["name"] = identifier(name)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters["title"] = ["like", "%" + escaped + "%"]
    visible = {}
    if kind == "prompt":
        visible = {"visibility": ["in", ["Public", "App"]], "owner": frappe.session.user}
    entries = frappe.get_list(dt, filters=filters, or_filters=visible, fields=["name"], order_by="name asc",
                              limit_page_length=25)
    result = []
    for row in entries:
        doc = frappe.get_doc(dt, row.name)
        if not can(dt, "read", doc):
            continue
        if kind == "prompt" and doc.visibility not in {"Public", "App"} and doc.owner != frappe.session.user:
            continue
        levels = {0, *doc.meta.get_permlevel_access("read", user=frappe.session.user)}
        entry = {"name": doc.name}
        for key in ("title", "description", "version", "instructions" if kind == "skill" else "prompt_body"):
            df = doc.meta.get_field(key)
            if not df or df.hidden or df.get("mask") or (df.permlevel or 0) not in levels:
                continue
            value = doc.get(key)
            if key in {"instructions", "prompt_body"}:
                if name:
                    body = str(value or "")[:32000]
                    entry.update(_template(body, parameters) if kind == "prompt" else {"text": body})
            else:
                entry[key] = value
        result.append(entry)
    if name and not result:
        deny()
    return {"kind": kind, "items": result}


def instructions(kind, name=None, query=None, parameters=None, **runtime):
    _event()
    args = {"kind": kind, "name": name, "query": query, "parameters": parameters}
    result = _instructions(**args)
    bridge.track_business_read({"module": "analytics", "kind": "instructions", "args": args,
                                "fingerprint": fingerprint(result)})
    return result


def recheck_disclosure(check):
    try:
        event = _event()
        if check.get("kind") == "file":
            _, _, digest = _file_bytes(event, check["file_id"])
        elif check.get("kind") == "instructions":
            digest = fingerprint(_instructions(**check["args"]))
        else:
            return False
        return digest == check["fingerprint"]
    except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError, OSError, ValueError, TypeError, KeyError):
        return False

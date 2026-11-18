"""Tools that can run under an authenticated Raven event."""


def tool(name, path, description, parameters):
    return {
        "tool_name": "dinext_" + name,
        "category": "Dinext Raven",
        "function_path": "dinext_huf_raven." + path,
        "description": description,
        "parameters": [
            {"fieldname": key, "label": key.replace("_", " ").title(), "type": kind,
             "required": int(required), "description": help_text}
            for key, kind, required, help_text in parameters
        ],
    }


TOOLS = [
    tool("query", "business.query",
         "Read Dinext ERP data as the employee. Available operations: list_doctypes, get_doctype_info, get_document, list_documents, search_doctype, search_documents, search_link, report_list, report_requirements, generate_report, get_pending_approvals, get_workflow_actions, list_user_dashboards.",
         [("operation", "string", True, "Operation name"),
          ("parameters", "object", False, "Named parameters, for example {doctype, name}, {doctype, fields, filters}, {query}, or {report_name, filters}.")]),
    tool("propose", "actions.propose",
         "Propose a create, update, delete, submit, cancel or workflow action. The employee confirms in Raven. Use this for Dashboard and Dashboard Chart writes.",
         [("operation", "string", True, "create, update, delete, submit, cancel or workflow"),
          ("doctype", "string", True, "Exact DocType"), ("name", "string", False, "Existing record ID"),
          ("values", "object", False, "Requested field values"),
          ("workflow_action", "string", False, "Allowed transition label")]),
    tool("analyze", "analytics.analyze",
         "Analyze employee-readable data. Tables are local snapshots with explicit row limits. A chart is delivered as a private PNG.",
         [("mode", "string", True, "sql, python, summary or chart"),
          ("sources", "object", True, 'Table names mapped to {doctype, fields, filters, start, limit}. Name the main table records.'),
          ("expression", "string", False, 'SQL SELECT, a Python DataFrame expression, or chart JSON with table, x, y, kind (bar, line or scatter), and title.')]),
    tool("extract_file", "analytics.extract_file",
         "Extract readable text or tabular data from a Raven attachment or an accessible ERP document's local file.",
         [("file_id", "string", True, "Frappe File record ID"),
          ("operation", "string", False, "extract, ocr, parse_data or extract_tables")]),
    tool("instructions", "analytics.instructions",
         "Find or read reusable Huf skills and prompt templates visible to the employee.",
         [("kind", "string", True, "skill or prompt"), ("name", "string", False, "Exact returned record ID"),
          ("query", "string", False, "Title search"), ("parameters", "object", False, "Named template substitutions")]),
]

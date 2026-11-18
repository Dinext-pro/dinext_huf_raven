import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import frappe

from dinext_huf_raven import actions, bridge, business, business_fields as fields, business_reports as reports


class Row(frappe._dict):
    def set(self, key, value):
        self[key] = value


def field(name, fieldtype="Data", **flags):
    return frappe._dict(fieldname=name, fieldtype=fieldtype, label=name, **flags)


def meta(name, *columns):
    return frappe._dict(name=name, fields=columns, get_permlevel_access=lambda *args, **kwargs: [0],
                        get_search_fields=lambda: ["name"], get_field=lambda key: next(
                            (df for df in columns if df.fieldname == key), None))


class TestBusiness(unittest.TestCase):
    def setUp(self):
        self.patches = self.enterContext(ExitStack())
        self.user = "employee@example.test"
        self.date = datetime(2026, 1, 15, 10)
        self.patches.enter_context(patch.object(frappe, "session", frappe._dict(user=self.user)))
        self.patches.enter_context(patch.object(frappe, "local", frappe._dict(site="test.local")))
        for mod in (fields, reports, actions):
            self.patches.enter_context(patch.object(mod, "_", lambda msg: msg))
        self.patches.enter_context(patch.object(actions, "now_datetime", return_value=self.date))
        self.patches.enter_context(patch.object(frappe, "throw", side_effect=self.throw))

    @staticmethod
    def throw(message, exc=frappe.ValidationError, **kwargs):
        raise exc(message)

    def test_metadata_including_child_uses_parent_permlevels(self):
        child = meta("Lines", field("qty"), field("private_note", permlevel=2), field("access_token"),
                     field("pin", "Password"), field("settlement", hidden=1))
        parent = meta("Invoice", field("subject"), field("rows", "Table", options="Lines"), field("internal", hidden=1))
        with patch.object(business, "_readable_meta", return_value=parent), patch.object(frappe, "get_meta", return_value=child):
            entries = business.get_doctype_info("Invoice")["fields"]
        self.assertEqual([entry["fieldname"] for entry in entries], ["subject", "rows"])
        self.assertEqual([entry["fieldname"] for entry in entries[1]["child_fields"]], ["qty"])

    def test_filters_and_requested_columns_cannot_reference_protected_fields(self):
        for value in ({"api_key": "secret"}, {"tabOther.name": ["like", "%"]}):
            with self.assertRaises(frappe.PermissionError):
                fields.checked_filters({"name", "subject"}, value)
        with self.assertRaises(frappe.PermissionError):
            business._select_fields(["*"], {"name", "subject"}, ["name"])
        with self.assertRaises(frappe.ValidationError):
            fields.checked_filters({"name"}, {"name": ["in", [{"where": "x"}]]})

    def test_list_checks_the_actual_record_and_uses_native_query_permissions(self):
        dt = meta("Work", field("subject"))
        with patch.object(business, "_readable_meta", return_value=dt), patch.object(frappe, "get_list", return_value=[
            frappe._dict(name="mine", subject="visible"), frappe._dict(name="theirs", subject="private"),
        ]) as fetch, patch.object(business, "can", side_effect=lambda dt, p, name: name == "mine"):
            got = business.list_documents("Work", ["name", "subject"], filters={"subject": ["like", "v%"]})
        self.assertEqual(got["rows"], [{"name": "mine", "subject": "visible"}])
        self.assertNotIn("ignore_permissions", fetch.call_args.kwargs)

    def test_child_patch_retains_unmentioned_rows_and_rejects_other_parent_rows(self):
        child = meta("Line", field("qty", "Float"), field("password", "Password"))
        row, kept = Row(name="r1", qty=1, meta=child), Row(name="r2", qty=3, meta=child)
        parent = Row(doctype="Order", items=[row, kept])
        df = field("items", "Table", options="Line")
        fields.apply_rows(parent, df, [{"name": "r1", "qty": 5}], create=False)
        self.assertEqual([r.qty for r in parent.get("items")], [5, 3])
        with self.assertRaises(frappe.ValidationError):
            fields.apply_rows(parent, df, [{"name": "r3", "qty": 1}], create=False)
        with self.assertRaises(frappe.PermissionError):
            fields.apply_rows(parent, df, [{"name": "r1", "password": "x"}], create=False)

    def test_script_report_static_filter_declarations(self):
        js = """frappe.query_reports['Test'] = {filters: [
          {fieldname: 'company', label: __('Company'), fieldtype: 'Link', options: 'Company', reqd: 1},
          {'fieldname': 'status', fieldtype: 'Select', options:['Open','Closed'],
            get_query: function(){ return {filters: {disabled: 0}}; }},
        ]};"""
        result = reports.script_filters(js)
        self.assertEqual([row["fieldname"] for row in result], ["company", "status"])
        self.assertEqual(result[1]["options"], ["Open", "Closed"])
        self.assertTrue(result[0]["reqd"])

    def test_report_export_escapes_spreadsheet_formulas_and_uses_requesters_file(self):
        doc = Mock()
        with patch.object(frappe, "generate_hash", return_value="example"), patch.object(frappe, "get_doc", return_value=doc) as create:
            reports.export_report({"rows": [{"memo": "=7+1"}], "columns": [{"label": "Memo", "fieldname": "memo"}]}, "csv")
        values = create.call_args.args[0]
        self.assertIn("'=7+1", values["content"].decode("utf-8-sig"))
        self.assertEqual(values["is_private"], 1)
        self.assertNotIn("attached_to_doctype", values)
        self.assertNotIn("owner", values)
        doc.insert.assert_called_once_with()

    def test_disclosure_repeats_the_read_and_honors_revocation_or_changed_contents(self):
        check = {"operation": "get_document", "parameters": {}, "fingerprint": fields.fingerprint({"data": "a"})}
        with patch.object(business, "_run", return_value={"data": "a"}) as read:
            self.assertTrue(business.recheck_disclosure(check))
            self.assertTrue(read.call_args.kwargs["replay"])
            read.return_value = {"data": "b"}
            self.assertFalse(business.recheck_disclosure(check))
            read.side_effect = frappe.PermissionError
            self.assertFalse(business.recheck_disclosure(check))

    def decision_fixture(self, *, status="Pending"):
        payload = {"operation": "update", "doctype": "ToDo", "name": "todo", "values": {"description": "step"}, "workflow_action": None}
        key = "1" * 32
        self.original = frappe._dict(name="source", doctype="Dinext AI Event", channel_id="shared",
                                     raven_bot="Bot", requester_user=self.user, status="Delivered", reply_message="reply",
                                     response=key + "\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        self.command = frappe._dict(name="cmd", doctype="Dinext AI Event", requester_user=self.user, raven_bot="Bot")
        self.action = frappe._dict(name=key, requester_user=self.user, source_event="source", channel_id="shared",
                                   agent_run="run", target_version="version", status=status, modified=self.date,
                                   expires_at=self.date + timedelta(minutes=15), payload_json=fields.canonical(payload))
        self.action.payload_hash = actions._digest(self.action)
        self.db = Mock()
        self.db.exists.return_value = True
        self.patches.enter_context(patch.object(frappe, "db", self.db))
        self.patches.enter_context(patch.object(frappe, "get_doc",
            side_effect=lambda dt, *args, **kwargs: self.action if dt == actions.ACTION else self.original))
        self.route = self.patches.enter_context(patch.object(bridge, "authorize_event", return_value=frappe._dict(direct=True)))
        self.patches.enter_context(patch.object(bridge, "track_business_read"))
        self.prepare = self.patches.enter_context(patch.object(actions, "_prepare", return_value=(object(), "version")))
        self.execute = self.patches.enter_context(patch.object(actions, "_execute", return_value={"doctype": "ToDo", "name": "todo"}))
        return key

    def test_confirm_replays_the_receipt_and_applies_once(self):
        key = self.decision_fixture()
        first = actions.handle_command(self.command, "подтвердить " + key)
        self.assertEqual(self.action.status, "Applied")
        self.assertEqual(first, actions.handle_command(self.command, "confirm " + key))
        self.execute.assert_called_once()
        self.assertTrue(self.prepare.call_args.kwargs["lock"])
        self.assertEqual(self.prepare.call_args.kwargs["expected"], "version")

    def test_command_requires_the_issuer_bot_dm_and_immutable_payload(self):
        key = self.decision_fixture()
        self.assertIsNone(actions.handle_command(self.command, "maybe confirm " + key))
        self.route.return_value.direct = False
        with self.assertRaises(frappe.PermissionError):
            actions.handle_command(self.command, "confirm " + key)
        self.route.return_value.direct = True
        self.command.requester_user = "second@example.test"
        with self.assertRaises(frappe.PermissionError):
            actions.handle_command(self.command, "confirm " + key)
        self.command.requester_user = self.user
        self.action.payload_json += " "
        with self.assertRaises(frappe.PermissionError):
            actions.handle_command(self.command, "confirm " + key)
        self.execute.assert_not_called()

    def test_rejection_and_expiry_do_not_apply(self):
        key = self.decision_fixture()
        actions.handle_command(self.command, "reject " + key)
        self.assertEqual(self.action.status, "Rejected")
        actions.handle_command(self.command, "confirm " + key)
        self.action.status = "Pending"
        self.action.expires_at = self.date
        self.action.payload_hash = actions._digest(self.action)
        actions.handle_command(self.command, "confirm " + key)
        self.assertEqual(self.action.status, "Expired")
        self.execute.assert_not_called()

    def test_changed_version_or_revoked_permissions_do_not_apply(self):
        key = self.decision_fixture()
        for error, state in ((actions.StaleProposal(), "Stale"), (frappe.PermissionError(), "Failed")):
            self.action.status = "Pending"
            self.prepare.side_effect = error
            actions.handle_command(self.command, "confirm " + key)
            self.assertEqual(self.action.status, state)
        self.execute.assert_not_called()

    def test_uncertain_controller_or_interrupted_worker_cannot_reapply(self):
        key = self.decision_fixture()
        self.execute.side_effect = RuntimeError("outcome unavailable")
        actions.handle_command(self.command, "confirm " + key)
        self.assertEqual(self.action.status, "Uncertain")
        actions.handle_command(self.command, "confirm " + key)
        self.action.status = "Applying"
        self.action.modified = self.date - timedelta(hours=1)
        actions.handle_command(self.command, "confirm " + key)
        self.assertEqual(self.action.status, "Uncertain")
        self.execute.assert_called_once()

    def test_unshown_canonical_offer_cannot_be_applied(self):
        key = self.decision_fixture()
        self.original.response = "confirm " + key
        with self.assertRaises(frappe.PermissionError):
            actions.handle_command(self.command, "confirm " + key)
        self.execute.assert_not_called()

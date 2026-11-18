"""Run native tools through Huf's dispatcher and real Raven DM commands on localhost."""

import json
from unittest.mock import patch

import frappe

from dinext_huf_raven import actions, bridge, business
from dinext_huf_raven.tests import acceptance


def _offer(state, args, *, expect_denied=False):
    from huf.ai.providers import litellm, test_provider

    original = test_provider.run
    dispatched = []

    async def choose_tool(agent, enhanced_prompt, provider, model, context=None):
        name = "dinext_propose"
        tool = next(t for t in agent.tools if t.name == name)
        call_id = frappe.generate_hash(length=20)
        output = await litellm._execute_tool_call(tool, json.dumps(args), context=context, tool_call_id=call_id)
        dispatched.append(str(output))
        result = await original(agent, enhanced_prompt, provider, model, context=context)
        result.new_items = [test_provider._tool_call_item(name, args, call_id),
                            test_provider._tool_call_output_item(name, str(output), call_id)]
        return result

    with patch.object(test_provider, "run", side_effect=choose_tool):
        source = acceptance._send(state, state.users[0], state.dms[0], acceptance.MARKER + " propose")
        event = acceptance._complete(source)
    rows = frappe.get_all(actions.ACTION, filters={"source_event": event.name}, pluck="name")
    if expect_denied:
        assert event.status == "Delivered" and dispatched and not rows
        return None
    assert len(rows) == 1 and rows[0] in event.response, (event.status, dispatched)
    action = frappe.get_doc(actions.ACTION, rows[0])
    assert action.status == "Pending" and action.agent_run == event.huf_run and action.requester_user == state.users[0]
    return action


def exercise(state):
    alice, bob = state.users
    label = "Dinext synthetic ToDo " + state.tag
    if frappe.db.exists("Custom DocPerm", {"parent": "ToDo", "role": "Dinext_Sales_Manager", "create": 1}):
        frappe.get_doc("User", alice).add_roles("Dinext_Sales_Manager")
        frappe.db.commit()

    def decide(action, command):
        return acceptance._complete(acceptance._send(state, alice, state.dms[0], command + " " + action.name))

    with patch.object(bridge, "_queue"):
        try:
            offer = _offer(state, {"operation": "create", "doctype": "ToDo", "values": {"description": label}})
            assert not frappe.db.exists("ToDo", {"owner": alice, "description": label})
            other = acceptance._event(acceptance._send(state, bob, state.dms[1], "confirm " + offer.name))
            bridge.process_event(other.name)
            assert other.reload().status == "Denied" and offer.reload().status == "Pending"
            decide(offer, "confirm")
            assert offer.reload().status == "Applied", (offer.status, offer.result_json)
            name = json.loads(offer.result_json)["name"]
            assert frappe.db.get_value("ToDo", name, "owner") == alice
            decide(offer, "confirm")
            assert frappe.db.count("ToDo", {"owner": alice, "description": label}) == 1
            with bridge.employee_context(bob):
                try:
                    assert not business.list_documents("ToDo", ["name"], {"name": name})["rows"]
                except frappe.PermissionError:
                    pass
            reject = _offer(state, {"operation": "update", "doctype": "ToDo", "name": name,
                                   "values": {"description": label + " declined"}})
            decide(reject, "reject")
            assert reject.reload().status == "Rejected" and frappe.db.get_value("ToDo", name, "description") == label
            stale = _offer(state, {"operation": "update", "doctype": "ToDo", "name": name,
                                   "values": {"description": label + " stale"}})
            with bridge.employee_context(alice):
                doc = frappe.get_doc("ToDo", name)
                doc.priority = "High"
                doc.save()
                frappe.db.commit()
            decide(stale, "confirm")
            assert stale.reload().status == "Stale" and frappe.db.get_value("ToDo", name, "description") == label
            update = _offer(state, {"operation": "update", "doctype": "ToDo", "name": name,
                                   "values": {"description": label + " confirmed"}})
            decide(update, "confirm")
            assert update.reload().status == "Applied"
            assert frappe.db.get_value("ToDo", name, "description") == label + " confirmed"
            with bridge.employee_context(alice):
                may_delete = frappe.has_permission("ToDo", "delete", doc=frappe.get_doc("ToDo", name))
            delete = _offer(state, {"operation": "delete", "doctype": "ToDo", "name": name},
                            expect_denied=not may_delete)
            if may_delete:
                decide(delete, "confirm")
                assert delete.reload().status == "Applied" and not frappe.db.exists("ToDo", name)
            else:
                assert frappe.db.exists("ToDo", name)
        finally:
            acceptance._delete_where("ToDo", owner=alice, description=["like", label + "%"])
            frappe.db.commit()
    return {"native_huf_dispatch": True, "proposal_in_dm": True, "wrong_employee_denied": True,
            "confirmed_create_and_update": True, "delete_matches_site_permission": True,
            "duplicate": True, "declined": True, "stale": True}

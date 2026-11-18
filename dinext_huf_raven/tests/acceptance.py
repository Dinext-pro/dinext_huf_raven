"""Local Raven/Huf acceptance with disposable employees, channels and configuration."""

import json
import secrets
import sys
import time
from contextlib import contextmanager
from html import escape
from unittest.mock import patch

import frappe
from frappe.utils.password import update_password

from dinext_huf_raven import bridge, delivery
from dinext_huf_raven.raven_context import EVENT


MARKER = "__TEST_SCENARIO__:TEST_TEXT"
TEST_REPLY = "This is a deterministic TEST_TEXT response from the HUF test provider."
PROVIDER = "test_provider"
SETTINGS = "Dinext AI Settings"


def _insert(doctype, **fields):
    return frappe.get_doc({"doctype": doctype, **fields}).insert(ignore_permissions=True)


def _delete(doctype, name):
    if name and frappe.db.exists(doctype, name):
        frappe.delete_doc(doctype, name, ignore_permissions=True, force=True, delete_permanently=True)


def _delete_where(doctype, **filters):
    for name in frappe.get_all(doctype, filters=filters, pluck="name"):
        _delete(doctype, name)


def _member(channel, user):
    if not frappe.db.exists("Raven Channel Member", {"channel_id": channel, "user_id": user}):
        doc = frappe.get_doc({"doctype": "Raven Channel Member", "channel_id": channel, "user_id": user})
        doc.flags.ignore_system_message = True
        doc.insert(ignore_permissions=True)


def _create(state, model):
    from dinext_huf_raven.setup import activate, provision

    for index, address in enumerate(state.users):
        _insert("User", email=address, first_name=f"QA {index + 1}", language="en", enabled=1,
                send_welcome_email=0, roles=[{"role": "Raven User"}, {"role": "Huf User"}])
        update_password(address, state.password)
        frappe.db.set_value("Raven User", address, "onboarded", 1)
    if not model:
        if state.owns_provider:
            _insert("AI Provider", provider_name=PROVIDER, provider_brand="other", api_key="local-test-placeholder")
        _insert("AI Model", model_name=state.test_model, provider=PROVIDER, modalities="Text")
        model = state.test_model
    # The installation helper defines exactly the same Huf tool allow-list as the real assistant.
    provision(raven_bot=state.bot, agent=state.agent, model=model, users=state.users)
    activate(state.bot, state.agent)
    with bridge.employee_context(state.users[0]):
        workspace = _insert("Raven Workspace", workspace_name=state.workspace, type="Private")
        for user in [state.users[1], state.bot]:
            _insert("Raven Workspace Member", workspace=workspace.name, user=user)
        channel = _insert("Raven Channel", channel_name="acceptance", workspace=workspace.name, type="Private")
        state.channel = channel.name
        for user in [state.users[1], state.bot]:
            _member(channel.name, user)
    bot = frappe.get_doc("Raven Bot", state.bot)
    state.dms = []
    for user in state.users:
        with bridge.employee_context(user):
            state.dms.append(bot.create_direct_message_channel(user))
    frappe.db.commit()


def _cleanup(state):
    # Restore the singleton before deleting anything it may still reference.
    current = frappe.get_single(SETTINGS)
    if current.raven_bot == state.bot and current.agent == state.agent:
        frappe.db.set_single_value(SETTINGS, state.settings)
    frappe.db.commit()
    if frappe.db.exists("DocType", "Dinext AI Action"):
        _delete_where("Dinext AI Action", requester_user=["in", state.users])
    _delete_where(EVENT, raven_bot=state.bot, agent=state.agent)
    for conversation in frappe.get_all("Agent Conversation", {"agent": state.agent}, pluck="name"):
        for doctype in ("Agent Tool Call", "Agent Message", "Agent Run"):
            _delete_where(doctype, conversation=conversation)
        _delete("Agent Conversation", conversation)
    for user in state.users:
        _delete_where("File", owner=user)
    # Raven's channel controller removes its messages. Delete threads while their
    # root messages still exist and before the workspace removes the parent.
    if frappe.db.exists("Raven Workspace", state.workspace):
        _delete_where("Raven Channel", workspace=state.workspace, is_thread=1)
        _delete("Raven Workspace", state.workspace)
    for user in state.users:
        for field in ("dm_user_1", "dm_user_2"):
            _delete_where("Raven Channel", **{field: user, "is_direct_message": 1})
    _delete("Agent", state.agent)
    _delete("Raven Bot", state.bot)
    # Raven clears the bot link before deleting its user. Some v3 versions
    # leave that synthetic profile in the directory after the bot is gone.
    if frappe.db.exists("Raven User", {"name": state.bot, "type": "Bot", "bot": ["is", "not set"]}):
        _delete("Raven User", state.bot)
    _delete("AI Model", state.test_model)
    if state.owns_provider and not frappe.db.exists("AI Model", {"provider": PROVIDER}):
        _delete("AI Provider", PROVIDER)
    for user in state.users:
        _delete_where("Huf User Role", user=user)
        _delete("User", user)
    frappe.db.commit()


@contextmanager
def fixture(model=None):
    from raven.notification import push_disabled

    if frappe.local.site != "localhost" or frappe.session.user != "Administrator" or not push_disabled():
        raise RuntimeError("This fixture requires the localhost bench console and disabled Raven push notifications.")
    tag = secrets.token_hex(10)
    original = frappe.get_single(SETTINGS)
    state = frappe._dict(
        users=[f"ai-qa-{tag}-{i}@example.invalid" for i in (1, 2)], password=secrets.token_urlsafe(30),
        tag=tag, workspace=f"AI QA {tag}", bot=f"AI QA Bot {tag}", agent=f"AI QA Agent {tag}",
        test_model=f"dinext-qa-{tag}", owns_provider=not model and not frappe.db.exists("AI Provider", PROVIDER),
        settings={field: original.get(field) for field in ("enabled", "raven_bot", "agent")},
    )
    for doctype, name in [("User", user) for user in state.users] + [
        ("Agent", state.agent), ("Raven Bot", state.bot), ("Raven Workspace", state.workspace), ("AI Model", state.test_model),
    ]:
        if frappe.db.exists(doctype, name):
            raise RuntimeError("A generated fixture identity already exists.")
    old_flags = frappe.flags.in_test, frappe.flags.mute_emails
    frappe.flags.in_test = frappe.flags.mute_emails = True
    try:
        _create(state, model)
        yield state
    finally:
        try:
            frappe.db.rollback()
            _cleanup(state)
        finally:
            frappe.flags.in_test, frappe.flags.mute_emails = old_flags


def _send(state, user, channel, text, mention=False, files=None):
    from raven.api.raven_message import send_message_with_attachments

    prefix = f'<span data-type="userMention" data-id="{escape(state.bot)}">@QA</span> ' if mention else ""
    with bridge.employee_context(user):
        created = send_message_with_attachments(
            channel, content=f"<p>{prefix}{escape(text)}</p>", files=files or [],
            client_id=secrets.token_hex(16), send_silently=True,
        )
        frappe.db.commit()
    return created[-1].name


def _event(source):
    return frappe.get_doc(EVENT, {"source_message": source})


def _complete(source):
    event = _event(source)
    bridge.process_event(event.name)
    delivery.deliver_event(event.name)
    frappe.db.commit()
    event.reload()
    assert event.status == "Delivered", (source, event.status, event.huf_run)
    return event


def _assert_dm(event, dm):
    reply = frappe.get_doc("Raven Message", event.reply_message)
    assert reply.channel_id == dm and reply.is_bot_message and TEST_REPLY in reply.content
    run = frappe.get_doc("Agent Run", event.huf_run)
    assert run.provider == PROVIDER and run.status == "Success" and run.owner == event.requester_user


def exercise(state):
    """Exercise actual Raven hooks, Huf TEST_TEXT, durable recovery and deterministic revocation."""
    from frappe.utils.file_manager import save_file

    alice, bob = state.users
    with patch.object(bridge, "_queue"):
        dm_source = _send(state, alice, state.dms[0], MARKER + " DM")
        dm = _complete(dm_source)
        _assert_dm(dm, state.dms[0])
        first_receipt = dm.reply_message
        with bridge.employee_context(alice):
            assert bridge.on_message(frappe.get_doc("Raven Message", dm_source)) == dm.name
        replay = _complete(dm_source)
        assert replay.reply_message == first_receipt and replay.huf_run == dm.huf_run

        source = _send(state, alice, state.channel, MARKER + " shared", mention=True)
        group = _complete(source)
        _assert_dm(group, state.dms[0])
        notice = frappe.get_doc("Raven Message", group.status_message)
        assert notice.channel_id == source and TEST_REPLY not in notice.content and notice.is_bot_message
        assert dm.huf_conversation != group.huf_conversation

        continuation = _complete(_send(state, alice, source, MARKER + " follow-up"))
        assert continuation.huf_conversation == group.huf_conversation
        _member(source, bob)
        bob_event = _complete(_send(state, bob, source, MARKER + " another employee"))
        _assert_dm(bob_event, state.dms[1])
        assert bob_event.huf_conversation != group.huf_conversation

        with bridge.employee_context(alice):
            file = save_file("qa-" + state.tag + ".txt", b"Synthetic report only", None, None, is_private=1)
        file_source = _send(state, alice, state.dms[0], MARKER + " attachment",
                            files=[{"file_url": file.file_url, "file_size": file.file_size}])
        file_event = _complete(file_source)
        assert len(json.loads(file_event.source_parts)) == 1
        run_files = frappe.parse_json(frappe.db.get_value("Agent Run", file_event.huf_run, "runtime_context"))["files"]
        assert [item["file_id"] for item in run_files] == [file.name]

        revoked = _event(_send(state, bob, state.channel, MARKER + " revoke", mention=True))
        bridge.process_event(revoked.name)
        revoked.reload()
        assert revoked.status == "Ready"
        member = frappe.db.get_value("Raven Channel Member", {"user_id": bob, "channel_id": state.channel})
        _delete("Raven Channel Member", member)
        frappe.db.commit()
        delivery.deliver_event(revoked.name)
        revoked.reload()
        assert revoked.status == "Denied" and not revoked.reply_message and not revoked.response
    return {"provider": PROVIDER, "direct_message": True, "mention": True, "thread": True,
            "per_employee_conversations": True, "duplicate": True, "attachment": True, "revoked": True}


def run():
    """bench --site localhost execute dinext_huf_raven.tests.acceptance.run"""
    from dinext_huf_raven.tests.test_business_site import exercise as exercise_business

    with fixture() as state:
        return {"raven": exercise(state), "business": exercise_business(state)}


def _browser_request(state, request):
    """Cypress talks over local stdin. No fixture management endpoint is exposed on the site."""
    if request["command"] == "fixture":
        return {key: state[key] for key in ("workspace", "channel", "dms", "bot", "tag")}
    if request["command"] == "login":
        import requests
        from urllib.parse import urlsplit

        root = frappe.utils.get_site_url("localhost")
        if urlsplit(root).hostname not in {"localhost", "127.0.0.1"} or request["index"] not in (0, 1):
            raise RuntimeError("The browser login requires the local fixture.")
        user = state.users[request["index"]]
        # Log in through the normal HTTP endpoint without putting a password in a Cypress command log.
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(root.rstrip("/") + "/api/method/login", data={"usr": user, "pwd": state.password},
                                   timeout=(10, 80), allow_redirects=False)
            if response.status_code != 200 or not client.cookies.get("sid"):
                raise RuntimeError("Synthetic employee login failed with HTTP " + str(response.status_code))
            cookies = {key: value for key, value in client.cookies.items()
                       if key in {"sid", "user_id", "full_name", "system_user", "user_image"}}
        return {"user": user, "cookies": cookies}
    source, text = request.get("source"), request.get("text")
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        frappe.db.rollback()
        if not source and text:
            source = frappe.db.get_value("Raven Message", {
                "owner": state.users[0], "content": text, "channel_id": state.dms[0],
            })
        event_id = frappe.db.get_value(EVENT, {"agent": state.agent, "source_message": source}) if source else None
        if event_id:
            event = frappe.get_doc(EVENT, event_id)
            if event.status in ("Denied", "Failed", "Uncertain"):
                raise RuntimeError("Unexpected event state: " + event.status)
            if event.status == "Delivered":
                return {"source": source, "reply": event.reply_message, "notice": event.status_message,
                        "run": event.huf_run, "conversation": event.huf_conversation}
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for a committed Raven reply from the local queues.")


def _serve_browser():
    frappe.init(site="localhost")
    frappe.connect()
    try:
        with fixture() as state:
            for line in sys.stdin:
                request = json.loads(line)
                if request["command"] == "close":
                    break
                try:
                    result = {"result": _browser_request(state, request)}
                except Exception as error:
                    result = {"error": str(error)}
                print("DINEXT_QA " + json.dumps(result), flush=True)
    finally:
        frappe.destroy()


if __name__ == "__main__":
    _serve_browser()

import unittest
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from dinext_huf_raven import bridge, delivery, raven_context


class TestRavenBridge(unittest.TestCase):
    def test_bot_and_system_messages_cannot_start_runs(self):
        message = frappe._dict(owner="employee@example.test", message_type="Text")
        self.assertTrue(raven_context.is_human_message(message, message.owner))
        self.assertFalse(raven_context.is_human_message(message, "other@example.test"))
        for identity in ("Guest", "Administrator"):
            self.assertFalse(raven_context.is_human_message(frappe._dict(message, owner=identity), identity))
        for change in ({"bot": "Assistant"}, {"is_bot_message": 1}, {"message_type": "System"}, {"notification": "notice"}):
            self.assertFalse(raven_context.is_human_message(frappe._dict(message, **change), message.owner))

    def test_dm_needs_exact_recipient_and_current_members(self):
        channel = frappe._dict(is_direct_message=1, dm_user_1="employee", dm_user_2="bot")
        pair = {"employee", "bot"}
        self.assertTrue(raven_context.is_private_pair(channel, pair, pair))
        self.assertFalse(raven_context.is_private_pair(channel, pair, pair | {"third"}))
        self.assertFalse(raven_context.is_private_pair(channel, pair, {"employee"}))
        self.assertFalse(raven_context.is_private_pair(channel, {"other", "bot"}, {"other", "bot"}))
        legacy_dm = frappe._dict(is_direct_message=1)
        self.assertTrue(raven_context.is_private_pair(legacy_dm, pair, pair))

    def test_only_structural_mentions_address_bot(self):
        message = frappe._dict(text="<p>@Dinext AI</p>", mentions=[])
        self.assertFalse(raven_context.mentions_bot(message, "Dinext AI"))
        message.mentions = [frappe._dict(user="Dinext AI")]
        self.assertFalse(raven_context.mentions_bot(message, "Dinext AI"))
        message.text = '<p><span data-type="userMention" data-id="Dinext AI">@Assistant</span></p>'
        self.assertTrue(raven_context.mentions_bot(message, "Dinext AI"))

    def test_partition_includes_site_user_agent_and_dialog(self):
        event = frappe._dict(requester_user="employee", agent="Agent", dialog_id="thread",
                             root_channel="channel", raven_bot="Bot")
        key = bridge.dialog_key(event, "first.test")
        self.assertEqual(key, bridge.dialog_key(event, "first.test"))
        self.assertNotEqual(key, bridge.dialog_key(event, "second.test"))
        for field in ("requester_user", "agent", "dialog_id", "root_channel", "raven_bot"):
            self.assertNotEqual(key, bridge.dialog_key(frappe._dict(event, **{field: "other"}), "first.test"))

    def test_cached_result_can_only_enter_delivery(self):
        event = frappe._dict(name="event", status="Ready", requester_user="employee")
        with patch.object(bridge.frappe, "get_doc", return_value=event), \
             patch.object(bridge, "enqueue_delivery") as post, \
             patch.object(bridge, "_execute") as execute:
            bridge.process_event(event.name)
        post.assert_called_once_with(event.name)
        execute.assert_not_called()

    def test_renderer_treats_model_html_and_mentions_as_text(self):
        rendered = delivery.response_html('<span data-type="userMention">Hi</span>\n<script>bad()</script>', [])
        self.assertNotIn("<span", rendered)
        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_group_result_uses_dm_and_fixed_thread_notice(self):
        event = frappe._dict(source_message="source", requester_user="employee", response="ERP secret")
        bot = Mock()
        route = SimpleNamespace(bot=bot, direct=False, channel=frappe._dict(name="thread", is_thread=1))
        with patch.object(delivery, "_private_target", return_value="private-dm"), \
             patch.object(delivery, "_", side_effect=lambda value: value):
            delivery.publish(event, route, [])
        personal, notice = bot.send_message.call_args_list
        self.assertEqual(personal.args[0], "private-dm")
        self.assertIn("ERP secret", personal.kwargs["text"])
        self.assertEqual(notice.args[0], "thread")
        self.assertNotIn("ERP secret", notice.kwargs["text"])

    def test_recovery_reuses_huf_response(self):
        event = frappe._dict(name="event", huf_run="run")
        run = frappe._dict(name="run", status="Success", response="Saved response")
        with patch.object(bridge, "_run_record", return_value=run), \
             patch.object(bridge, "_finish") as finish, patch.object(bridge, "_execute") as execute:
            bridge._reconcile(event, stale=True)
        finish.assert_called_once_with(event, run.response)
        execute.assert_not_called()

    def test_lost_outcome_is_withheld_from_automatic_resubmission(self):
        event = frappe._dict(name="event", huf_run=None)
        with patch.object(bridge, "_run_record", return_value=None), \
             patch.object(bridge, "_", side_effect=lambda value: value), \
             patch.object(bridge, "_update") as update, patch.object(bridge, "enqueue_delivery") as post:
            bridge._reconcile(event, stale=True)
        self.assertEqual(update.call_args.kwargs["status"], "Uncertain")
        post.assert_called_once_with("event")

    def test_revoked_read_skips_all_raven_sends(self):
        event = frappe._dict(name="event", status="Ready", requester_user="employee", reply_message=None)
        db = Mock()
        with patch.object(delivery.frappe, "get_doc", return_value=event), \
             patch.object(delivery.frappe, "db", db), \
             patch.object(bridge, "employee_context", return_value=nullcontext()), \
             patch.object(delivery, "authorize_event", return_value=Mock()), \
             patch.object(bridge, "check_disclosure", side_effect=frappe.PermissionError), \
             patch.object(delivery, "publish") as publish:
            delivery.deliver_event("event")
        publish.assert_not_called()
        db.set_value.assert_called_once_with(raven_context.EVENT, "event", {"status": "Denied", "response": None})

    def test_failed_raven_send_leaves_ready_for_retry(self):
        event = frappe._dict(name="event", status="Ready", requester_user="employee", reply_message=None)
        db = Mock()
        with patch.object(delivery.frappe, "get_doc", return_value=event), \
             patch.object(delivery.frappe, "db", db), \
             patch.object(bridge, "employee_context", return_value=nullcontext()), \
             patch.object(delivery, "authorize_event", return_value=Mock()), \
             patch.object(bridge, "check_disclosure", return_value=[]), \
             patch.object(delivery, "publish", side_effect=RuntimeError("queue down")):
            with self.assertRaises(RuntimeError):
                delivery.deliver_event("event")
        db.set_value.assert_not_called()

    def test_renderer_links_private_file_and_local_desk_document(self):
        file = frappe._dict(file_url="/private/files/output.csv", file_name='data & rows.csv')
        rendered = delivery.response_html("[Order](/app/sales-order/SO-1) <img src='https://outside.test/a'>", [file])
        self.assertIn('href="/app/sales-order/SO-1"', rendered)
        self.assertIn('href="/private/files/output.csv">data &amp; rows.csv', rendered)
        self.assertNotIn("<img", rendered)

    def test_only_local_frappe_uploads_are_eligible(self):
        self.assertTrue(raven_context.local_file_url("/private/files/my%20report.pdf"))
        for url in ("https://other.test/private/files/a", "//other.test/files/a", "/files/%2e%2e/secrets", "/api/method/x"):
            self.assertFalse(raven_context.local_file_url(url))

    def test_worker_user_and_original_session_are_restored_after_error(self):
        original = frappe._dict(user="scheduler", sid="original", data={"saved": 1})
        local = SimpleNamespace(session=original, cache={}, form_dict={}, jenv_restricted=None, jenv_unrestricted=None,
                                role_permissions={}, new_doc_templates={}, user_perms=None, lang="ru",
                                flags=frappe._dict())
        def switch(user):
            local.session.update(user=user, sid=user, data={})
            local.role_permissions = {"acting_user": user}
        db = Mock()
        db.get_value.return_value = "en"
        with patch.object(bridge.frappe, "local", local), patch.object(bridge.frappe, "db", db), \
             patch.object(bridge.frappe, "set_user", side_effect=switch):
            with self.assertRaises(RuntimeError):
                with bridge.employee_context("employee"):
                    self.assertEqual(local.session.user, "employee")
                    self.assertEqual(local.lang, "en")
                    raise RuntimeError("interrupted")
        self.assertIs(local.session, original)
        self.assertEqual(original.sid, "original")
        self.assertEqual(local.role_permissions, {})
        self.assertEqual(local.lang, "ru")

    def test_observed_attachments_share_one_event_with_the_caption(self):
        owner = "employee"
        flags = frappe._dict()
        event = Mock()
        event.name = "event"
        db = Mock()
        db.get_value.return_value = None
        route = SimpleNamespace(root_channel="source", dialog_id="dialog", bot=frappe._dict(name="bot"),
                                agent=frappe._dict(name="agent"))
        common = dict(owner=owner, channel_id="channel", message_batch_id=None)
        capture = Mock(return_value=event)
        with patch.object(bridge.frappe, "flags", flags), \
             patch.object(bridge.frappe, "session", frappe._dict(user=owner)), \
             patch.object(bridge.frappe, "db", db), \
             patch.object(bridge.frappe, "get_single", return_value=frappe._dict(enabled=1)), \
             patch.object(bridge.frappe, "get_doc", capture), patch.object(bridge, "route_message", return_value=route), \
             patch.object(bridge, "enqueue_processing") as enqueue:
            for name in ("file-1", "file-2"):
                bridge.on_message(frappe._dict(common, name=name, message_type="File",
                                               flags=frappe._dict(skip_channel_summary=1)))
            enqueue.assert_not_called()
            bridge.on_message(frappe._dict(common, name="caption", message_type="Text", flags=frappe._dict()))
        saved = capture.call_args.args[0]
        self.assertEqual(saved["source_message"], "caption")
        self.assertEqual(json.loads(saved["source_parts"]), ["file-1", "file-2"])
        enqueue.assert_called_once_with(event.name)

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import frappe
from openpyxl import Workbook

from dinext_huf_raven import analytics, business_fields, file_extractors


class TestBusinessAnalytics(unittest.TestCase):
    def test_chart_is_rendered_as_a_private_artifact(self):
        doc = Mock(name="private chart", file_name="chart.png")
        doc.name = "private-file"
        tables = {"records": (["label", "total"], [{"label": "A", "total": 2}, {"label": "B", "total": 4}])}
        with patch.object(frappe, "get_doc", return_value=Mock(insert=Mock(return_value=doc))) as get_doc, \
             patch.object(analytics.bridge, "add_artifact") as register:
            result = analytics._chart(tables, {"table": "records", "x": "label", "y": "total"})
        self.assertEqual(result["file_id"], "private-file")
        self.assertEqual(get_doc.call_args.args[0]["is_private"], 1)
        self.assertTrue(get_doc.call_args.args[0]["content"].startswith(b"\x89PNG\r\n\x1a\n"))
        register.assert_called_once_with(doc)

    def test_projection_uses_permission_aware_read_for_sql_python_and_summary(self):
        source = {"records": {"doctype": "ToDo", "fields": ["value"], "filters": {"value": [">", 0]}}}
        visible = {"rows": [{"value": 2}, {"value": 3}], "next_start": None}
        with patch.object(analytics, "_event"), patch.object(analytics.business, "query", return_value=visible) as read:
            sql = analytics.analyze("sql", source, "select sum(value) from records")
            python = analytics.analyze("python", source, "df['value'].sum()")
            summary = analytics.analyze("summary", source)
        self.assertEqual(sql["result"]["rows"], [[5]])
        self.assertEqual(python["result"]["value"], 5)
        self.assertEqual(summary["result"]["records"]["value"]["mean"], 2.5)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(read.call_args.args[0], "list_documents")
        self.assertEqual(read.call_args.args[1]["filters"], {"value": [">", 0]})

    def test_projection_reports_paging_and_limits_the_selected_records(self):
        source = {"records": {"doctype": "Task", "fields": ["name"], "limit": 102}}
        rows = [{"name": str(i)} for i in range(100)]
        with patch.object(analytics, "_event"), patch.object(analytics.business, "query", side_effect=[
            {"rows": rows, "next_start": 100}, {"rows": [{"name": "100"}, {"name": "101"}], "next_start": 102},
        ]) as query:
            result = analytics.analyze("summary", source)
        self.assertEqual(query.call_args.args[1]["start"], 100)
        self.assertEqual(query.call_args.args[1]["limit"], 2)
        self.assertEqual(result["scope"]["records"], {"rows": 102, "next_start": 102})

    def test_text_and_structured_csv_in_the_resource_limited_parser(self):
        self.assertEqual(analytics._parse_file("txt", "Образец".encode(), "extract")["text"], "Образец")
        table = analytics._parse_file("csv", b"sku;qty\nA;2\nB;3\n", "parse_data")
        self.assertEqual(table["rows"], [["sku", "qty"], ["A", "2"], ["B", "3"]])

    def test_small_office_and_pdf_inputs(self):
        import pymupdf

        for ext, path, uri in (
            ("docx", "word/document.xml", "http://schemas.openxmlformats.org/wordprocessingml/2006/main"),
            ("pptx", "ppt/slides/slide1.xml", "http://schemas.openxmlformats.org/drawingml/2006/main"),
        ):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr(path, f'<root xmlns:w="{uri}"><w:p><w:t>Example</w:t></w:p></root>')
            self.assertEqual(analytics._parse_file(ext, buf.getvalue(), "extract")["text"], "Example")
        buf = io.BytesIO()
        wb = Workbook()
        wb.active.append(["Head", 2])
        wb.save(buf)
        self.assertEqual(analytics._parse_file("xlsx", buf.getvalue(), "parse_data")["sheets"][0]["rows"], [["Head", "2"]])
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((30, 50), "Employee report")
            self.assertIn("Employee report", analytics._parse_file("pdf", pdf.tobytes(), "extract")["text"])

    def test_image_without_ocr_reports_that_it_has_no_transcription(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 15)).save(buf, "PNG")
        with patch.object(file_extractors.shutil, "which", return_value=None):
            data = file_extractors.extract("png", buf.getvalue())
        self.assertEqual(data["status"], "ocr_unavailable")
        self.assertEqual((data["width"], data["height"]), (10, 15))

    def test_file_membership_and_live_content_are_checked_again(self):
        user = "employee@example.test"
        event = frappe._dict(requester_user=user)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "a.txt")
            path.write_bytes(b"First")
            doc = frappe._dict(name="file", file_name="a.txt", owner=user, is_private=1, attached_to_doctype="Raven Message",
                               attached_to_name="m", attached_to_field="file", file_url="/private/files/a.txt",
                               has_permission=lambda p: True, get_full_path=lambda: str(path))
            with patch.object(analytics, "_event", return_value=event), patch.object(frappe, "get_doc", return_value=doc), \
                 patch.object(frappe, "get_site_path", return_value=directory), \
                 patch.object(analytics.bridge, "authorize_event", return_value=frappe._dict(files=[{"file_id": "file"}])) as route, \
                 patch.object(analytics, "deny", side_effect=frappe.PermissionError), \
                 patch.object(analytics.bridge, "track_business_read") as tracked:
                result = analytics.extract_file("file")
                self.assertEqual(result["text"], "First")
                check = tracked.call_args.args[0]
                self.assertTrue(analytics.recheck_disclosure(check))
                path.write_bytes(b"Changed")
                self.assertFalse(analytics.recheck_disclosure(check))
                route.return_value.files = []
                with self.assertRaises(frappe.PermissionError):
                    analytics.extract_file("file")

    def test_prompt_supports_values_and_conditionals_and_refuses_executable_expressions(self):
        body = "Item {{ sku }}{% if totals %} with quantities{% endif %}"
        self.assertEqual(analytics._template(body, {})["parameters"], ["sku", "totals"])
        self.assertEqual(analytics._template(body, {"sku": "ABC", "totals": True})["text"], "Item ABC with quantities")
        with patch.object(analytics, "invalid", side_effect=frappe.ValidationError), self.assertRaises(frappe.ValidationError):
            analytics._template("{{ sku.upper() }}", {"sku": "ABC"})

    def test_prompt_private_to_another_user_cannot_enter_results(self):
        private = frappe._dict(name="private", owner="other@example.test", visibility="Private")
        with patch.object(frappe, "session", frappe._dict(user="me")), \
             patch.object(frappe, "get_list", return_value=[frappe._dict(name="private")]), \
             patch.object(frappe, "get_doc", return_value=private), patch.object(analytics, "can", return_value=True):
            self.assertEqual(analytics._instructions("prompt")["items"], [])

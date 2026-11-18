"""Bounded local parser. The worker supplies authorized bytes, never a URL or user-supplied path."""

import csv
import io
import json
import re
import shutil
import subprocess
import sys
import zipfile


MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 32000
MAX_PIXELS = 8000000


def _plain(content):
    for encoding in ("utf-8-sig", "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "cp1251"):
        try:
            text = content.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" in text:
            raise ValueError("binary")
        return text
    raise ValueError("encoding")


def _office_archive(content):
    archive = zipfile.ZipFile(io.BytesIO(content))
    entries = archive.infolist()
    if len(entries) > 2000 or sum(f.file_size for f in entries) > 32 * 1024 * 1024 or any(
        f.file_size > 12 * 1024 * 1024 or f.flag_bits & 1 for f in entries
    ):
        archive.close()
        raise ValueError("size")
    return archive


def _office_text(archive, extension):
    from defusedxml.ElementTree import fromstring

    if extension == "docx":
        members = ["word/document.xml"]
        ptag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    else:
        members = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                         key=lambda n: int(re.search(r"(\d+)\.xml", n)[1]))
        ptag = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    lines = []
    length = 0
    for member in members[:100]:
        for paragraph in fromstring(archive.read(member)).iter(ptag + "p"):
            line = "".join(node.text or "" for node in paragraph.iter(ptag + "t"))
            lines.append(line)
            length += len(line) + 1
            if length > MAX_TEXT:
                return {"text": "\n".join(lines)[:MAX_TEXT], "truncated": True}
    return {"text": "\n".join(lines), "truncated": len(members) > 100}


def _sheet(content):
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
    lines, sheets, length, truncated = [], [], 0, len(workbook.sheetnames) > 5
    try:
        for sheet in workbook.worksheets[:5]:
            lines.append(str(sheet.title))
            data = []
            sheets.append({"name": sheet.title, "rows": data})
            truncated |= bool((sheet.max_row or 0) > 200 or (sheet.max_column or 0) > 32)
            for row in sheet.iter_rows(max_row=min(sheet.max_row or 200, 200), max_col=min(sheet.max_column or 32, 32),
                                       values_only=True):
                cells = ["" if cell is None else str(cell)[:1000] for cell in row]
                text = "\t".join(cells)
                data.append(cells)
                lines.append(text)
                length += len(text) + 1
                if length > MAX_TEXT:
                    return {"text": "\n".join(lines)[:MAX_TEXT], "sheets": sheets, "truncated": True}
    finally:
        workbook.close()
    return {"text": "\n".join(lines)[:MAX_TEXT], "sheets": sheets, "truncated": truncated}


def _ocr(png):
    binary = shutil.which("tesseract")
    if not binary:
        return None
    langs = subprocess.run([binary, "--list-langs"], capture_output=True, timeout=4, check=True).stdout.decode().splitlines()
    preferred = [lang for lang in ("rus", "eng") if lang in langs]
    if not preferred:
        return None
    return subprocess.run([binary, "stdin", "stdout", "-l", "+".join(preferred)], input=png,
                          capture_output=True, check=True, timeout=8).stdout.decode("utf-8")


def _image(content):
    from PIL import Image

    with Image.open(io.BytesIO(content)) as img:
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("size")
        data = io.BytesIO()
        img.convert("RGB").save(data, format="PNG")
        text = _ocr(data.getvalue())
        return {"text": (text or "")[:MAX_TEXT], "width": img.width, "height": img.height,
                "status": "ocr_unavailable" if text is None else "extracted", "truncated": len(text or "") > MAX_TEXT}


def _pdf(content, operation):
    import pymupdf

    parts, tables, length, missing_ocr, ocr_pages = [], [], 0, False, 0
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("encrypted")
        truncated = len(doc) > 40
        for index in range(min(len(doc), 40)):
            page = doc[index]
            if operation == "extract_tables":
                for table in page.find_tables().tables:
                    rows = [[str(v or "")[:1000] for v in row[:32]] for row in table.extract()[:100]]
                    length += sum(len(c) + 1 for row in rows for c in row)
                    tables.append({"page": index + 1, "rows": rows})
                    if length > MAX_TEXT or len(tables) >= 25:
                        return {"tables": tables, "truncated": True}
                continue
            text = "" if operation == "ocr" else page.get_text()
            if not text.strip():
                if ocr_pages >= 3:
                    truncated = True
                elif shutil.which("tesseract") and 0 < page.rect.width * page.rect.height * 2.25 <= MAX_PIXELS:
                    image = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                    extracted = _ocr(image.tobytes("png"))
                    missing_ocr |= extracted is None
                    text = extracted or ""
                    ocr_pages += 1
                else:
                    missing_ocr = True
            parts.append(text)
            length += len(text) + 1
            if length > MAX_TEXT:
                truncated = True
                break
    if operation == "extract_tables":
        return {"tables": tables, "truncated": truncated}
    return {"text": "\n".join(parts)[:MAX_TEXT], "truncated": truncated,
            "status": "ocr_unavailable" if missing_ocr else "extracted"}


def extract(extension, content, operation="extract"):
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError("size")
    if operation not in {"extract", "ocr", "parse_data", "extract_tables"}:
        raise ValueError("operation")
    if operation == "extract_tables" and extension != "pdf" or operation == "parse_data" and extension not in {"csv", "tsv", "xlsx"}:
        raise ValueError("format")
    if operation == "ocr" and extension not in {"pdf", "png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif"}:
        raise ValueError("format")
    if extension in {"txt", "md", "log", "json", "csv", "tsv"}:
        text = _plain(content)
        if extension in {"csv", "tsv"}:
            dialect = csv.excel_tab if extension == "tsv" else csv.excel
            if extension == "csv":
                try:
                    dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
                except csv.Error:
                    pass
            rows, data = [], []
            truncated = False
            length = 0
            for line in csv.reader(io.StringIO(text), dialect=dialect):
                if len(rows) >= 200 or length >= MAX_TEXT:
                    truncated = True
                    break
                truncated |= len(line) > 32
                cells = [v[:1000] for v in line[:32]]
                result = "\t".join(cells)
                data.append(cells)
                length += len(result) + 1
                rows.append(result)
            return {"text": "\n".join(rows)[:MAX_TEXT], "rows": data, "truncated": truncated or length > MAX_TEXT}
        return {"text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT}
    if extension == "pdf":
        return _pdf(content, operation)
    if extension in {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif"}:
        return _image(content)
    if extension in {"xlsx", "docx", "pptx"}:
        with _office_archive(content) as archive:
            return _sheet(content) if extension == "xlsx" else _office_text(archive, extension)
    raise ValueError("unsupported")


if __name__ == "__main__":
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (14, 16))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    print(json.dumps(extract(sys.argv[1], sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[2]), ensure_ascii=False))

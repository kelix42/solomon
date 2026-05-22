"""Tests for solomon.corpus.extract.

Covers the per-format extractors that don't need a network. Vision-fallback
paths (scanned PDFs, images) are exercised by mocking ``extract_text_via_sonnet``
so we hit the dispatch / metadata code without an actual API call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solomon.corpus import extract as ce
from solomon.corpus.extract import (
    ExtractedDoc,
    UnsupportedFileType,
    extract,
    extract_csv,
    extract_eml,
    extract_html,
    extract_json,
    extract_txt_md,
)


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def test_extract_txt_md_reads_utf8(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# heading\n\nhello world", encoding="utf-8")
    doc = extract_txt_md(p)
    assert doc.text == "# heading\n\nhello world"
    assert doc.metadata["extractor"] == "txt_md"
    assert doc.metadata["format"] == ".md"


def test_extract_txt_md_lossy_on_bad_bytes(tmp_path):
    p = tmp_path / "weird.txt"
    p.write_bytes(b"hello\xff\xfeworld")
    doc = extract_txt_md(p)
    assert "hello" in doc.text and "world" in doc.text


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------


def test_extract_csv_pipe_joins(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("name,role\nAlice,founder\nBob,coo\n", encoding="utf-8")
    doc = extract_csv(p)
    assert doc.text.startswith("name | role")
    assert "Alice | founder" in doc.text
    assert doc.metadata["row_count"] == 3


def test_extract_tsv_uses_tab(tmp_path):
    p = tmp_path / "data.tsv"
    p.write_text("a\tb\n1\t2\n", encoding="utf-8")
    doc = extract_csv(p)
    assert "a | b" in doc.text
    assert "1 | 2" in doc.text


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_extract_json_pretty_sorted(tmp_path):
    p = tmp_path / "data.json"
    p.write_text('{"b": 1, "a": 2}', encoding="utf-8")
    doc = extract_json(p)
    # Pretty-printed and key-sorted.
    parsed = json.loads(doc.text)
    assert parsed == {"a": 2, "b": 1}
    lines = doc.text.splitlines()
    # Key 'a' should appear before key 'b'.
    a_idx = next(i for i, ln in enumerate(lines) if '"a"' in ln)
    b_idx = next(i for i, ln in enumerate(lines) if '"b"' in ln)
    assert a_idx < b_idx


def test_extract_json_fallback_on_bad_input(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json at all", encoding="utf-8")
    doc = extract_json(p)
    assert doc.text == "not json at all"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_extract_html_drops_script_style(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(
        "<html><head><style>body{color:red}</style></head>"
        "<body><h1>Title</h1><script>alert(1)</script>"
        "<p>Hello there.</p></body></html>",
        encoding="utf-8",
    )
    doc = extract_html(p)
    assert "Title" in doc.text
    assert "Hello there." in doc.text
    assert "color:red" not in doc.text
    assert "alert" not in doc.text


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def test_extract_eml_headers_and_body(tmp_path):
    p = tmp_path / "msg.eml"
    p.write_bytes(
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Hello\r\n"
        b"Date: Mon, 1 Jan 2026 10:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Quick note about the meeting.\r\n"
    )
    doc = extract_eml(p)
    assert "From: alice@example.com" in doc.text
    assert "Subject: Hello" in doc.text
    assert "Quick note about the meeting." in doc.text
    assert doc.metadata["headers"]["subject"] == "Hello"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def test_extract_docx_paragraphs_and_tables(tmp_path):
    docx = pytest.importorskip("docx")
    p = tmp_path / "doc.docx"
    d = docx.Document()
    d.add_paragraph("First paragraph.")
    d.add_paragraph("Second paragraph.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "h1"
    table.cell(0, 1).text = "h2"
    table.cell(1, 0).text = "v1"
    table.cell(1, 1).text = "v2"
    d.save(str(p))
    doc = ce.extract_docx(p)
    assert "First paragraph." in doc.text
    assert "Second paragraph." in doc.text
    assert "h1 | h2" in doc.text
    assert "v1 | v2" in doc.text


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def test_extract_pptx_slide_headers(tmp_path):
    pptx = pytest.importorskip("pptx")
    p = tmp_path / "deck.pptx"
    pres = pptx.Presentation()
    slide_layout = pres.slide_layouts[5]  # Title only
    s1 = pres.slides.add_slide(slide_layout)
    s1.shapes.title.text = "Roadmap"
    s2 = pres.slides.add_slide(slide_layout)
    s2.shapes.title.text = "Next quarter"
    pres.save(str(p))
    doc = ce.extract_pptx(p)
    assert "## Slide 1" in doc.text
    assert "## Slide 2" in doc.text
    assert "Roadmap" in doc.text
    assert "Next quarter" in doc.text
    assert doc.metadata["page_count"] == 2
    assert len(doc.page_breaks) == 2


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def test_extract_xlsx_sheets(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "sheet.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Numbers"
    ws.append(["a", "b"])
    ws.append([1, 2])
    ws2 = wb.create_sheet("Names")
    ws2.append(["Alice", "Bob"])
    wb.save(str(p))
    doc = ce.extract_xlsx(p)
    assert "## Sheet: Numbers" in doc.text
    assert "## Sheet: Names" in doc.text
    assert "a | b" in doc.text
    assert "1 | 2" in doc.text
    assert "Alice | Bob" in doc.text
    assert doc.metadata["page_count"] == 2


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _make_simple_pdf(path: Path, body: str) -> None:
    """Build a one-page PDF with `body` as visible text using pypdf's
    page-building primitives. pypdf can write but ``add_blank_page`` plus
    a stream gives us a deterministic text layer with no third-party deps.
    """
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, NameObject, DecodedStreamObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    # Build a minimal content stream:  BT /F1 12 Tf 20 200 Td (<text>) Tj ET
    escaped = body.replace("(", "\\(").replace(")", "\\)")
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 12 Tf 20 200 Td ({escaped}) Tj ET".encode("latin-1")
    )
    page[NameObject("/Contents")] = stream
    # Provide a font dict so pypdf doesn't choke at extract time.
    from pypdf.generic import DictionaryObject, NameObject as N
    page[N("/Resources")] = DictionaryObject(
        {
            N("/Font"): DictionaryObject(
                {
                    N("/F1"): DictionaryObject(
                        {
                            N("/Type"): N("/Font"),
                            N("/Subtype"): N("/Type1"),
                            N("/BaseFont"): N("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    with path.open("wb") as f:
        writer.write(f)


def test_extract_pdf_text_layer(tmp_path):
    """If the PDF has a text layer, we get it back."""
    p = tmp_path / "doc.pdf"
    _make_simple_pdf(p, "Hello PDF world")
    doc = ce.extract_pdf(p)
    assert "Hello PDF world" in doc.text
    assert doc.metadata["extractor"] == "pdf"
    assert "via" not in doc.metadata  # didn't fall back to vision


def test_extract_pdf_no_text_layer_raises_when_vision_disabled(tmp_path, monkeypatch):
    """Empty text layer + no vision API → UnsupportedFileType."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    p = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with p.open("wb") as f:
        writer.write(f)

    monkeypatch.delenv("SOLOMON_ALLOW_VISION_API", raising=False)
    with pytest.raises(UnsupportedFileType):
        ce.extract_pdf(p)


def test_extract_pdf_no_text_layer_uses_vision_stub(tmp_path, monkeypatch):
    """With the flag set + a patched vision fn, the doc.text comes from it."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    p = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with p.open("wb") as f:
        writer.write(f)

    monkeypatch.setattr(ce, "extract_text_via_sonnet", lambda _p: "OCR'd text")
    doc = ce.extract_pdf(p)
    assert doc.text == "OCR'd text"
    assert doc.metadata["via"] == "vision"


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def test_dispatch_unknown_extension_raises(tmp_path):
    p = tmp_path / "file.xyz"
    p.write_text("noise")
    with pytest.raises(UnsupportedFileType):
        extract(p)


def test_dispatch_empty_text_raises(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n   \n", encoding="utf-8")
    with pytest.raises(UnsupportedFileType):
        extract(p)


def test_dispatch_routes_by_suffix(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("hello", encoding="utf-8")
    doc = extract(p)
    assert doc.text == "hello"
    assert doc.metadata["extractor"] == "txt_md"


def test_dispatch_image_blocked_without_flag(tmp_path, monkeypatch):
    p = tmp_path / "photo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # png magic, body irrelevant
    monkeypatch.delenv("SOLOMON_ALLOW_VISION_API", raising=False)
    with pytest.raises(UnsupportedFileType):
        extract(p)


def test_dispatch_image_with_flag_and_stub(tmp_path, monkeypatch):
    p = tmp_path / "photo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(ce, "extract_text_via_sonnet", lambda _p: "caption")
    doc = extract(p)
    assert doc.text == "caption"
    assert doc.metadata["via"] == "vision"

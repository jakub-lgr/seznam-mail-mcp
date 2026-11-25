"""Turn an e-mail attachment into text and/or images an AI client can read.

Supported: PDF (text; scanned pages are rendered as images), Word .docx, Excel .xlsx,
plain text / CSV / JSON / XML / HTML, images, and attached e-mails (message/rfc822).
Everything runs in-process on untrusted input, so sizes, pages, rows and pixels are capped.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from email.message import EmailMessage
from xml.etree import ElementTree

from .htmltext import html_to_text

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_RENDERED_PAGES = 3
MIN_PAGE_TEXT = 10  # characters; less than that = treat the page as a scan
MAX_SHEET_ROWS = 1000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024  # uncompressed, against zip bombs
IMAGE_MAX_SIDE = 1568  # what vision models use anyway; keeps payloads small
IMAGE_MAX_PIXELS = 60_000_000

_TEXT_TYPES = {"application/json", "application/xml", "application/csv", "application/x-csv"}
_DOCX_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_XLSX_TYPES = {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": next(iter(_DOCX_TYPES)),
    ".xlsx": next(iter(_XLSX_TYPES)),
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/plain",
    ".eml": "message/rfc822",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class AttachmentError(Exception):
    """User-facing problem with an attachment (message is safe to show)."""


@dataclass
class Extracted:
    kind: str
    """pdf, docx, xlsx, text, image, email"""
    text: str = ""
    images: list[tuple[bytes, str]] = field(default_factory=list)
    """(data, mime type)"""
    notes: list[str] = field(default_factory=list)
    pages: int | None = None


def effective_type(content_type: str, filename: str | None) -> str:
    """Mail clients often send application/octet-stream; fall back to the file extension."""
    ct = (content_type or "").lower()
    if ct and ct not in ("application/octet-stream", "application/x-download", "application/force-download"):
        return ct
    name = (filename or "").lower()
    for ext, mapped in _EXTENSIONS.items():
        if name.endswith(ext):
            return mapped
    return ct or "application/octet-stream"


def extract(data: bytes | EmailMessage, content_type: str, filename: str | None, charset: str | None = None) -> Extracted:
    ct = effective_type(content_type, filename)
    if isinstance(data, EmailMessage) or ct == "message/rfc822":
        return _email(data)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(f"Příloha je moc velká ({len(data) // (1024 * 1024)} MB, limit {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB).")
    if ct == "application/pdf":
        return _pdf(data)
    if ct in _DOCX_TYPES:
        return _docx(data)
    if ct in _XLSX_TYPES:
        return _xlsx(data)
    if ct in _IMAGE_TYPES or ct.startswith("image/"):
        return Extracted(kind="image", images=[_image(data)])
    if ct.startswith("text/") or ct in _TEXT_TYPES:
        text = _decode(data, charset)
        if ct == "text/html":
            text = html_to_text(text)
        return Extracted(kind="text", text=text)
    raise AttachmentError(
        f"Typ přílohy {ct} zatím neumím přečíst. Podporované: PDF, Word (.docx), Excel (.xlsx), "
        "text, CSV, HTML, obrázky a přiložené e-maily."
    )


# ---------------------------------------------------------------- formats


def _decode(data: bytes, charset: str | None) -> str:
    for enc in filter(None, (charset, "utf-8", "cp1250")):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(data: bytes) -> Extracted:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise AttachmentError("PDF je chráněné heslem.")
        total = len(reader.pages)
    except AttachmentError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as e:
        raise AttachmentError(f"PDF nejde otevřít: {e}") from e

    out = Extracted(kind="pdf", pages=total)
    parts = []
    textless: list[int] = []  # 0-based pages without a text layer (scans, photos)
    for i, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # broken page content stream
            text = ""
        if len(text) < MIN_PAGE_TEXT:
            textless.append(i)
        if text:
            parts.append(f"--- strana {i + 1} ---\n{text}")
    out.text = "\n\n".join(parts)
    if total > MAX_PDF_PAGES:
        out.notes.append(f"Přečteno jen prvních {MAX_PDF_PAGES} z {total} stran.")

    # Scanned pages: render the first few as images so the model can read them.
    if textless:
        shown_pages = textless[:MAX_RENDERED_PAGES]
        rendered = _render_pdf_pages(data, shown_pages)
        if rendered:
            out.images = rendered
            numbers = ", ".join(str(i + 1) for i in shown_pages[: len(rendered)])
            note = f"Strany bez textu (nejspíš sken) přikládám jako obrázek: {numbers}."
            if len(textless) > len(rendered):
                note += f" Dalších {len(textless) - len(rendered)} stran bez textu jsem nepřiložil."
            out.notes.append(note)
    return out


def _render_pdf_pages(data: bytes, pages: list[int]) -> list[tuple[bytes, str]]:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
    except Exception:
        return []
    images = []
    try:
        for i in pages:
            page = pdf[i]
            width, height = page.get_size()
            scale = min(IMAGE_MAX_SIDE / max(width, height, 1), 3.0)
            pil = page.render(scale=scale).to_pil()
            images.append(_encode_image(pil))
    except Exception:
        pass
    finally:
        pdf.close()
    return images


def _zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise AttachmentError("Soubor je poškozený (není to platný .docx/.xlsx).") from e
    if sum(i.file_size for i in zf.infolist()) > MAX_ZIP_MEMBER_BYTES:
        raise AttachmentError("Soubor je po rozbalení moc velký.")
    return zf


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_paragraph(p: ElementTree.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == _W + "t" and el.text:
            out.append(el.text)
        elif el.tag == _W + "tab":
            out.append("\t")
        elif el.tag in (_W + "br", _W + "cr"):
            out.append("\n")
    return "".join(out)


def _docx(data: bytes) -> Extracted:
    zf = _zip(data)
    try:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    except KeyError as e:
        raise AttachmentError("V souboru chybí text dokumentu (není to .docx?).") from e
    except ElementTree.ParseError as e:
        raise AttachmentError("Dokument nejde přečíst (poškozené XML).") from e
    body = root.find(_W + "body")
    lines: list[str] = []
    for block in body if body is not None else []:
        if block.tag == _W + "p":
            lines.append(_docx_paragraph(block))
        elif block.tag == _W + "tbl":
            for row in block.iter(_W + "tr"):
                cells = [" ".join(_docx_paragraph(p) for p in tc.iter(_W + "p")).strip() for tc in row.iter(_W + "tc")]
                lines.append(" | ".join(cells))
            lines.append("")
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return Extracted(kind="docx", text=text.strip())


def _xlsx(data: bytes) -> Extracted:
    _zip(data)  # size check before openpyxl unpacks anything
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise AttachmentError(f"Tabulku nejde otevřít: {e}") from e
    out = Extracted(kind="xlsx")
    parts = []
    try:
        for ws in wb.worksheets:
            rows = []
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= MAX_SHEET_ROWS:
                    out.notes.append(f"List „{ws.title}“: přečteno jen prvních {MAX_SHEET_ROWS} řádků.")
                    break
                cells = ["" if v is None else str(v) for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if cells:
                    rows.append(" | ".join(cells))
            parts.append(f"--- list: {ws.title} ---\n" + ("\n".join(rows) if rows else "(prázdný)"))
    finally:
        wb.close()
    out.text = "\n\n".join(parts)
    return out


def _encode_image(img) -> tuple[bytes, str]:
    from PIL import Image

    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def _image(data: bytes) -> tuple[bytes, str]:
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        img.seek(0)  # first frame of an animation
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise AttachmentError(f"Obrázek nejde otevřít: {e}") from e
    return _encode_image(img)


def _email(data: bytes | EmailMessage) -> Extracted:
    from . import mail

    msg = data if isinstance(data, EmailMessage) else mail.parse_message(data)
    body, _source = mail.extract_body(msg)
    header = "\n".join(
        f"{name}: {value}"
        for name, value in (
            ("Od", ", ".join(mail.addresses(msg, "From"))),
            ("Komu", ", ".join(mail.addresses(msg, "To"))),
            ("Datum", mail.date_iso(msg) or ""),
            ("Předmět", mail.header(msg, "Subject") or ""),
        )
        if value
    )
    out = Extracted(kind="email", text=f"{header}\n\n{body}")
    inner = mail.extract_attachments(msg)
    if inner:
        out.notes.append("Přiložený e-mail má vlastní přílohy: " + ", ".join(a["filename"] for a in inner))
    return out

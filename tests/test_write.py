"""Attachments, organising and sending, over the fake IMAP/SMTP servers from test_mail."""

import base64
import io
import json
import zipfile
from email.message import EmailMessage
from email import policy

import pytest
from PIL import Image

from seznam_mail_mcp import attachments, mail
from seznam_mail_mcp.attachments import AttachmentError
from seznam_mail_mcp.config import ToolOptions
from seznam_mail_mcp.mail import MailAccount
from seznam_mail_mcp.server import build_server

from .test_mail import PLAIN, FakeIMAP, FakeSMTP, call, client  # noqa: F401  (client is a fixture)

# ------------------------------------------------------------ sample files


def make_pdf(text: str | None) -> bytes:
    """Minimal one-page PDF; text=None gives a page with no text layer (like a scan)."""
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode() if text else b"0 0 1 rg 72 72 200 200 re f"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def make_docx() -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    xml = (
        f"<w:document {w}><w:body>"
        "<w:p><w:r><w:t>Smlouva o dílo</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cena</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>1000 Kč</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Faktury"
    ws.append(["Číslo", "Částka"])
    ws.append(["2025-01", 1500])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_png(width=3000, height=1000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, format="PNG")
    return buf.getvalue()


def with_attachments(*files: tuple[str, str, bytes]) -> bytes:
    msg = EmailMessage()
    msg["From"] = "petr@example.cz"
    msg["To"] = "me@seznam.cz"
    msg["Subject"] = "Přílohy"
    msg["Message-ID"] = "<att@example.cz>"
    msg.set_content("V příloze.")
    for name, ctype, data in files:
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes(policy=policy.SMTP)


def texts(res) -> list[str]:
    return [c.text for c in res.content if c.type == "text"]


# ------------------------------------------------------------ attachments (pure)


def test_extract_pdf_text():
    out = attachments.extract(make_pdf("Faktura c. 2025-001 za rijen"), "application/pdf", "f.pdf")
    assert out.kind == "pdf" and out.pages == 1 and "Faktura c. 2025-001" in out.text and not out.images


def test_extract_scanned_pdf_is_rendered():
    out = attachments.extract(make_pdf(None), "application/pdf", "scan.pdf")
    assert out.images and out.images[0][1] in ("image/jpeg", "image/png")
    assert any("sken" in n for n in out.notes)


def test_extract_docx_and_xlsx_by_extension():
    docx = attachments.extract(make_docx(), "application/octet-stream", "smlouva.docx")
    assert docx.text == "Smlouva o dílo\nCena | 1000 Kč"
    xlsx = attachments.extract(make_xlsx(), "application/octet-stream", "faktury.xlsx")
    assert "--- list: Faktury ---" in xlsx.text and "2025-01 | 1500" in xlsx.text


def test_extract_image_is_downscaled():
    out = attachments.extract(make_png(), "image/png", "a.png")
    data, mime = out.images[0]
    assert mime == "image/jpeg"
    assert max(Image.open(io.BytesIO(data)).size) == attachments.IMAGE_MAX_SIDE


def test_extract_text_cp1250_and_unsupported():
    assert attachments.extract("Příliš žluťoučký".encode("cp1250"), "text/plain", "a.txt").text == "Příliš žluťoučký"
    with pytest.raises(AttachmentError, match="neumím"):
        attachments.extract(b"MZ", "application/x-msdownload", "virus.exe")


def test_zip_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_ZIP_MEMBER_BYTES", 100)
    with pytest.raises(AttachmentError, match="moc velký"):
        attachments.extract(make_docx(), "application/octet-stream", "a.docx")


# ------------------------------------------------------------ read_attachment tool


async def test_tool_read_attachment_text_and_image(client):
    FakeIMAP.boxes["INBOX"][3] = with_attachments(
        ("smlouva.docx", "application/octet-stream", make_docx()),
        ("foto.png", "image/png", make_png(800, 600)),
    )
    res = await call(client, "read_email", {"uid": 3})
    assert [(a["index"], a["filename"]) for a in res.structured_content["attachments"]] == [(1, "smlouva.docx"), (2, "foto.png")]

    res = await call(client, "read_attachment", {"uid": 3, "index": 1})
    meta = json.loads(texts(res)[0])
    assert meta["kind"] == "docx" and meta["filename"] == "smlouva.docx" and "notice" in meta
    assert "Cena | 1000 Kč" in texts(res)[1]

    res = await call(client, "read_attachment", {"uid": 3, "index": 2})
    images = [c for c in res.content if c.type == "image"]
    assert len(images) == 1 and Image.open(io.BytesIO(base64.b64decode(images[0].data))).size == (800, 600)


async def test_tool_read_attachment_attached_email(client):
    inner = EmailMessage()
    inner["From"] = "jana@example.cz"
    inner["Subject"] = "Původní zpráva"
    inner.set_content("Obsah přeposlaného e-mailu.")
    outer = EmailMessage()
    outer["From"] = "petr@example.cz"
    outer["Subject"] = "Fwd"
    outer.set_content("Přeposílám.")
    outer.add_attachment(inner)
    FakeIMAP.boxes["INBOX"][4] = outer.as_bytes(policy=policy.SMTP)

    res = await call(client, "read_attachment", {"uid": 4, "index": 1})
    assert "Předmět: Původní zpráva" in texts(res)[1] and "přeposlaného" in texts(res)[1]


async def test_tool_read_attachment_bad_index(client):
    res = await call(client, "read_attachment", {"uid": 1, "index": 5})
    assert res.is_error and "1 příloh" in res.content[0].text


# ------------------------------------------------------------ organising


async def test_tool_mark_emails(client):
    res = await call(client, "mark_emails", {"uids": [1, 2, 999], "read": False, "flagged": True})
    data = res.structured_content
    assert data["updated"] == [{"uid": 1, "unread": True, "flagged": True}, {"uid": 2, "unread": True, "flagged": True}]
    assert data["not_found"] == [999]
    res = await call(client, "mark_emails", {"uids": [1]})
    assert res.is_error


async def test_tool_move_by_role_and_trash(client):
    res = await call(client, "move_emails", {"uids": [2], "target_folder": "archive"})
    data = res.structured_content
    assert data["to_folder"] == "archive" and data["moved"] == [{"uid": 2, "new_uid": 100}]
    assert 2 not in FakeIMAP.boxes["INBOX"] and 100 in FakeIMAP.boxes["archive"]

    res = await call(client, "trash_emails", {"uids": [1]})
    assert res.structured_content["to_folder"] == "Koš" and 1 not in FakeIMAP.boxes["INBOX"]
    assert FakeIMAP.flags[("Koš", 100)] == {b"\\Seen"}  # flags travel with the message

    res = await call(client, "trash_emails", {"uids": [100], "folder": "Koš"})
    assert res.is_error and "Trvalé mazání" in res.content[0].text
    res = await call(client, "move_emails", {"uids": [100], "target_folder": "Neexistuje", "folder": "Koš"})
    assert res.is_error and "neexistuje" in res.content[0].text


async def test_write_tools_can_be_disabled():
    async def resolve(_ctx):
        return MailAccount("me@seznam.cz", "ok")

    server = build_server(resolve, ToolOptions(enable_organize=False, enable_send=False))
    from mcp import Client

    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"list_folders", "search_emails", "read_email", "read_thread", "read_attachment", "create_draft"}


# ------------------------------------------------------------ sending


async def test_tool_send_reply(client):
    res = await call(client, "send_email", {"body": "Díky!", "reply_to_uid": 1, "bcc": ["tajny@example.cz"]})
    data = res.structured_content
    assert data["sent"] and data["subject"] == "Re: Faktura za říjen" and data["saved_to"] == "Odeslané"
    assert data["warnings"] == []

    msg, sender, rcpts = FakeSMTP.sent[0]
    assert sender == "me@seznam.cz" and rcpts == ["petr@example.cz", "tajny@example.cz"]
    assert msg["In-Reply-To"] == "<a1@example.cz>" and "Bcc" not in msg and b"tajny" not in msg.as_bytes()

    folder, raw, flags = FakeIMAP.appended[0]
    assert folder == "Odeslané" and b"\\Seen" in flags
    assert b"\\Answered" in FakeIMAP.flags[("INBOX", 1)]


async def test_tool_send_validates_before_sending(client):
    res = await call(client, "send_email", {"body": "x", "to": ["neni-adresa"], "subject": "a"})
    assert res.is_error and "Neplatná adresa" in res.content[0].text
    many = [f"a{i}@example.cz" for i in range(mail.MAX_RECIPIENTS + 1)]
    res = await call(client, "send_email", {"body": "x", "to": many, "subject": "a"})
    assert res.is_error and "nejvýš" in res.content[0].text
    res = await call(client, "send_email", {"body": "x", "to": ["a@example.cz"]})
    assert res.is_error and "předmět" in res.content[0].text
    assert FakeSMTP.sent == []


async def test_tool_send_draft(client):
    await call(client, "create_draft", {"body": "Návrh", "to": ["petr@example.cz"], "cc": ["jana@example.cz"], "subject": "Ahoj"})
    draft_uid = max(FakeIMAP.boxes["Koncepty"])
    res = await call(client, "send_draft", {"uid": draft_uid})
    data = res.structured_content
    assert data["sent"] and data["recipients"] == 2 and data["warnings"] == []
    assert FakeSMTP.sent[0][2] == ["petr@example.cz", "jana@example.cz"]
    assert FakeIMAP.boxes["Koncepty"] == {}  # draft removed after sending
    assert any(f == "Odeslané" for f, _raw, _fl in FakeIMAP.appended)


async def test_send_draft_refuses_normal_message(client):
    res = await call(client, "send_draft", {"uid": 1, "folder": "INBOX"})
    assert res.is_error and "koncept" in res.content[0].text
    assert FakeSMTP.sent == []


async def test_send_daily_limit(monkeypatch, client):
    FakeIMAP.boxes.setdefault("INBOX", {})

    async def resolve(_ctx):
        return MailAccount("me@seznam.cz", "ok")

    server = build_server(resolve, ToolOptions(send_daily_limit=2))
    args = {"body": "x", "to": ["a@example.cz"], "subject": "s"}
    assert not (await call(server, "send_email", args)).is_error
    assert not (await call(server, "send_email", args)).is_error
    res = await call(server, "send_email", args)
    assert res.is_error and "limit" in res.content[0].text
    assert len(FakeSMTP.sent) == 2


# ------------------------------------------------------------ fixes after the first live test


def test_clean_text_drops_binary_noise_keeps_languages():
    text, dropped = attachments.clean_text(
        "Vyúčtování\nCelkem: 49,90 €\nДоговір оренди\n\x01ÿĀ漢字ꙮ⣿⣿⣿⣷⣄⡀⢀⣤⣶\nkůň"
    )
    assert text == "Vyúčtování\nCelkem: 49,90 €\nДоговір оренди\nkůň" and dropped == 1
    qr_ascii = "#y#R;?gApw!)?zzJ0{B!`$n0/#11\nCwUq1+-cVCqK2Sl=y;Wgwg'@?ckM\x7fcChE"
    real = (
        "IČ: 649 49 681, DIČ: CZ64949681\nDPH (21%) 123,45 Kč\nhttps://www.example.cz/faktura?id=1\n"
        "petr.novak@example.cz\nAdresa trvalého bydliště: ………………………………………\nV (město)______________, dne: ________\n"
        "_______________________"
    )
    text, dropped = attachments.clean_text(real + "\n" + qr_ascii)
    assert text == real and dropped == 2


def test_count_attachments_from_bodystructure():
    from .test_mail import bodystructure, parse

    raw = with_attachments(("a.pdf", "application/pdf", b"%PDF"), ("b.png", "image/png", make_png(10, 10)))
    assert mail.count_attachments(bodystructure(parse(raw))) == 2
    assert mail.count_attachments(bodystructure(parse(with_attachments()))) == 0
    assert mail.count_attachments(None) == 0

    # HTML newsletter: logo embedded in multipart/related is not an attachment, the PDF is
    html = EmailMessage()
    html.set_content("<p>Faktura</p><img src='cid:logo'>", subtype="html")
    html.add_related(make_png(10, 10), maintype="image", subtype="png", cid="<logo>", filename="logo.png")
    html.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="f.pdf")
    msg = parse(html.as_bytes(policy=policy.SMTP))
    assert mail.count_attachments(bodystructure(msg)) == len(mail.extract_attachments(msg)) == 1

    # whole message is multipart/related: embedded images are listed (and counted) as attachments
    only_related = EmailMessage()
    only_related.set_content("<p>Platba</p>", subtype="html")
    only_related.add_related(make_png(10, 10), maintype="image", subtype="png", cid="<logo>", filename="logo.png")
    msg = parse(only_related.as_bytes(policy=policy.SMTP))
    assert mail.count_attachments(bodystructure(msg)) == len(mail.extract_attachments(msg)) == 1


async def test_move_finds_new_uid_without_copyuid(monkeypatch, client):
    original = FakeIMAP.move

    def move_without_copyuid(self, uids, folder):
        original(self, uids, folder)
        return b"Move completed"  # Seznam's COPYUID arrives untagged and is lost

    monkeypatch.setattr(FakeIMAP, "move", move_without_copyuid)
    res = await call(client, "trash_emails", {"uids": [2]})
    assert res.structured_content["moved"] == [{"uid": 2, "new_uid": 100}]


async def test_has_attachment_scan_note(monkeypatch, client):
    monkeypatch.setattr(mail, "ATTACHMENT_SCAN_LIMIT", 1)
    res = await call(client, "search_emails", {"has_attachment": True})
    data = res.structured_content
    assert data["messages"] == [] and "jen u 1" in data["note"]  # newest (uid 2) has none, uid 1 not scanned

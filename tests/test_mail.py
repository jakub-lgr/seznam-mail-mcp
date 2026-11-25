from datetime import date
from email import policy
from email.parser import BytesParser

import pytest
from mcp import Client

from seznam_mail_mcp import mail
from seznam_mail_mcp.htmltext import html_to_text
from seznam_mail_mcp.mail import MailAccount, build_draft, build_search_criteria, extract_body
from seznam_mail_mcp.server import build_server

PLAIN = (
    "From: =?utf-8?q?Petr_Nov=C3=A1k?= <petr@example.cz>\r\n"
    "To: me@seznam.cz\r\n"
    "Subject: =?utf-8?q?Faktura_za_=C5=99=C3=ADjen?=\r\n"
    "Date: Mon, 20 Oct 2025 10:00:00 +0200\r\n"
    "Message-ID: <a1@example.cz>\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
    "--XX\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
    "Dobrý den, posílám fakturu.\r\n"
    "--XX\r\nContent-Type: application/pdf; name=f.pdf\r\nContent-Disposition: attachment; filename=f.pdf\r\n"
    "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--XX--\r\n"
).encode()

REPLY = (
    "From: me@seznam.cz\r\nTo: petr@example.cz\r\nSubject: Re: Faktura za rijen\r\n"
    "Date: Tue, 21 Oct 2025 09:00:00 +0200\r\nMessage-ID: <b2@seznam.cz>\r\n"
    "In-Reply-To: <a1@example.cz>\r\nReferences: <a1@example.cz>\r\n"
    "Content-Type: text/html; charset=utf-8\r\n\r\n<p>D&iacute;ky,</p><p>zaplaceno.</p>\r\n"
).encode()


UNRELATED = (
    "From: jana@example.cz\r\nTo: me@seznam.cz\r\nSubject: Faktura za rijen\r\n"
    "Date: Wed, 22 Oct 2025 09:00:00 +0200\r\nMessage-ID: <c3@example.cz>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\nJina faktura.\r\n"
).encode()


def parse(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


# ------------------------------------------------------------ pure functions


def test_html_to_text():
    html = "<html><style>p{}</style><body><p>Ahoj&nbsp;světe</p><ul><li>a</li></ul><a href='https://x.cz'>odkaz</a></body>"
    text = html_to_text(html)
    assert "p{}" not in text
    assert "Ahoj světe" in text
    assert "- a" in text
    assert "odkaz (https://x.cz)" in text


def test_extract_body_plain_and_attachments():
    msg = parse(PLAIN)
    assert extract_body(msg) == ("Dobrý den, posílám fakturu.", "plain")
    assert mail.extract_attachments(msg) == [{"filename": "f.pdf", "content_type": "application/pdf", "size": 5}]


def test_extract_body_html():
    assert extract_body(parse(REPLY)) == ("Díky,\n\nzaplaceno.", "html")


def test_search_criteria_charset():
    crit, charset = build_search_criteria(from_="petr", since=date(2025, 1, 1), unread_only=True)
    assert crit == ["FROM", "petr", "SINCE", date(2025, 1, 1), "UNSEEN"] and charset is None
    crit, charset = build_search_criteria(subject="Pozvánka", from_="Fusková")
    assert crit == ["FROM", "Fuskova", "SUBJECT", "Pozvanka"] and charset is None
    crit, charset = build_search_criteria(text="Zařazení")
    assert crit == ["TEXT", "Zařazení"] and charset == "UTF-8"
    assert build_search_criteria() == (["ALL"], None)


def test_build_reply_draft():
    draft = build_draft(sender="me@seznam.cz", to=["petr@example.cz"], subject="", body="Díky", reply_to_msg=parse(PLAIN))
    assert draft["Subject"] == "Re: Faktura za říjen"
    assert draft["In-Reply-To"] == "<a1@example.cz>"
    assert draft["References"] == "<a1@example.cz>"
    assert draft["Message-ID"].endswith("@seznam.cz>")


def test_account_repr_hides_password():
    assert "tajne" not in repr(MailAccount("a@seznam.cz", "tajne"))


# ------------------------------------------------------------ tools over a fake IMAP server


class FakeIMAP:
    boxes: dict
    appended: list

    def __init__(self, host, port=None, ssl=True, timeout=None):
        self.selected = None

    def login(self, user, password):
        if password != "ok":
            raise mail.LoginError("bad")

    def logout(self):
        pass

    def list_folders(self):
        return [((b"\\HasNoChildren",), b"/", "INBOX"), ((b"\\Sent",), b"/", "Odeslané"), ((b"\\Drafts",), b"/", "Koncepty")]

    def folder_status(self, name, items):
        return {b"MESSAGES": len(self.boxes[name]), b"UNSEEN": 1}

    def select_folder(self, name, readonly=False):
        if name not in self.boxes:
            raise mail.IMAPClientError("no such folder")
        self.selected = name

    def search(self, criteria, charset=None):
        box = self.boxes[self.selected]
        if criteria[:2] == ["HEADER", "Message-ID"]:
            return [u for u, raw in box.items() if f"Message-ID: {criteria[2]}".encode() in raw]
        if criteria[:1] == ["HEADER"]:
            raise mail.IMAPClientError("socket error: EOF")  # what Seznam does for References/In-Reply-To
        words = [criteria[i + 1] for i, c in enumerate(criteria) if c == "SUBJECT"]
        out = []
        for u, raw in box.items():
            subj = mail.fold_diacritics(str(parse(raw)["Subject"])).lower()
            if all(w.lower() in subj.split() for w in words):
                out.append(u)
        return out

    def fetch(self, uids, items):
        box = self.boxes[self.selected]
        out = {}
        for uid in uids:
            raw = box[uid]
            body = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if any("HEADER" in i for i in items) else raw
            out[uid] = {b"BODY[]": body, b"FLAGS": (b"\\Seen",) if uid == 1 else (), b"RFC822.SIZE": len(raw)}
        return out

    def append(self, folder, msg, flags=(), msg_time=None):
        self.appended.append((folder, msg, flags))
        return b"[APPENDUID 1 77] done"


@pytest.fixture
def client(monkeypatch):
    FakeIMAP.boxes = {"INBOX": {1: PLAIN, 2: UNRELATED}, "Odeslané": {5: REPLY}, "Koncepty": {}}
    FakeIMAP.appended = []
    monkeypatch.setattr(mail, "IMAPClient", FakeIMAP)

    async def resolve(_ctx):
        return MailAccount("me@seznam.cz", "ok")

    return build_server(resolve)


async def call(server, name, args=None):
    async with Client(server) as c:
        res = await c.call_tool(name, args or {})
    return res


async def test_tool_list_folders(client):
    res = await call(client, "list_folders")
    roles = {f["name"]: f["role"] for f in res.structured_content["folders"]}
    assert roles == {"INBOX": "inbox", "Odeslané": "sent", "Koncepty": "drafts"}


async def test_tool_search_and_read(client):
    res = await call(client, "search_emails", {"subject": "faktura"})
    msg = res.structured_content["messages"][-1]
    assert (msg["uid"], msg["subject"], msg["unread"]) == (1, "Faktura za říjen", False)
    assert msg["from"] == ["Petr Novák <petr@example.cz>"]

    res = await call(client, "read_email", {"uid": 1})
    data = res.structured_content
    assert data["body"] == "Dobrý den, posílám fakturu." and "notice" in data
    assert data["attachments"][0]["filename"] == "f.pdf"


async def test_tool_read_thread_includes_sent(client):
    res = await call(client, "read_thread", {"uid": 1})
    subjects = [m["subject"] for m in res.structured_content["messages"]]
    assert subjects == ["Faktura za říjen", "Re: Faktura za rijen"]  # UNRELATED (same subject) excluded


async def test_tool_read_thread_from_reply(client):
    res = await call(client, "read_thread", {"uid": 5, "folder": "Odeslané"})
    assert [(m["folder"], m["uid"]) for m in res.structured_content["messages"]] == [("INBOX", 1), ("Odeslané", 5)]


def test_base_subject_and_words():
    assert mail.base_subject("Re: FW: Odp: Faktura") == "Faktura"
    assert mail.subject_search_words("Re: Pozvánka na pohovor") == ["Pozvanka", "pohovor"]


async def test_tool_create_reply_draft(client):
    res = await call(client, "create_draft", {"body": "Díky!", "reply_to_uid": 1})
    data = res.structured_content
    assert data["saved_to"] == "Koncepty" and data["uid"] == 77
    assert data["to"] == ["Petr Novák <petr@example.cz>"]
    folder, raw, flags = FakeIMAP.appended[0]
    assert b"\\Draft" in flags and b"In-Reply-To: <a1@example.cz>" in raw


async def test_tool_errors_are_readable(client):
    res = await call(client, "read_email", {"uid": 1, "folder": "Neexistuje"})
    assert res.is_error and "Neexistuje" in res.content[0].text
    res = await call(client, "search_emails", {"since": "včera"})
    assert res.is_error and "RRRR-MM-DD" in res.content[0].text

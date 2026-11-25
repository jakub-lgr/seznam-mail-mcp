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
    assert mail.extract_attachments(msg) == [{"index": 1, "filename": "f.pdf", "content_type": "application/pdf", "size": 5}]


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
    crit, _ = build_search_criteria(subject="[TEST] Nový koncept", from_="Petr <petr@example.cz>")
    assert crit == ["FROM", "Petr", "FROM", "petr@example.cz", "SUBJECT", "TEST", "SUBJECT", "Novy", "SUBJECT", "koncept"]
    assert build_search_criteria(subject="!!!") == (["ALL"], None)


def test_build_reply_draft():
    draft = build_draft(sender="me@seznam.cz", to=["petr@example.cz"], subject="", body="Díky", reply_to_msg=parse(PLAIN))
    assert draft["Subject"] == "Re: Faktura za říjen"
    assert draft["In-Reply-To"] == "<a1@example.cz>"
    assert draft["References"] == "<a1@example.cz>"
    assert draft["Message-ID"].endswith("@seznam.cz>")


def test_account_repr_hides_password():
    assert "tajne" not in repr(MailAccount("a@seznam.cz", "tajne"))


# ------------------------------------------------------------ tools over a fake IMAP server


def bodystructure(msg):
    """Roughly what an IMAP server returns for BODYSTRUCTURE (enough for count_attachments)."""
    from imapclient.response_types import BodyData

    if msg.is_multipart() and msg.get_content_type() != "message/rfc822":
        return BodyData(([bodystructure(p) for p in msg.iter_parts()], msg.get_content_subtype().encode()))
    maintype, subtype = msg.get_content_maintype().encode(), msg.get_content_subtype().encode()
    params = tuple(x for k, v in msg.get_params()[1:] for x in (k.encode(), str(v).encode())) or None
    disp = msg.get_content_disposition()
    disposition = (disp.encode(), (b"filename", msg.get_filename().encode()) if msg.get_filename() else None) if disp else None
    base = (maintype, subtype, params, None, None, b"7bit", 10)
    if maintype == b"text":
        return BodyData(base + (1, None, disposition))
    if (maintype, subtype) == (b"message", b"rfc822"):
        return BodyData(base + (None, None, 1, None, disposition))
    return BodyData(base + (None, disposition))


class FakeIMAP:
    boxes: dict
    appended: list
    flags: dict  # (folder, uid) -> set of flags
    capabilities = (b"MOVE", b"UIDPLUS")

    def __init__(self, host, port=None, ssl=True, timeout=None):
        self.selected = None
        self.readonly = True

    def login(self, user, password):
        if password != "ok":
            raise mail.LoginError("bad")

    def logout(self):
        pass

    def noop(self):
        pass

    def list_folders(self):
        return [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\Sent",), b"/", "Odeslané"),
            ((b"\\Drafts",), b"/", "Koncepty"),
            ((b"\\Trash",), b"/", "Koš"),
            ((), b"/", "archive"),
        ]

    def has_capability(self, cap):
        return cap.encode() in self.capabilities

    def _writable(self):
        assert not self.readonly, "write on a read-only SELECT"

    def add_flags(self, uids, flags, silent=False):
        self._writable()
        for u in uids:
            self.flags.setdefault((self.selected, u), set()).update(flags)

    def remove_flags(self, uids, flags, silent=False):
        self._writable()
        for u in uids:
            self.flags.setdefault((self.selected, u), set()).difference_update(flags)

    def _put(self, folder, raw, flags=()):
        box = self.boxes[folder]
        uid = max(box, default=99) + 1
        box[uid] = raw
        self.flags[(folder, uid)] = set(flags)
        return uid

    def move(self, uids, folder):
        self._writable()
        new = []
        for u in uids:
            new.append(self._put(folder, self.boxes[self.selected].pop(u), self.flags.pop((self.selected, u), set())))
        old_set, new_set = ",".join(map(str, uids)), ",".join(map(str, new))
        return f"[COPYUID 1 {old_set} {new_set}] Move completed".encode()

    def uid_expunge(self, uids):
        self._writable()
        for u in uids:
            if b"\\Deleted" in self.flags.get((self.selected, u), set()):
                self.boxes[self.selected].pop(u)

    def folder_status(self, name, items):
        return {b"MESSAGES": len(self.boxes[name]), b"UNSEEN": 1}

    def select_folder(self, name, readonly=False):
        if name not in self.boxes:
            raise mail.IMAPClientError("no such folder")
        self.selected = name
        self.readonly = readonly

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
            if uid not in box:
                continue
            raw = box[uid]
            body = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if any("HEADER" in i for i in items) else raw
            flags = tuple(self.flags.get((self.selected, uid), ()))
            out[uid] = {b"BODY[]": body, b"FLAGS": flags, b"RFC822.SIZE": len(raw)}
            if "BODYSTRUCTURE" in items:
                out[uid][b"BODYSTRUCTURE"] = bodystructure(parse(raw))
        return out

    def append(self, folder, msg, flags=(), msg_time=None):
        self.appended.append((folder, msg, flags))
        self._put(folder, msg, flags)
        return b"[APPENDUID 1 77] done"


class FakeSMTP:
    sent: list

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if password != "ok":
            raise mail.smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSMTP.sent.append((msg, from_addr, list(to_addrs)))
        return {}


@pytest.fixture
def client(monkeypatch):
    FakeIMAP.boxes = {"INBOX": {1: PLAIN, 2: UNRELATED}, "Odeslané": {5: REPLY}, "Koncepty": {}, "Koš": {}, "archive": {}}
    FakeIMAP.flags = {("INBOX", 1): {b"\\Seen"}}
    FakeIMAP.appended = []
    FakeSMTP.sent = []
    monkeypatch.setattr(mail, "IMAPClient", FakeIMAP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)

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
    assert roles == {"INBOX": "inbox", "Odeslané": "sent", "Koncepty": "drafts", "Koš": "trash", "archive": "archive"}


async def test_tool_search_and_read(client):
    res = await call(client, "search_emails", {"subject": "faktura"})
    msg = res.structured_content["messages"][-1]
    assert (msg["uid"], msg["subject"], msg["unread"]) == (1, "Faktura za říjen", False)
    assert msg["from"] == ["Petr Novák <petr@example.cz>"] and msg["attachments"] == 1

    res = await call(client, "search_emails", {"has_attachment": True})
    assert [m["uid"] for m in res.structured_content["messages"]] == [1]

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

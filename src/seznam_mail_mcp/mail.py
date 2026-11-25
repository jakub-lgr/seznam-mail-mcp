"""IMAP access to a Seznam.cz mailbox (synchronous; call from a worker thread).

Messages are always read with BODY.PEEK, so reading never marks them as seen.
The only write operation is appending a draft to the Drafts folder.
"""

from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from typing import Any, Iterator

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .htmltext import html_to_text

IMAP_HOST = "imap.seznam.cz"
IMAP_PORT = 993
TIMEOUT = 30

HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES"
_HEADER_FETCH = f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]"

# Folder roles: SPECIAL-USE flag first, then common names (case-insensitive).
_ROLE_FLAGS = {
    "drafts": b"\\Drafts",
    "sent": b"\\Sent",
    "trash": b"\\Trash",
    "spam": b"\\Junk",
    "archive": b"\\Archive",
}
_ROLE_NAMES = {
    "drafts": ["drafts", "koncepty", "draft", "rozepsané"],
    "sent": ["sent", "odeslané", "odeslane", "sent items", "sent messages"],
    "trash": ["trash", "koš", "kos", "deleted", "deleted items"],
    "spam": ["spam", "junk", "nevyžádané", "hromadné"],
    "archive": ["archive", "archiv"],
}


class MailError(Exception):
    """User-facing mail error (message is safe to show)."""


@dataclass(frozen=True)
class MailAccount:
    email: str
    password: str
    imap_host: str = IMAP_HOST
    imap_port: int = IMAP_PORT

    def __repr__(self) -> str:  # never leak the password into logs
        return f"MailAccount({self.email!r})"


@contextmanager
def connect(account: MailAccount) -> Iterator[IMAPClient]:
    try:
        client = IMAPClient(account.imap_host, port=account.imap_port, ssl=True, timeout=TIMEOUT)
    except OSError as e:
        raise MailError(f"Nelze se připojit k {account.imap_host}: {e}") from e
    try:
        try:
            client.login(account.email, account.password)
        except LoginError as e:
            raise MailError(
                "Přihlášení k e-mailu selhalo. Zkontroluj adresu, heslo pro aplikaci "
                "a že je na Seznamu povolený přístup přes IMAP."
            ) from e
        yield client
    except IMAPClientError as e:
        raise MailError(f"Chyba IMAP: {e}") from e
    finally:
        try:
            client.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- helpers


def _parse(raw: bytes, headers_only: bool = False) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw, headersonly=headers_only)  # type: ignore[return-value]


def _hdr(msg: Message, name: str) -> str | None:
    try:
        value = msg.get(name)
    except Exception:  # malformed header
        value = msg.get_all(name, [None])[0] if hasattr(msg, "get_all") else None
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def _addresses(msg: Message, name: str) -> list[str]:
    raw = _hdr(msg, name)
    if not raw:
        return []
    out = []
    for display, addr in getaddresses([raw]):
        if addr:
            out.append(f"{display} <{addr}>" if display else addr)
    return out


def _date_iso(msg: Message) -> str | None:
    raw = _hdr(msg, "Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return raw


def _body_key(data: dict) -> bytes | None:
    for key in data:
        if isinstance(key, bytes) and key.startswith(b"BODY["):
            return data[key]
    return None


def _summary(uid: int, data: dict, folder: str) -> dict[str, Any]:
    msg = _parse(_body_key(data) or b"", headers_only=True)
    flags = [f.decode() if isinstance(f, bytes) else str(f) for f in data.get(b"FLAGS", ())]
    return {
        "folder": folder,
        "uid": uid,
        "date": _date_iso(msg),
        "from": _addresses(msg, "From"),
        "to": _addresses(msg, "To"),
        "subject": _hdr(msg, "Subject") or "",
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
        "size": data.get(b"RFC822.SIZE"),
    }


def _text_of(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
    except Exception:
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return content if isinstance(content, str) else ""


def extract_body(msg: EmailMessage) -> tuple[str, str]:
    """Return (text, source) where source is 'plain', 'html' or 'none'."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", "none"
    text = _text_of(part)
    if part.get_content_type() == "text/html":
        return html_to_text(text), "html"
    if not text.strip():  # empty plain part, try html
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            return html_to_text(_text_of(html_part)), "html"
    return text.strip(), "plain"


def extract_attachments(msg: EmailMessage) -> list[dict[str, Any]]:
    out = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        out.append(
            {
                "filename": part.get_filename() or "(bez názvu)",
                "content_type": part.get_content_type(),
                "size": len(payload),
            }
        )
    return out


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _select(client: IMAPClient, folder: str) -> None:
    try:
        client.select_folder(folder, readonly=True)
    except IMAPClientError as e:
        raise MailError(f"Složka '{folder}' neexistuje nebo ji nejde otevřít. Použij list_folders.") from e


def _folder_role(flags: tuple, name: str) -> str | None:
    if name.upper() == "INBOX":
        return "inbox"
    for role, flag in _ROLE_FLAGS.items():
        if flag in flags:
            return role
    lowered = name.lower().rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    for role, names in _ROLE_NAMES.items():
        if lowered in names:
            return role
    return None


def find_folder(client: IMAPClient, role: str) -> str | None:
    folders = [(flags, name) for flags, _delim, name in client.list_folders()]
    for flags, name in folders:  # special-use flag wins
        if _ROLE_FLAGS.get(role) in flags:
            return name
    for flags, name in folders:
        if _folder_role(flags, name) == role:
            return name
    return None


def fold_diacritics(s: str) -> str:
    """'Pozvánka' -> 'Pozvanka'.

    Seznam's IMAP server matches FROM/TO/SUBJECT against an accent-folded word index:
    the ASCII form finds messages with diacritics, the original form finds nothing.
    """
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


# ---------------------------------------------------------------- operations


def verify_login(account: MailAccount) -> None:
    """Raise MailError unless the credentials can log in."""
    with connect(account) as client:
        client.noop()


def list_folders(account: MailAccount) -> list[dict[str, Any]]:
    with connect(account) as client:
        out = []
        for flags, _delim, name in client.list_folders():
            if b"\\Noselect" in flags or b"\\NonExistent" in flags:
                continue
            try:
                st = client.folder_status(name, [b"MESSAGES", b"UNSEEN"])
                total, unread = st.get(b"MESSAGES"), st.get(b"UNSEEN")
            except IMAPClientError:
                total = unread = None
            out.append({"name": name, "role": _folder_role(flags, name), "total": total, "unread": unread})
        return out


def build_search_criteria(
    *,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    since: date | None = None,
    before: date | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
) -> tuple[list[Any], str | None]:
    criteria: list[Any] = []
    for key, value in (("FROM", from_), ("TO", to), ("SUBJECT", subject)):
        if value:
            criteria += [key, fold_diacritics(value)]
    if text:  # TEXT works with diacritics on Seznam
        criteria += ["TEXT", text]
    if since:
        criteria += ["SINCE", since]
    if before:
        criteria += ["BEFORE", before]
    if unread_only:
        criteria.append("UNSEEN")
    if flagged_only:
        criteria.append("FLAGGED")
    if not criteria:
        criteria = ["ALL"]
    charset = None if all(_is_ascii(c) for c in criteria if isinstance(c, str)) else "UTF-8"
    return criteria, charset


def search(account: MailAccount, folder: str, limit: int, **filters: Any) -> dict[str, Any]:
    criteria, charset = build_search_criteria(**filters)
    with connect(account) as client:
        _select(client, folder)
        try:
            uids = client.search(criteria, charset=charset)
        except IMAPClientError as e:
            raise MailError(f"Hledání selhalo: {e}") from e
        uids = sorted(uids, reverse=True)
        page = uids[:limit]
        data = client.fetch(page, [_HEADER_FETCH, "FLAGS", "RFC822.SIZE"]) if page else {}
        messages = [_summary(uid, data[uid], folder) for uid in page if uid in data]
        return {"folder": folder, "total_matches": len(uids), "returned": len(messages), "messages": messages}


def _fetch_full(client: IMAPClient, uid: int) -> tuple[EmailMessage, list]:
    data = client.fetch([uid], ["BODY.PEEK[]", "FLAGS"])
    if uid not in data:
        raise MailError(f"Zpráva s UID {uid} nebyla nalezena.")
    return _parse(_body_key(data[uid]) or b""), list(data[uid].get(b"FLAGS", ()))


def message_detail(msg: EmailMessage, folder: str, uid: int, flags: list, max_chars: int) -> dict[str, Any]:
    body, source = extract_body(msg)
    body, truncated = truncate(body, max_chars)
    flag_names = [f.decode() if isinstance(f, bytes) else str(f) for f in flags]
    return {
        "folder": folder,
        "uid": uid,
        "message_id": _hdr(msg, "Message-ID"),
        "date": _date_iso(msg),
        "from": _addresses(msg, "From"),
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        "reply_to": _addresses(msg, "Reply-To"),
        "subject": _hdr(msg, "Subject") or "",
        "unread": "\\Seen" not in flag_names,
        "body_format": source,
        "body": body,
        "body_truncated": truncated,
        "attachments": extract_attachments(msg),
    }


def read_message(account: MailAccount, folder: str, uid: int, max_chars: int) -> dict[str, Any]:
    with connect(account) as client:
        _select(client, folder)
        msg, flags = _fetch_full(client, uid)
        return message_detail(msg, folder, uid, flags, max_chars)


def _message_ids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|odp|pře?d|tr|aw|wg)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_THREAD_CANDIDATE_LIMIT = 300
_SAFE_MESSAGE_ID = re.compile(r"<[A-Za-z0-9.@_+=$%!#&*/?^`|~-]+>")


def base_subject(subject: str) -> str:
    """'Re: Fwd: Faktura' -> 'Faktura'."""
    return _SUBJECT_PREFIX.sub("", subject or "").strip()


def subject_search_words(subject: str, max_words: int = 3) -> list[str]:
    """Longest accent-folded words of the base subject (Seznam indexes whole, accent-folded words)."""
    words = re.findall(r"\w{3,}", fold_diacritics(base_subject(subject)))
    return sorted(dict.fromkeys(words), key=len, reverse=True)[:max_words]


def _thread_ids(msg: Message) -> set[str]:
    ids = set(_message_ids(_hdr(msg, "Message-ID")))
    ids.update(_message_ids(_hdr(msg, "References")))
    ids.update(_message_ids(_hdr(msg, "In-Reply-To")))
    return ids


def read_thread(account: MailAccount, folder: str, uid: int, max_chars_per_message: int, max_messages: int) -> dict[str, Any]:
    """Collect the conversation around one message.

    Seznam's IMAP server drops the connection on SEARCH HEADER References / In-Reply-To,
    so candidates come from SUBJECT words and HEADER Message-ID (both work), and the
    thread is assembled locally from Message-ID / References / In-Reply-To headers.
    """
    with connect(account) as client:
        _select(client, folder)
        start, _ = _fetch_full(client, uid)
        thread_ids = _thread_ids(start)
        words = subject_search_words(_hdr(start, "Subject") or "")

        folders = [folder]
        sent = find_folder(client, "sent")
        if sent and sent not in folders:
            folders.append(sent)
        if "INBOX" not in (f.upper() for f in folders):
            folders.append("INBOX")

        # 1) candidates per folder
        candidates: dict[str, dict[int, EmailMessage]] = {}
        for f in folders:
            _select(client, f)
            uids: set[int] = set()
            for mid in list(thread_ids)[:20]:
                if not _SAFE_MESSAGE_ID.fullmatch(mid):  # e.g. '{' -> Seznam: "mtd: internal error"
                    continue
                try:
                    uids.update(client.search(["HEADER", "Message-ID", mid]))
                except IMAPClientError:
                    pass  # supplementary lookup only; subject words still find candidates
            if words:
                uids.update(client.search([item for w in words for item in ("SUBJECT", w)]))
            uids_sorted = sorted(uids, reverse=True)[:_THREAD_CANDIDATE_LIMIT]
            data = client.fetch(uids_sorted, [_HEADER_FETCH]) if uids_sorted else {}
            candidates[f] = {u: _parse(_body_key(d) or b"", headers_only=True) for u, d in data.items()}

        # 2) grow the thread until stable
        members: set[tuple[str, int]] = {(folder, uid)}
        changed = True
        while changed:
            changed = False
            for f, msgs in candidates.items():
                for u, hdrs in msgs.items():
                    if (f, u) in members:
                        continue
                    ids = _thread_ids(hdrs)
                    if ids & thread_ids:
                        members.add((f, u))
                        thread_ids |= ids
                        changed = True

        # 3) de-duplicate (same Message-ID in two folders), sort by date
        def _date_key(item: tuple[str, int]) -> float:
            hdrs = start if item == (folder, uid) else candidates[item[0]][item[1]]
            try:
                return parsedate_to_datetime(_hdr(hdrs, "Date") or "").timestamp()
            except Exception:
                return 0.0

        ordered: list[tuple[str, int]] = []
        seen_ids: set[str] = set()
        for item in sorted(members, key=_date_key):
            hdrs = start if item == (folder, uid) else candidates[item[0]][item[1]]
            mid = _hdr(hdrs, "Message-ID") or f"{item[0]}:{item[1]}"
            if mid not in seen_ids:
                seen_ids.add(mid)
                ordered.append(item)

        truncated = len(ordered) > max_messages
        ordered = ordered[-max_messages:]

        # 4) fetch bodies
        messages = []
        for f, u in ordered:
            _select(client, f)
            msg, flags = _fetch_full(client, u)
            messages.append(message_detail(msg, f, u, flags, max_chars_per_message))
        return {"messages": messages, "truncated": truncated}


def build_draft(
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    reply_to_msg: EmailMessage | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to_msg is not None:
        orig_subject = _hdr(reply_to_msg, "Subject") or ""
        if not subject:
            subject = orig_subject if re.match(r"(?i)^(re|odp):", orig_subject) else f"Re: {orig_subject}"
        orig_id = _hdr(reply_to_msg, "Message-ID")
        if orig_id:
            msg["In-Reply-To"] = orig_id
            refs = _message_ids(_hdr(reply_to_msg, "References")) + _message_ids(orig_id)
            msg["References"] = " ".join(dict.fromkeys(refs))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg.set_content(body)
    return msg


def create_draft(
    account: MailAccount,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    reply_folder: str | None = None,
    reply_uid: int | None = None,
) -> dict[str, Any]:
    with connect(account) as client:
        original = None
        if reply_uid is not None:
            _select(client, reply_folder or "INBOX")
            original, _ = _fetch_full(client, reply_uid)
            if not to:
                to = _addresses(original, "Reply-To") or _addresses(original, "From")
        if not to:
            raise MailError("Chybí adresát (to).")
        drafts = find_folder(client, "drafts")
        if not drafts:
            raise MailError("Nenašel jsem složku s koncepty.")
        msg = build_draft(sender=account.email, to=to, subject=subject, body=body, cc=cc, reply_to_msg=original)
        resp = client.append(drafts, msg.as_bytes(policy=policy.SMTP), flags=(b"\\Draft", b"\\Seen"),
                             msg_time=datetime.now(timezone.utc))
        m = re.search(rb"APPENDUID \d+ (\d+)", resp or b"")
        return {
            "saved_to": drafts,
            "uid": int(m.group(1)) if m else None,
            "to": to,
            "cc": cc or [],
            "subject": msg["Subject"],
            "in_reply_to": msg.get("In-Reply-To"),
        }

"""IMAP/SMTP access to a Seznam.cz mailbox (synchronous; call from a worker thread).

Messages are always read with BODY.PEEK, so reading never marks them as seen.
Write operations: drafts, flags (read/starred), moving (incl. to trash) and sending.
Nothing is ever permanently deleted, except a draft right after it was sent.
"""

from __future__ import annotations

import re
import smtplib
import socket
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
SMTP_HOST = "smtp.seznam.cz"
SMTP_PORT = 465
TIMEOUT = 30
MAX_BATCH = 100  # messages per flag/move call
MAX_RECIPIENTS = 20  # To + Cc + Bcc of one sent e-mail

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
    smtp_host: str = SMTP_HOST
    smtp_port: int = SMTP_PORT

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
        "attachments": count_attachments(data.get(b"BODYSTRUCTURE")),
        "size": data.get(b"RFC822.SIZE"),
    }


def _pairs(value: Any) -> dict[str, Any]:
    """BODYSTRUCTURE parameter list (k1, v1, k2, v2, ...) -> {k: v} with lower-case keys."""
    if not isinstance(value, (tuple, list)):
        return {}
    items = list(value)
    out = {}
    for k, v in zip(items[::2], items[1::2]):
        if isinstance(k, bytes):
            out[k.decode(errors="replace").lower()] = v
    return out


def _lower(value: Any) -> str:
    return value.decode(errors="replace").lower() if isinstance(value, bytes) else str(value or "").lower()


def count_attachments(bs: Any) -> int:
    """Count attachments from an IMAP BODYSTRUCTURE without downloading the message."""
    if not bs:
        return 0
    if isinstance(bs[0], list):  # multipart: ([parts], subtype, ...)
        return sum(count_attachments(part) for part in bs[0])
    maintype, subtype = _lower(bs[0]), _lower(bs[1] if len(bs) > 1 else b"")
    if (maintype, subtype) == ("message", "rfc822"):
        return 1  # attached e-mail
    params = _pairs(bs[2] if len(bs) > 2 else None)
    disp_index = 9 if maintype == "text" else 8  # text parts carry a line count before MD5
    disp = bs[disp_index] if len(bs) > disp_index else None
    disp_type = _lower(disp[0]) if isinstance(disp, (tuple, list)) and disp else ""
    disp_params = _pairs(disp[1]) if disp_type and len(disp) > 1 else {}
    if disp_type == "attachment":
        return 1
    named = "name" in params or "filename" in disp_params
    return 1 if named and maintype != "text" else 0


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


def attachment_parts(msg: EmailMessage) -> list[EmailMessage]:
    return list(msg.iter_attachments())  # type: ignore[arg-type]


def _part_size(part: Message) -> int:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        inner = inner[0] if isinstance(inner, list) and inner else inner
        return len(inner.as_bytes()) if isinstance(inner, Message) else 0
    return len(part.get_payload(decode=True) or b"")


def extract_attachments(msg: EmailMessage) -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "filename": part.get_filename() or "(bez názvu)",
            "content_type": part.get_content_type(),
            "size": _part_size(part),
        }
        for i, part in enumerate(attachment_parts(msg), start=1)
    ]


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _select(client: IMAPClient, folder: str, readonly: bool = True) -> None:
    try:
        client.select_folder(folder, readonly=readonly)
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


# public names for other modules
parse_message = _parse
header = _hdr
addresses = _addresses
date_iso = _date_iso


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


_WORD = re.compile(r"\w+")
_ADDRESS_WORD = re.compile(r"[\w.@+-]+")


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
    has_attachment: bool = False,  # not an IMAP criterion; filtered in search()
) -> tuple[list[Any], str | None]:
    criteria: list[Any] = []
    # Seznam matches whole words: search each word separately (AND), without punctuation
    # like the brackets in "[TEST]". Addresses keep their @ . + - characters.
    for key, value, pattern in (("FROM", from_, _ADDRESS_WORD), ("TO", to, _ADDRESS_WORD), ("SUBJECT", subject, _WORD)):
        for word in pattern.findall(fold_diacritics(value or "")):
            criteria += [key, word]
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


ATTACHMENT_SCAN_LIMIT = 500  # newest matches inspected for has_attachment
_SUMMARY_FETCH = [_HEADER_FETCH, "FLAGS", "RFC822.SIZE", "BODYSTRUCTURE"]


def search(account: MailAccount, folder: str, limit: int, **filters: Any) -> dict[str, Any]:
    criteria, charset = build_search_criteria(**filters)
    with connect(account) as client:
        _select(client, folder)
        try:
            uids = client.search(criteria, charset=charset)
        except IMAPClientError as e:
            raise MailError(f"Hledání selhalo: {e}") from e
        uids = sorted(uids, reverse=True)
        if not filters.get("has_attachment"):
            page = uids[:limit]
            data = client.fetch(page, _SUMMARY_FETCH) if page else {}
            messages = [_summary(uid, data[uid], folder) for uid in page if uid in data]
            return {"folder": folder, "total_matches": len(uids), "returned": len(messages), "messages": messages}

        # IMAP cannot search for attachments: check BODYSTRUCTURE of the newest matches.
        scan = uids[:ATTACHMENT_SCAN_LIMIT]
        with_att: list[int] = []
        for i in range(0, len(scan), 100):
            chunk = scan[i : i + 100]
            structures = client.fetch(chunk, ["BODYSTRUCTURE"])
            with_att += [u for u in chunk if count_attachments(structures.get(u, {}).get(b"BODYSTRUCTURE"))]
            if len(with_att) >= limit:
                break
        page = with_att[:limit]
        data = client.fetch(page, _SUMMARY_FETCH) if page else {}
        messages = [_summary(uid, data[uid], folder) for uid in page if uid in data]
        result = {"folder": folder, "returned": len(messages), "messages": messages}
        if len(uids) > ATTACHMENT_SCAN_LIMIT and len(with_att) < limit:
            result["note"] = (
                f"Přílohy se kontrolovaly jen u {ATTACHMENT_SCAN_LIMIT} nejnovějších z {len(uids)} zpráv. "
                "Starší najdeš se zúženým hledáním (since/before)."
            )
        return result


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


# ---------------------------------------------------------------- attachments


def get_attachment(account: MailAccount, folder: str, uid: int, index: int) -> tuple[dict[str, Any], EmailMessage]:
    """Return (info, part) for the index-th attachment (1-based, as listed by read_email)."""
    with connect(account) as client:
        _select(client, folder)
        msg, _ = _fetch_full(client, uid)
    parts = attachment_parts(msg)
    if not 1 <= index <= len(parts):
        raise MailError(f"E-mail má {len(parts)} příloh, index {index} neexistuje." if parts else "E-mail nemá žádné přílohy.")
    return extract_attachments(msg)[index - 1], parts[index - 1]


# ---------------------------------------------------------------- organising


def _check_batch(uids: list[int]) -> list[int]:
    uids = list(dict.fromkeys(uids))
    if not uids:
        raise MailError("Chybí UID zpráv.")
    if len(uids) > MAX_BATCH:
        raise MailError(f"Najednou jde zpracovat nejvýš {MAX_BATCH} zpráv.")
    return uids


def _existing(client: IMAPClient, uids: list[int]) -> tuple[list[int], list[int]]:
    found = set(client.fetch(uids, ["FLAGS"]).keys())
    return [u for u in uids if u in found], [u for u in uids if u not in found]


def set_flags(
    account: MailAccount, folder: str, uids: list[int], *, read: bool | None = None, flagged: bool | None = None
) -> dict[str, Any]:
    uids = _check_batch(uids)
    if read is None and flagged is None:
        raise MailError("Zadej read nebo flagged.")
    with connect(account) as client:
        _select(client, folder, readonly=False)
        ok, missing = _existing(client, uids)
        if ok:
            for value, flag in ((read, b"\\Seen"), (flagged, b"\\Flagged")):
                if value is True:
                    client.add_flags(ok, [flag], silent=True)
                elif value is False:
                    client.remove_flags(ok, [flag], silent=True)
            data = client.fetch(ok, ["FLAGS"])
        else:
            data = {}
        result = []
        for u in ok:
            flags = [f.decode() if isinstance(f, bytes) else str(f) for f in data.get(u, {}).get(b"FLAGS", ())]
            result.append({"uid": u, "unread": "\\Seen" not in flags, "flagged": "\\Flagged" in flags})
        return {"folder": folder, "updated": result, "not_found": missing}


_UIDSET = re.compile(r"COPYUID \d+ ([\d:,]+) ([\d:,]+)")


def _expand_uid_set(spec: str) -> list[int]:
    out: list[int] = []
    for chunk in spec.split(","):
        a, _, b = chunk.partition(":")
        lo, hi = int(a), int(b or a)
        out.extend(range(lo, hi + 1) if lo <= hi else range(lo, hi - 1, -1))
    return out


def _copyuid_map(response: Any) -> dict[int, int]:
    text = response.decode(errors="replace") if isinstance(response, bytes) else str(response or "")
    m = _UIDSET.search(text)
    if not m:
        return {}
    old, new = _expand_uid_set(m.group(1)), _expand_uid_set(m.group(2))
    return dict(zip(old, new)) if len(old) == len(new) else {}


def _message_id_map(client: IMAPClient, uids: list[int]) -> dict[int, str]:
    data = client.fetch(uids, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
    out = {}
    for u, d in data.items():
        ids = _message_ids(_hdr(_parse(_body_key(d) or b"", headers_only=True), "Message-ID"))
        if ids:
            out[u] = ids[0]
    return out


def _find_by_message_id(client: IMAPClient, folder: str, wanted: dict[int, str]) -> dict[int, int]:
    if not wanted:
        return {}
    found: dict[int, int] = {}
    try:
        _select(client, folder)
        for old, mid in wanted.items():
            if not _SAFE_MESSAGE_ID.fullmatch(mid):  # Seznam errors on some characters
                continue
            try:
                hits = client.search(["HEADER", "Message-ID", mid])
            except IMAPClientError:
                continue
            if hits:
                found[old] = max(hits)
    except MailError:
        pass
    return found


def _resolve_target(client: IMAPClient, target: str) -> str:
    """A folder name, or a role: inbox, archive, trash, spam."""
    names = [name for flags, _d, name in client.list_folders() if b"\\Noselect" not in flags]
    if target in names:
        return target
    role = target.strip().lower()
    if role == "inbox":
        return "INBOX"
    if role in _ROLE_FLAGS:
        found = find_folder(client, role)
        if found:
            return found
    lowered = {n.lower(): n for n in names}
    if role in lowered:
        return lowered[role]
    raise MailError(f"Cílová složka '{target}' neexistuje. Použij list_folders.")


def move_messages(account: MailAccount, folder: str, uids: list[int], target: str) -> dict[str, Any]:
    uids = _check_batch(uids)
    with connect(account) as client:
        dest = _resolve_target(client, target)
        if dest == folder:
            raise MailError(f"Zprávy už jsou ve složce '{dest}'.")
        _select(client, folder, readonly=False)
        ok, missing = _existing(client, uids)
        mapping: dict[int, int] = {}
        if ok:
            ids = _message_id_map(client, ok)
            if client.has_capability("MOVE"):
                resp = client.move(ok, dest)
            else:  # COPY + \Deleted + UID EXPUNGE of just these messages
                resp = client.copy(ok, dest)
                client.add_flags(ok, [b"\\Deleted"], silent=True)
                client.uid_expunge(ok)
            mapping = _copyuid_map(resp)
            if len(mapping) < len(ok):
                # Seznam sends COPYUID in an untagged response the client library drops:
                # find the moved messages in the target folder by Message-ID instead.
                mapping.update(_find_by_message_id(client, dest, {u: ids[u] for u in ok if u not in mapping and u in ids}))
        return {
            "from_folder": folder,
            "to_folder": dest,
            "moved": [{"uid": u, "new_uid": mapping.get(u)} for u in ok],
            "not_found": missing,
        }


def trash_messages(account: MailAccount, folder: str, uids: list[int]) -> dict[str, Any]:
    with connect(account) as client:
        trash = find_folder(client, "trash")
    if not trash:
        raise MailError("Nenašel jsem složku Koš.")
    if folder == trash:
        raise MailError("Zprávy už jsou v Koši. Trvalé mazání server záměrně neumí.")
    return move_messages(account, folder, uids, trash)


# ---------------------------------------------------------------- sending


def _recipients(msg: Message) -> list[str]:
    out = []
    for name in ("To", "Cc", "Bcc"):
        out += [addr for _d, addr in getaddresses(msg.get_all(name, [])) if addr]
    return list(dict.fromkeys(a.strip() for a in out))


_ADDR = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def _check_recipients(recipients: list[str]) -> None:
    if not recipients:
        raise MailError("Chybí adresát (to).")
    bad = [r for r in recipients if not _ADDR.match(r)]
    if bad:
        raise MailError("Neplatná adresa: " + ", ".join(bad))
    if len(recipients) > MAX_RECIPIENTS:
        raise MailError(f"Najednou jde poslat nejvýš {MAX_RECIPIENTS} adresátům.")


def smtp_send(account: MailAccount, msg: EmailMessage, recipients: list[str]) -> None:
    try:
        with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=TIMEOUT) as smtp:
            smtp.login(account.email, account.password)
            refused = smtp.send_message(msg, from_addr=account.email, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("Přihlášení k SMTP selhalo. Zkontroluj heslo pro aplikaci.") from e
    except smtplib.SMTPRecipientsRefused as e:
        raise MailError("Server odmítl všechny adresáty: " + ", ".join(e.recipients)) from e
    except smtplib.SMTPSenderRefused as e:
        raise MailError(f"Server odmítl odesílatele: {e.smtp_error!r}") from e
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        raise MailError(f"Odeslání selhalo: {e}") from e
    if refused:
        raise MailError("E-mail odešel, ale tyto adresy server odmítl: " + ", ".join(refused))


def _after_send(client: IMAPClient, msg: EmailMessage, save_copy: bool) -> tuple[str | None, list[str]]:
    """Save a copy to Sent. Errors become warnings: the e-mail has already left."""
    warnings: list[str] = []
    if not save_copy:
        return None, warnings
    sent = find_folder(client, "sent")
    if not sent:
        return None, ["E-mail odešel, ale nenašel jsem složku Odeslané pro uložení kopie."]
    try:
        client.append(sent, msg.as_bytes(policy=policy.SMTP), flags=(b"\\Seen",), msg_time=datetime.now(timezone.utc))
    except IMAPClientError as e:
        warnings.append(f"E-mail odešel, ale kopie do Odeslaných se neuložila: {e}")
        return None, warnings
    return sent, warnings


def _mark_answered(client: IMAPClient, folder: str, uid: int, warnings: list[str]) -> None:
    try:
        _select(client, folder, readonly=False)
        client.add_flags([uid], [b"\\Answered"], silent=True)
    except (IMAPClientError, MailError):
        warnings.append("Původní e-mail se nepodařilo označit jako zodpovězený.")


def send_email(
    account: MailAccount,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_folder: str | None = None,
    reply_uid: int | None = None,
    save_copy: bool = True,
) -> dict[str, Any]:
    with connect(account) as client:
        original = None
        if reply_uid is not None:
            _select(client, reply_folder or "INBOX")
            original, _ = _fetch_full(client, reply_uid)
            if not to:
                to = _addresses(original, "Reply-To") or _addresses(original, "From")
        msg = build_draft(sender=account.email, to=to, subject=subject, body=body, cc=cc, reply_to_msg=original)
        recipients = list(dict.fromkeys(_recipients(msg) + [a for _d, a in getaddresses(bcc or []) if a]))
        _check_recipients(recipients)
        if not (msg["Subject"] or "").strip():
            raise MailError("Chybí předmět.")
        smtp_send(account, msg, recipients)  # Bcc is only in the envelope, never in the headers
        saved_to, warnings = _after_send(client, msg, save_copy)
        if original is not None and reply_uid is not None:
            _mark_answered(client, reply_folder or "INBOX", reply_uid, warnings)
        return {
            "sent": True,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "subject": msg["Subject"],
            "in_reply_to": msg.get("In-Reply-To"),
            "saved_to": saved_to,
            "warnings": warnings,
        }


def send_draft(account: MailAccount, uid: int, folder: str | None = None, save_copy: bool = True) -> dict[str, Any]:
    with connect(account) as client:
        drafts = find_folder(client, "drafts")
        folder = folder or drafts
        if not folder:
            raise MailError("Nenašel jsem složku s koncepty.")
        _select(client, folder)
        msg, flags = _fetch_full(client, uid)
        if folder != drafts and b"\\Draft" not in flags:
            raise MailError("Odeslat jde jen koncept (zprávu ze složky Koncepty).")
        recipients = _recipients(msg)
        _check_recipients(recipients)
        del msg["Bcc"]
        del msg["Date"]
        msg["Date"] = formatdate(localtime=True)
        if not msg["Message-ID"]:
            msg["Message-ID"] = make_msgid(domain=account.email.rsplit("@", 1)[-1])
        smtp_send(account, msg, recipients)
        saved_to, warnings = _after_send(client, msg, save_copy)
        try:  # the draft is now a sent e-mail; remove it like webmail does
            _select(client, folder, readonly=False)
            client.add_flags([uid], [b"\\Deleted"], silent=True)
            client.uid_expunge([uid])
        except (IMAPClientError, MailError):
            warnings.append("E-mail odešel, ale koncept se nepodařilo odstranit z Konceptů.")
        return {
            "sent": True,
            "to": _addresses(msg, "To"),
            "cc": _addresses(msg, "Cc"),
            "subject": msg["Subject"],
            "recipients": len(recipients),
            "saved_to": saved_to,
            "warnings": warnings,
        }

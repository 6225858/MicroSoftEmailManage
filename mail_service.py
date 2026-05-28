import email
import html
import imaplib
import base64
import binascii
import re
from datetime import timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from requests import HTTPError
from sqlalchemy.orm import Session

from models import MailAccount
from oauth_service import OAuthServiceError, get_valid_access_token


IMAP_HOST = "outlook.live.com"
JUNK_CANDIDATES = ["Junk", "Junk Email", "Spam", "\u5783\u573e\u90ae\u4ef6"]
CHINA_TZ = timezone(timedelta(hours=8))


class MailServiceError(Exception):
    def __init__(self, message: str, tag: str | None = None):
        super().__init__(message)
        self.message = message
        self.tag = tag


PRE_CONTENT_PATTERN = re.compile(r"^<pre[^>]*>([\s\S]*)</pre>$", re.IGNORECASE)


def _normalize_tags(tags: str) -> str:
    seen = []
    for item in tags.replace("\uff0c", ",").split(","):
        value = item.strip()
        if value and value not in seen:
            seen.append(value)
    return ",".join(seen)


def _add_tag(account: MailAccount, tag: str) -> None:
    account.tags = _normalize_tags(",".join(filter(None, [account.tags or "", tag])))


def _generate_auth_string(email_name: str, access_token: str) -> str:
    return f"user={email_name}\1auth=Bearer {access_token}\1\1"


def _decode_modified_utf7(value: bytes) -> str:
    text = value.decode("ascii", errors="ignore")
    output = []
    index = 0

    while index < len(text):
        if text[index] != "&":
            output.append(text[index])
            index += 1
            continue

        end_index = text.find("-", index)
        if end_index == -1:
            output.append(text[index:])
            break

        encoded = text[index + 1 : end_index]
        if not encoded:
            output.append("&")
        else:
            padding = "=" * (-len(encoded) % 4)
            try:
                output.append(base64.b64decode((encoded + padding).replace(",", "/")).decode("utf-16-be"))
            except (binascii.Error, UnicodeDecodeError):
                output.append(text[index : end_index + 1])

        index = end_index + 1

    return "".join(output)


def _encode_modified_utf7(value: str) -> bytes:
    output = []
    buffer = []

    def flush_buffer() -> None:
        if not buffer:
            return
        encoded = base64.b64encode("".join(buffer).encode("utf-16-be")).decode("ascii")
        output.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        buffer.clear()

    for char in value:
        codepoint = ord(char)
        if char == "&":
            flush_buffer()
            output.append("&-")
        elif 0x20 <= codepoint <= 0x7E:
            flush_buffer()
            output.append(char)
        else:
            buffer.append(char)

    flush_buffer()
    return "".join(output).encode("ascii")


def _quote_mailbox_name(value: bytes) -> bytes:
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _parse_list_item(item: bytes) -> tuple[bytes, str, str]:
    text = item.strip()
    flags_match = re.match(rb"\((?P<flags>[^)]*)\)", text)
    flags = flags_match.group("flags").decode("ascii", errors="ignore").lower() if flags_match else ""

    if text.endswith(b'"'):
        index = len(text) - 2
        escaped = False
        start_index = -1
        while index >= 0:
            char = text[index : index + 1]
            if char == b'"' and not escaped:
                start_index = index
                break
            escaped = char == b"\\" and not escaped
            if char != b"\\":
                escaped = False
            index -= 1

        raw_name = text[start_index + 1 : -1] if start_index >= 0 else text
        raw_name = raw_name.replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    else:
        raw_name = text.rsplit(maxsplit=1)[-1]

    return raw_name, _decode_modified_utf7(raw_name), flags


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _format_mail_datetime(value: str | None) -> str:
    if not value:
        return ""

    mail_datetime = parsedate_to_datetime(value)
    if mail_datetime.tzinfo is not None:
        mail_datetime = mail_datetime.astimezone(CHINA_TZ)

    return mail_datetime.strftime("%Y-%m-%d %H:%M:%S")


def _extract_body(message: email.message.Message) -> str:
    html_body = ""
    text_body = ""

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="ignore")

            if content_type == "text/html" and not html_body:
                html_body = content
            elif content_type == "text/plain" and not text_body:
                text_body = content
    else:
        payload = message.get_payload(decode=True) or b""
        charset = message.get_content_charset() or "utf-8"
        text_body = payload.decode(charset, errors="ignore")

    if html_body:
        return html_body
    if text_body:
        return f"<pre>{html.escape(text_body)}</pre>"
    return "<p>No content</p>"


def _looks_like_escaped_html(value: str) -> bool:
    sample = value.strip().lower()
    return any(
        marker in sample
        for marker in (
            "&lt;!doctype",
            "&lt;html",
            "&lt;body",
            "&lt;table",
            "&lt;div",
            "&lt;section",
            "&lt;p",
        )
    )


def _extract_pre_content(value: str) -> str:
    match = PRE_CONTENT_PATTERN.match(value)
    return match.group(1) if match else ""


def get_mail_body_render_mode(body: str | None) -> dict[str, str]:
    content = (body or "").strip()
    if not content:
        return {
            "type": "inline",
            "content": "<p>No content</p>",
        }

    if content.lower().startswith("<pre"):
        pre_content = _extract_pre_content(content)
        if pre_content and _looks_like_escaped_html(pre_content):
            return {
                "type": "iframe",
                "content": html.unescape(pre_content),
            }
        return {
            "type": "inline",
            "content": content,
        }

    return {
        "type": "iframe",
        "content": html.unescape(content) if _looks_like_escaped_html(content) else content,
    }


def _select_folder(mail_client: imaplib.IMAP4_SSL, folder: str) -> None:
    if folder.lower() == "junk":
        result, folders = mail_client.list()
        if result != "OK":
            raise MailServiceError("failed to list folders")

        folder_names = [_parse_list_item(item) for item in folders or []]

        target_folder = next(
            (
                raw_name
                for raw_name, display_name, flags in folder_names
                if "\\junk" in flags or any(key.lower() in display_name.lower() for key in JUNK_CANDIDATES)
            ),
            None,
        )
        if not target_folder:
            raise MailServiceError("junk folder not found")
    else:
        target_folder = _encode_modified_utf7("Inbox")

    result, _ = mail_client.select(_quote_mailbox_name(target_folder))
    if result != "OK":
        display_name = _decode_modified_utf7(target_folder)
        raise MailServiceError(f"failed to open folder: {display_name}")


def load_mail_messages(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
):
    try:
        access_token = get_valid_access_token(account, db)
    except HTTPError as exc:
        raise MailServiceError(f"token refresh failed: {exc}", tag="token_invalid") from exc
    except OAuthServiceError as exc:
        raise MailServiceError(f"token refresh failed: {exc}", tag="token_invalid") from exc

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.authenticate(
            "XOAUTH2",
            lambda _: _generate_auth_string(account.email, access_token),
        )
        _select_folder(mail, folder)
        result, data = mail.search(None, "ALL")
        if result != "OK":
            raise MailServiceError("failed to search mails")

        # IMAP search returns byte-string sequence ids; sort them numerically
        # so values like 60/6/59 don't end up in lexicographic order.
        mail_ids = sorted(data[0].split(), key=lambda value: int(value), reverse=True)[:limit]
        items = []
        for mail_id in mail_ids:
            fetch_result, msg_data = mail.fetch(mail_id, "(RFC822)")
            if fetch_result != "OK" or not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            message = email.message_from_bytes(raw_email)
            try:
                mail_dt = _format_mail_datetime(message["Date"])
            except Exception:
                mail_dt = ""

            items.append(
                {
                    "id": mail_id.decode("utf-8", errors="ignore"),
                    "subject": _decode_header(message.get("Subject")),
                    "mail_from": _decode_header(message.get("From")).replace("<", "(").replace(">", ")"),
                    "mail_to": _decode_header(message.get("To")).replace("<", "(").replace(">", ")"),
                    "mail_dt": mail_dt,
                    "body": _extract_body(message),
                }
            )

        mail.logout()
        return items
    except imaplib.IMAP4.error as exc:
        raise MailServiceError(f"imap login failed: {exc}") from exc


def load_account_mails(
    account: MailAccount,
    db: Session,
    folder: str = "inbox",
    limit: int = 20,
):
    try:
        return load_mail_messages(account, db, folder=folder, limit=limit)
    except MailServiceError as exc:
        if exc.tag:
            _add_tag(account, exc.tag)
            db.commit()
        raise

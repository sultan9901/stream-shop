"""Transactional email: SMTP backend + a credential-free `console` backend.

``EMAIL_BACKEND=console`` writes a real ``.eml`` file into ``uploads/outbox`` so
the whole delivery pipeline is testable locally without SMTP credentials.
Credentials are read from the environment only — never hard-coded.
"""
from __future__ import annotations

import logging
import re
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import UPLOAD_ROOT, settings

log = logging.getLogger("stream.email")

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # beyond this we always send a link instead
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class EmailError(RuntimeError):
    pass


def _build(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    attachment: tuple[str, bytes, str] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.email_from_name, settings.email_from))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="streamcorporation.local")
    msg["X-Stream-Corporation"] = "delivery"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        name, blob, ctype = attachment
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(blob, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
    return msg


async def _send_smtp(msg: EmailMessage) -> None:
    if not settings.smtp_host:
        raise EmailError("SMTP_HOST is not configured.")
    try:
        import aiosmtplib
    except ImportError as exc:  # pragma: no cover
        raise EmailError("aiosmtplib is not installed.") from exc

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_starttls and not settings.smtp_ssl,
            use_tls=settings.smtp_ssl,
            timeout=30,
        )
    except Exception as exc:
        raise EmailError(f"SMTP send failed: {exc}") from exc


def _write_outbox(msg: EmailMessage) -> str:
    outbox = UPLOAD_ROOT / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    stem = _SAFE.sub("_", f"{msg['To']}-{msg['Subject']}")[:80]
    path = outbox / f"{stem}-{abs(hash(msg['Message-ID'])) % 10**8}.eml"
    path.write_bytes(bytes(msg))
    return str(path)


async def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    attachment: tuple[str, bytes, str] | None = None,
) -> str:
    """Send and return a short transport note. Raises ``EmailError`` on failure."""
    if not to or "@" not in to:
        raise EmailError("Recipient address is missing or invalid.")
    msg = _build(to=to, subject=subject, text=text, html=html, attachment=attachment)

    backend = (settings.email_backend or "console").lower()
    if backend == "smtp":
        await _send_smtp(msg)
        return f"smtp:{settings.smtp_host}"
    path = _write_outbox(msg)
    log.info("console email written -> %s", path)
    return f"console:{path}"


def backend_label() -> str:
    backend = (settings.email_backend or "console").lower()
    if backend == "smtp":
        return f"SMTP ({settings.smtp_host or 'not configured'})"
    return "Console (uploads/outbox/*.eml)"

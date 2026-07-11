from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from app.config import settings
from app.utils.logger import log_event
from app.services import ses_service


async def send_ticket_email(
    *,
    to: Iterable[str],
    subject: str,
    body_text: str,
    transcript_text: Optional[str] = None,
    transcript_filename: str = "chat_transcript.txt",
) -> bool:
    """
    Send a ticket-notification email with the chat transcript attached.

    Tries Outlook first when configured; falls back to SES on failure
    or when Outlook is disabled.
    """
    if settings.OUTLOOK_ENABLED and settings.OUTLOOK_CLIENT_ID:
        ok = await _send_via_outlook(
            to=to,
            subject=subject,
            body_text=body_text,
            transcript_text=transcript_text,
            transcript_filename=transcript_filename,
        )
        if ok:
            return True
        log_event("errors", "warning", "Outlook send failed; falling back to SES")

    # SES fallback — inline the transcript in the body since the SES v1
    # send_email API doesn't take attachments without raw MIME.
    body = body_text
    if transcript_text:
        body += "\n\n----- CHAT TRANSCRIPT -----\n" + transcript_text
    return await ses_service.send_email(to=to, subject=subject, body_text=body)


async def _send_via_outlook(
    *,
    to: Iterable[str],
    subject: str,
    body_text: str,
    transcript_text: Optional[str],
    transcript_filename: str,
) -> bool:
    """Send via O365 library using client-credentials flow."""
    try:
        from O365 import Account
        from O365.utils import FileAttachment
    except Exception as e:  # pragma: no cover
        log_event("errors", "error", "O365 lib unavailable", error=str(e))
        return False

    def _send():
        creds = (settings.OUTLOOK_CLIENT_ID, settings.OUTLOOK_CLIENT_SECRET)
        account = Account(
            creds,
            auth_flow_type="credentials",
            tenant_id=settings.OUTLOOK_TENANT_ID,
        )
        if not account.is_authenticated:
            if not account.authenticate():
                return False

        mailbox = account.mailbox(settings.OUTLOOK_FROM_EMAIL)
        msg = mailbox.new_message()
        msg.to.add(list(to))
        msg.subject = subject
        msg.body = body_text
        if transcript_text:
            att = FileAttachment(
                (transcript_text.encode("utf-8"), transcript_filename),
            )
            msg.attachments.add(att)
        return msg.send()

    try:
        ok = await asyncio.to_thread(_send)
        log_event("audit", "info", "Outlook ticket email sent", to=list(to), subject=subject)
        return bool(ok)
    except Exception as e:
        log_event("errors", "error", "Outlook send exception", error=str(e))
        return False

"""AWS SES wrapper for OTPs and ticket notifications."""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

import boto3
from botocore.config import Config as BotoConfig

from app.config import settings
from app.utils.logger import log_event

_cfg = BotoConfig(region_name=settings.SES_REGION, retries={"max_attempts": 3, "mode": "standard"})
_ses = None


def _client():
    global _ses
    if _ses is None:
        kwargs = {"config": _cfg, "region_name": settings.SES_REGION}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        _ses = boto3.client("ses", **kwargs)
    return _ses


async def send_email(
    *,
    to: Iterable[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    cc: Optional[Iterable[str]] = None,
) -> bool:
    """
    Send an email via SES. Returns True on success.

    In dev mode (no AWS creds), logs and pretends success — useful so
    the rest of the flow can be tested end-to-end without SES enabled.
    """
    if not settings.AWS_ACCESS_KEY_ID:
        log_event("auth", "info", "[DEV] email skipped (no AWS creds)",
                  to=list(to), subject=subject)
        log_event("auth", "info", "[DEV] email body", body=body_text)
        return True

    msg: dict = {
        "Source": settings.SES_FROM_EMAIL,
        "Destination": {"ToAddresses": list(to)},
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
        },
    }
    if cc:
        msg["Destination"]["CcAddresses"] = list(cc)
    if body_html:
        msg["Message"]["Body"]["Html"] = {"Data": body_html, "Charset": "UTF-8"}

    def _send():
        _client().send_email(**msg)

    try:
        await asyncio.to_thread(_send)
        log_event("audit", "info", "SES email sent", to=list(to), subject=subject)
        return True
    except Exception as e:
        log_event("errors", "error", "SES send failed", error=str(e))
        return False


async def send_otp_email(email: str, code: str) -> bool:
    return await send_email(
        to=[email],
        subject="Your Company AI login code",
        body_text=(
            f"Your Company AI Assistant verification code is: {code}\n\n"
            f"This code expires in {settings.OTP_EXPIRE_SECONDS // 60} minutes.\n"
            "If you didn't request this, you can safely ignore the email."
        ),
        body_html=f"""
        <div style="font-family:Inter,Arial,sans-serif">
          <h2 style="color:#1f2937">Company AI Assistant</h2>
          <p>Your verification code:</p>
          <p style="font-size:28px;letter-spacing:6px;font-weight:600;color:#2563eb">{code}</p>
          <p style="color:#6b7280">Expires in {settings.OTP_EXPIRE_SECONDS // 60} minutes.</p>
        </div>
        """,
    )

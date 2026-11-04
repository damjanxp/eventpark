"""Sending email over SMTP (Mailpit locally; on AWS SES would be the stretch goal, or emails are only logged).

smtplib is synchronous, so it runs in a worker thread. No SMTP_URL configured = the email is logged instead (handy for
tests and for an AWS dev environment without SES).
"""

import asyncio
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import urlparse

import structlog

log = structlog.get_logger()


@dataclass
class Attachment:
    filename: str
    content: bytes
    maintype: str = "image"
    subtype: str = "png"


@dataclass
class Mail:
    to: str
    subject: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)


class Mailer:
    def __init__(self, smtp_url: str | None, sender: str) -> None:
        self.smtp_url, self.sender = smtp_url, sender

    def _send_sync(self, mail: Mail) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, mail.to, mail.subject
        message.set_content(mail.text)
        for a in mail.attachments:
            message.add_attachment(a.content, maintype=a.maintype, subtype=a.subtype, filename=a.filename)
        url = urlparse(self.smtp_url or "")
        with smtplib.SMTP(url.hostname or "localhost", url.port or 25, timeout=10) as smtp:
            smtp.send_message(message)

    async def send(self, mail: Mail) -> None:
        if not self.smtp_url:
            log.info("mail.not_sent_no_smtp", to_domain=mail.to.rsplit("@", 1)[-1], subject=mail.subject)
            return
        await asyncio.to_thread(self._send_sync, mail)
        log.info("mail.sent", to_domain=mail.to.rsplit("@", 1)[-1], subject=mail.subject)  # never log full addresses

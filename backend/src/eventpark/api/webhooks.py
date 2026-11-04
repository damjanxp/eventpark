"""Webhooks from external providers (/webhooks). Authenticated by signature, not by a token or key."""

import time

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from eventpark.api.deps import SessionDep, SettingsDep, utcnow
from eventpark.services import webhooks

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookAck(BaseModel):
    result: str


@router.post("/fakepay")
async def fakepay_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    fakepay_signature: str | None = Header(default=None),
) -> WebhookAck:
    body = await request.body()  # the RAW bytes: the signature covers exactly what was sent
    event = webhooks.parse_and_verify(
        body, fakepay_signature, secret=settings.fakepay_webhook_secret.get_secret_value(), now=int(time.time())
    )
    result = await webhooks.handle_payment_event(session, event, now=utcnow())
    return WebhookAck(result=result)

"""Client for the payment provider (fakepay; a real one would look the same).

Never called inside a database transaction (CLAUDE.md §3): the use cases commit their PENDING payment first, then
call the provider, then record the outcome in a second transaction. A provider that takes 8 s therefore never holds a
row lock for 8 s, and a crash in between leaves a PENDING/UNKNOWN payment that reconciliation resolves later.
"""

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from eventpark.domain.payments import PaymentOutcome

log = structlog.get_logger()


@dataclass(frozen=True)
class ChargeResult:
    outcome: PaymentOutcome  # SUCCEEDED | FAILED | NO_ANSWER
    provider_ref: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class IntentResult:
    intent_id: str
    checkout_url: str
    status: str


class ProviderUnavailable(Exception):
    pass


class PaymentProvider:
    def __init__(
        self, base_url: str, *, terminal_timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5.0, transport=transport)
        self._terminal_timeout = terminal_timeout

    async def aclose(self) -> None:
        await self._http.aclose()

    async def charge_terminal(
        self, *, idempotency_key: str, amount_minor: int, currency: str, card_token: str, reference: str
    ) -> ChargeResult:
        """Synchronous card charge with a hard timeout. Timeout != declined: it means 'we don't know'."""
        try:
            response = await self._http.post(
                "/v1/terminal/charges",
                json={"amount": amount_minor, "currency": currency, "card_token": card_token, "reference": reference},
                headers={"Idempotency-Key": idempotency_key},
                timeout=self._terminal_timeout,
            )
        except httpx.HTTPError as exc:
            log.warning("provider.charge_no_answer", error=type(exc).__name__)
            return ChargeResult(PaymentOutcome.NO_ANSWER)
        if response.status_code >= 500:
            return ChargeResult(PaymentOutcome.NO_ANSWER)
        return _charge_from(response)

    async def get_charge(self, idempotency_key: str) -> ChargeResult:
        """Reconciliation: what really happened to the charge with this key?"""
        try:
            response = await self._http.get(f"/v1/terminal/charges/{idempotency_key}")
        except httpx.HTTPError:
            return ChargeResult(PaymentOutcome.NO_ANSWER)
        if response.status_code == 404:
            # The provider never received it: nothing was charged.
            return ChargeResult(PaymentOutcome.FAILED, failure_reason="not_found_at_provider")
        if response.status_code >= 400:
            return ChargeResult(PaymentOutcome.NO_ANSWER)
        return _charge_from(response)

    async def create_intent(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        description: str,
        return_url: str,
        webhook_url: str,
        metadata: dict[str, str],
    ) -> IntentResult:
        try:
            response = await self._http.post(
                "/v1/payment_intents",
                json={
                    "amount": amount_minor,
                    "currency": currency,
                    "description": description,
                    "return_url": return_url,
                    "webhook_url": webhook_url,
                    "metadata": metadata,
                },
                headers={"Idempotency-Key": idempotency_key},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        body: dict[str, Any] = response.json()
        return IntentResult(intent_id=body["id"], checkout_url=body["checkout_url"], status=body["status"])

    async def get_intent(self, intent_id: str) -> IntentResult | None:
        try:
            response = await self._http.get(f"/v1/payment_intents/{intent_id}")
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        return IntentResult(intent_id=body["id"], checkout_url=body["checkout_url"], status=body["status"])


def _charge_from(response: httpx.Response) -> ChargeResult:
    body = response.json()
    if body.get("status") == "succeeded":
        return ChargeResult(PaymentOutcome.SUCCEEDED, provider_ref=body.get("id"))
    return ChargeResult(PaymentOutcome.FAILED, provider_ref=body.get("id"), failure_reason=body.get("decline_reason"))

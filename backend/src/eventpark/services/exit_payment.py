"""Paying at the exit lane (docs/01-product-and-domain.md §6 flow D, docs/02-architecture.md §3.3).

    device ─POST /gate/v1/exit/pay {request_id, passage_id, card_token}─▶ API
      tx 1:  payment PENDING (idempotency_key = request_id)         COMMIT   <- before any network call!
      call:  fakepay POST /v1/terminal/charges (Idempotency-Key, timeout 5 s)
      tx 2:  SUCCEEDED -> payment SUCCEEDED, ticket paid + EXITING, passage AUTHORIZED, outbox  -> OPEN
             FAILED    -> payment FAILED                                                       -> DECLINED
             no answer -> payment UNKNOWN                                                      -> PAYMENT_PENDING

Why two transactions: a database transaction must never wait on the network. If it did, a provider taking 8 s would
hold row locks for 8 s (blocking other lanes) and exhaust the connection pool under load.
Why the same request_id: the device retries a payment whose outcome it doesn't know with the same id. We find the
same payment row, the provider finds the same charge, so the card is charged at most once, however often we retry.
"""

import time
import uuid
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.errors import Conflict, NotFound
from eventpark.db.models import Passage, Payment, Ticket
from eventpark.domain.payments import (
    PaymentKind,
    PaymentStatus,
    PaymentTransitionError,
    payment_transition,
)
from eventpark.domain.tickets import Decision, PassageStatus, TicketStatus
from eventpark.messaging import outbox
from eventpark.services.gate import DeviceContext, GateDecision, GateSettings, format_money
from eventpark.services.provider import ChargeResult, PaymentProvider

log = structlog.get_logger()


async def _lock_ticket_passage(
    session: AsyncSession, device: DeviceContext, passage_id: uuid.UUID
) -> tuple[Ticket, Passage]:
    """Lock order (docs/02 §5): ticket -> passage -> payment."""
    peek = (await session.execute(select(Passage.device_id, Passage.ticket_id).where(Passage.id == passage_id))).first()
    if peek is None or peek.device_id != device.device_id or peek.ticket_id is None:
        raise NotFound("No such passage at this gate.", code="PASSAGE_NOT_FOUND")
    ticket = await session.scalar(select(Ticket).where(Ticket.id == peek.ticket_id).with_for_update())
    passage = await session.scalar(select(Passage).where(Passage.id == passage_id).with_for_update())
    if ticket is None or passage is None:  # pragma: no cover (FK)
        raise NotFound("No such passage at this gate.", code="PASSAGE_NOT_FOUND")
    return ticket, passage


def _open(passage: Passage, amount: int, currency: str) -> GateDecision:
    return GateDecision(
        decision=Decision.OPEN,
        passage_id=passage.id,
        amount_minor=amount,
        currency=currency,
        display_text=f"Paid {format_money(amount, currency)} - thank you, goodbye!",
    )


def _declined(passage: Passage, reason: str | None) -> GateDecision:
    return GateDecision(
        decision=Decision.DECLINED,
        passage_id=passage.id,
        amount_minor=passage.amount_due_minor,
        display_text="Card declined - please try another card" + (f" ({reason})" if reason else ""),
    )


def _pending(passage: Passage) -> GateDecision:
    return GateDecision(
        decision=Decision.PAYMENT_PENDING,
        passage_id=passage.id,
        amount_minor=passage.amount_due_minor,
        display_text="Please wait...",
    )


async def exit_pay(
    session: AsyncSession,
    device: DeviceContext,
    provider: PaymentProvider,
    *,
    request_id: str,
    passage_id: uuid.UUID,
    card_token: str,
    now: datetime,
    cfg: GateSettings,
) -> GateDecision:
    started = time.perf_counter()
    key = f"exit:{request_id}"

    # --- transaction 1: find or create the PENDING payment, commit ---
    async with session.begin():
        ticket, passage = await _lock_ticket_passage(session, device, passage_id)
        payment = await session.scalar(select(Payment).where(Payment.idempotency_key == key).with_for_update())
        if payment is not None:
            if payment.passage_id != passage.id:
                raise Conflict("This request id belongs to another passage.", code="IDEMPOTENCY_KEY_REUSED")
            if payment.status is PaymentStatus.SUCCEEDED:
                return _open(passage, payment.amount_minor, payment.currency)  # replay of a finished payment
            if payment.status is PaymentStatus.FAILED:
                return _declined(passage, payment.failure_reason)
            # PENDING/UNKNOWN: we don't know the outcome yet -> ask the provider again (same key) below
        else:
            if passage.status is not PassageStatus.PENDING_PAYMENT or not passage.amount_due_minor:
                raise Conflict(f"Passage is {passage.status.value}; nothing to pay.", code="NOTHING_TO_PAY")
            if ticket.status is not TicketStatus.INSIDE:
                raise Conflict(f"Ticket is {ticket.status.value}.", code="TICKET_NOT_INSIDE")
            payment = Payment(
                kind=PaymentKind.EXIT_FEE,
                ticket_id=ticket.id,
                passage_id=passage.id,
                amount_minor=passage.amount_due_minor,
                currency=str(passage.response.get("currency") or "RSD"),
                status=PaymentStatus.PENDING,
                idempotency_key=key,
            )
            session.add(payment)
            await session.flush()
        payment_id, amount, currency = payment.id, payment.amount_minor, payment.currency

    # --- outside any transaction: talk to the provider ---
    result = await provider.charge_terminal(
        idempotency_key=key, amount_minor=amount, currency=currency, card_token=card_token, reference=str(passage_id)
    )

    # --- transaction 2: record what we learned ---
    decision = await record_exit_payment(session, device, payment_id=payment_id, result=result, now=now, cfg=cfg)
    log.info(
        "gate.exit_payment",
        decision=decision.decision.value,
        outcome=result.outcome.value,
        passage_id=str(passage_id),
        gate=device.gate_code,
        amount_minor=amount,
        ms=int((time.perf_counter() - started) * 1000),
    )
    return decision


async def record_exit_payment(
    session: AsyncSession,
    device: DeviceContext | None,
    *,
    payment_id: uuid.UUID,
    result: ChargeResult,
    now: datetime,
    cfg: GateSettings,
) -> GateDecision:
    """Apply a provider outcome to an exit payment. Also used by reconciliation (device=None) in Phase 6."""
    async with session.begin():
        peek = (
            await session.execute(select(Payment.ticket_id, Payment.passage_id).where(Payment.id == payment_id))
        ).one()
        # populate_existing: this session may still hold these objects from exit_pay's first transaction (the
        # session doesn't expire them on commit). Without it, SELECT ... FOR UPDATE waits for the lock correctly but
        # then returns the CACHED object, e.g. status PENDING although a concurrent retry already committed
        # SUCCEEDED, so both would apply the effects (found by the Phase 9 chaos run: revenue counted twice).
        fresh = {"populate_existing": True}
        ticket = await session.scalar(
            select(Ticket).where(Ticket.id == peek.ticket_id).with_for_update().execution_options(**fresh)
        )
        passage = await session.scalar(
            select(Passage).where(Passage.id == peek.passage_id).with_for_update().execution_options(**fresh)
        )
        payment = await session.scalar(
            select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(**fresh)
        )
        if ticket is None or passage is None or payment is None:  # pragma: no cover (FKs)
            raise NotFound("Payment not found.", code="PAYMENT_NOT_FOUND")

        target = payment_transition(payment.status, result.outcome)
        if isinstance(target, PaymentTransitionError):
            log.error("payment.contradiction", payment_id=str(payment.id), detail=target.message)
            target = payment.status  # keep what we had; a human looks at it
        was_succeeded = payment.status is PaymentStatus.SUCCEEDED
        payment.status = target
        payment.provider_ref = result.provider_ref or payment.provider_ref
        if target is PaymentStatus.FAILED:
            payment.failure_reason = result.failure_reason
            return _declined(passage, result.failure_reason)
        if target is not PaymentStatus.SUCCEEDED:
            return _pending(passage)  # UNKNOWN: the device shows "please wait" and retries with the same request_id

        if not was_succeeded:  # first time we hear "succeeded": apply the effects exactly once
            ticket.paid_minor += payment.amount_minor
            ticket.paid_at = now
            if ticket.status is TicketStatus.INSIDE:
                ticket.status, ticket.hold_expires_at = TicketStatus.EXITING, now + cfg.hold
            # The money is taken: let the car out even if the 5-minute payment window has passed meanwhile.
            if passage.status in (PassageStatus.PENDING_PAYMENT, PassageStatus.EXPIRED):
                passage.status, passage.deadline_at = PassageStatus.AUTHORIZED, now + cfg.hold
            outbox.add(
                session,
                "payment.succeeded",
                {
                    "payment_id": payment.id,
                    "kind": payment.kind,
                    "amount_minor": payment.amount_minor,
                    "currency": payment.currency,
                    "ticket_id": ticket.id,
                    "passage_id": passage.id,
                    "gate_id": passage.gate_id,
                },
                event_id=ticket.event_id,
            )
        return _open(passage, payment.amount_minor, payment.currency)

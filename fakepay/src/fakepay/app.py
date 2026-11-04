"""fakepay: a tiny Stripe-like payment provider for local development and the simulator.

- Payment intents (online booking): create -> driver pays on our checkout page -> we POST a signed webhook to the
  merchant (retried with backoff until it answers 2xx; sometimes sent twice on purpose, to prove the merchant dedupes).
- Terminal charges (exit-lane card readers): synchronous, after a random delay; the card token decides the outcome:
  tok_ok -> succeeded, tok_decline -> declined, tok_slow -> 8 s (forces the merchant's timeout), tok_random -> random.
- Every create call is idempotent by `Idempotency-Key`: the same key returns the same object, never a second charge.

State lives in memory: restart = blank provider. Good enough for a fake; the merchant reconciles via GET endpoints.
"""

import asyncio
import json
import os
import random
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from fakepay.signing import signature_header


@dataclass
class Config:
    webhook_secret: str
    public_url: str = "http://localhost:8100"  # what a browser uses to reach the checkout page
    latency_min: float = 0.3
    latency_max: float = 2.0
    slow_seconds: float = 8.0
    failure_rate: float = 0.2  # for tok_random
    duplicate_webhook_rate: float = 0.1
    webhook_delay: float = 0.2
    webhook_max_attempts: int = 8
    webhook_retry_base: float = 0.5
    auto_pay: bool = False  # simulator: every new intent succeeds by itself after webhook_delay

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            webhook_secret=env["FAKEPAY_WEBHOOK_SECRET"],
            public_url=env.get("FAKEPAY_PUBLIC_URL", "http://localhost:8100"),
            latency_min=float(env.get("FAKEPAY_LATENCY_MIN", "0.3")),
            latency_max=float(env.get("FAKEPAY_LATENCY_MAX", "2.0")),
            slow_seconds=float(env.get("FAKEPAY_SLOW_SECONDS", "8")),
            failure_rate=float(env.get("FAKEPAY_FAILURE_RATE", "0.2")),
            duplicate_webhook_rate=float(env.get("FAKEPAY_DUPLICATE_WEBHOOK_RATE", "0.1")),
            webhook_delay=float(env.get("FAKEPAY_WEBHOOK_DELAY", "0.2")),
            auto_pay=env.get("FAKEPAY_AUTO_PAY", "false").lower() == "true",
        )


IntentStatus = Literal["requires_payment", "succeeded", "failed"]

# Test card tokens (public, like Stripe's test cards): they choose the terminal's behaviour.
TOK_SLOW, TOK_DECLINE, TOK_RANDOM = "tok_slow", "tok_decline", "tok_random"
CSS = (
    "body{font-family:system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem}"
    "button{font-size:1.1rem;padding:.6rem 1.2rem;margin-right:.5rem}"
)


class IntentIn(BaseModel):
    amount: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    description: str = ""
    return_url: str
    webhook_url: str
    metadata: dict[str, str] = Field(default_factory=dict)


class Intent(BaseModel):
    id: str
    status: IntentStatus
    amount: int
    currency: str
    description: str
    checkout_url: str
    return_url: str
    webhook_url: str
    metadata: dict[str, str]
    created: int


class ChargeIn(BaseModel):
    amount: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    card_token: str
    reference: str = ""


class Charge(BaseModel):
    id: str
    idempotency_key: str
    status: Literal["succeeded", "declined"]
    amount: int
    currency: str
    decline_reason: str | None = None
    created: int


@dataclass
class Store:
    intents: dict[str, Intent] = field(default_factory=dict)
    intent_keys: dict[str, str] = field(default_factory=dict)  # Idempotency-Key -> intent id
    charges: dict[str, Charge] = field(default_factory=dict)  # Idempotency-Key -> charge
    charge_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    webhooks_sent: list[dict[str, Any]] = field(default_factory=list)  # for tests / the debug page


def create_app(
    config: Config | None = None, *, http: httpx.AsyncClient | None = None, rng: random.Random | None = None
) -> FastAPI:
    cfg = config or Config.from_env()
    store = Store()
    rand = rng or random.Random()  # noqa: S311 (simulation randomness, not security)
    background: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.http = http or httpx.AsyncClient(timeout=5.0)
        yield
        for task in background:
            task.cancel()
        if http is None:
            await app.state.http.aclose()

    app = FastAPI(title="fakepay", description=__doc__, lifespan=lifespan)
    app.state.store = store

    def spawn(coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def deliver(intent: Intent) -> None:
        """POST a signed webhook; retry with exponential backoff until the merchant answers 2xx."""
        await asyncio.sleep(cfg.webhook_delay)
        event = {
            "id": f"evt_{secrets.token_hex(12)}",
            "type": f"payment_intent.{intent.status}",
            "created": int(time.time()),
            "data": {"object": intent.model_dump()},
        }
        body = json.dumps(event).encode()
        copies = 2 if rand.random() < cfg.duplicate_webhook_rate else 1  # sometimes twice: merchants must dedupe
        for _copy in range(copies):
            for attempt in range(cfg.webhook_max_attempts):
                headers = {
                    "Content-Type": "application/json",
                    "Fakepay-Signature": signature_header(cfg.webhook_secret, int(time.time()), body),
                }
                try:
                    response = await app.state.http.post(intent.webhook_url, content=body, headers=headers)
                    store.webhooks_sent.append(
                        {"event_id": event["id"], "status": response.status_code, "attempt": attempt}
                    )
                    if response.is_success:
                        break
                except httpx.HTTPError as exc:
                    store.webhooks_sent.append({"event_id": event["id"], "error": repr(exc), "attempt": attempt})
                await asyncio.sleep(cfg.webhook_retry_base * 2**attempt)

    def settle(intent: Intent, status: IntentStatus) -> Intent:
        if intent.status == "requires_payment":
            intent.status = status
            spawn(deliver(intent))
        return intent

    # --- payment intents ---

    @app.post("/v1/payment_intents", status_code=201)
    async def create_intent(body: IntentIn, idempotency_key: Annotated[str, Header()]) -> Intent:
        if (existing := store.intent_keys.get(idempotency_key)) is not None:
            return store.intents[existing]
        intent_id = f"pi_{secrets.token_hex(12)}"
        intent = Intent(
            id=intent_id,
            status="requires_payment",
            checkout_url=f"{cfg.public_url}/checkout/{intent_id}",
            created=int(time.time()),
            **body.model_dump(),
        )
        store.intents[intent_id], store.intent_keys[idempotency_key] = intent, intent_id
        if cfg.auto_pay or body.metadata.get("auto") in ("succeed", "fail"):
            spawn(_auto(intent, "failed" if body.metadata.get("auto") == "fail" else "succeeded"))
        return intent

    async def _auto(intent: Intent, status: IntentStatus) -> None:
        await asyncio.sleep(cfg.webhook_delay)
        settle(intent, status)

    @app.get("/v1/payment_intents/{intent_id}")
    async def get_intent(intent_id: str) -> Intent:
        intent = store.intents.get(intent_id)
        if intent is None:
            raise HTTPException(404, "no such intent")
        return intent

    @app.post("/v1/payment_intents/{intent_id}/simulate")
    async def simulate(intent_id: str, outcome: Literal["succeeded", "failed"]) -> Intent:
        """For tests and the simulator: decide an intent's outcome without the browser page."""
        return settle(await get_intent(intent_id), outcome)

    @app.get("/checkout/{intent_id}", response_class=HTMLResponse)
    async def checkout_page(intent_id: str) -> str:
        intent = await get_intent(intent_id)
        amount = f"{intent.amount // 100:,}.{intent.amount % 100:02d} {intent.currency}"
        state = "" if intent.status == "requires_payment" else f"<p><b>Already {intent.status}.</b></p>"
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>fakepay checkout</title>
<style>{CSS}</style></head>
<body><h1>fakepay</h1><p>{intent.description or "Payment"}</p><p style="font-size:2rem">{amount}</p>{state}
<form method="post" action="/checkout/{intent.id}/pay" style="display:inline"><button>Pay</button></form>
<form method="post" action="/checkout/{intent.id}/decline" style="display:inline"><button>Decline</button></form>
<p style="color:#666">Simulated provider: no real money moves.</p></body></html>"""

    @app.post("/checkout/{intent_id}/{action}")
    async def checkout_action(intent_id: str, action: Literal["pay", "decline"]) -> RedirectResponse:
        intent = settle(await get_intent(intent_id), "succeeded" if action == "pay" else "failed")
        separator = "&" if "?" in intent.return_url else "?"
        # The redirect is only a convenience for the browser. It is NOT proof of payment: the webhook is.
        return RedirectResponse(f"{intent.return_url}{separator}payment={intent.status}", status_code=303)

    # --- terminal (exit lanes) ---

    @app.post("/v1/terminal/charges")
    async def charge(body: ChargeIn, idempotency_key: Annotated[str, Header()]) -> Charge:
        lock = store.charge_locks.setdefault(idempotency_key, asyncio.Lock())
        async with lock:  # a retry arriving while the first attempt is still processing waits for it
            if (existing := store.charges.get(idempotency_key)) is not None:
                return existing
            if body.card_token == TOK_SLOW:
                await asyncio.sleep(cfg.slow_seconds)
            else:
                await asyncio.sleep(rand.uniform(cfg.latency_min, cfg.latency_max))
            declined = body.card_token == TOK_DECLINE or (
                body.card_token == TOK_RANDOM and rand.random() < cfg.failure_rate
            )
            result = Charge(
                id=f"ch_{secrets.token_hex(12)}",
                idempotency_key=idempotency_key,
                status="declined" if declined else "succeeded",
                amount=body.amount,
                currency=body.currency,
                decline_reason="card_declined" if declined else None,
                created=int(time.time()),
            )
            store.charges[idempotency_key] = result  # stored even if the caller timed out: it may ask again
            return result

    @app.get("/v1/terminal/charges/{idempotency_key}")
    async def get_charge(idempotency_key: str) -> Charge:
        charge_ = store.charges.get(idempotency_key)
        if charge_ is None:
            raise HTTPException(404, "no such charge")
        return charge_

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/debug/webhooks")
    async def debug_webhooks() -> list[dict[str, Any]]:
        return store.webhooks_sent[-100:]

    return app

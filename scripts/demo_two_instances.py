# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx", "websockets"]
# ///
"""Phase 7 demo: a dashboard connected to API instance A gets live updates caused by a gate talking to instance B.

    docker compose --profile scale up -d --wait && make seed
    uv run scripts/demo_two_instances.py

What happens
  1. log in as admin (through the load balancer, :8080) and get a one-time ws-ticket
  2. open the WebSocket on instance A (api, :8000) for the live event; the first message is the current occupancy
  3. gate N1 issues a drive-up ticket and completes the passage on instance B (api-2, :8001)
  4. B commits the passage + an outbox row -> relay -> SQS -> worker-entry -> Redis PUBLISH
     -> every API instance's subscriber -> A forwards it to our socket
A never saw the gate request. If the update arrives anyway, pub/sub fan-out works.
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
LB, A, B = "http://localhost:8080", "http://localhost:8000", "http://localhost:8001"
PASSWORD = "eventpark-demo-pass"  # noqa: S105 - the public seed password (make seed)


def occupied(zones: dict[str, dict[str, int]]) -> int:
    return sum(z.get("occupied", z.get("drive_up_occupied", 0)) for z in zones.values())


async def main() -> int:
    seed = json.loads((ROOT / ".devices.local.json").read_text())
    event_id = seed["live_event_id"]
    n1 = next(d for d in seed["devices"] if d["gate_code"] == "N1")

    async with httpx.AsyncClient(timeout=10) as http:
        login = await http.post(
            f"{LB}/admin/v1/auth/login", json={"email": seed["users"]["ADMIN"], "password": PASSWORD}
        )
        login.raise_for_status()
        print(f"logged in via load balancer (answered by {login.headers.get('x-upstream', '?')})")
        token = login.json()["access_token"]
        ticket = (await http.post(f"{A}/admin/v1/ws-ticket", headers={"Authorization": f"Bearer {token}"})).json()[
            "ticket"
        ]

        ws_url = f"ws://localhost:8000/ws/v1/events/{event_id}?ticket={ticket}"
        async with websockets.connect(ws_url) as ws:
            first = json.loads(await ws.recv())
            before = occupied(first["zones"])
            print(f"WebSocket open on instance A; occupancy now {before} (source: {first.get('source')})")

            gate = {"Authorization": f"Device {n1['api_key']}"}
            issued = await http.post(f"{B}/gate/v1/entry/issue", headers=gate, json={"request_id": str(uuid.uuid4())})
            issued.raise_for_status()
            decision = issued.json()
            print(f"instance B: N1 issued a ticket -> {decision['decision']} zone {decision.get('zone')}")
            if decision["decision"] != "OPEN":
                print("gate did not open (event full or not live?); run `make seed` and retry", file=sys.stderr)
                return 1
            done = await http.post(f"{B}/gate/v1/passages/{decision['passage_id']}/complete", headers=gate, json={})
            done.raise_for_status()
            sent = time.monotonic()
            print("instance B: passage completed (car drove through)")

            deadline = sent + 15
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
                except TimeoutError:
                    break
                if msg["type"] == "occupancy" and occupied(msg["zones"]) > before:
                    ms = (time.monotonic() - sent) * 1000
                    print(f"instance A pushed occupancy {occupied(msg['zones'])} after {ms:.0f} ms  ✔ fan-out works")
                    return 0
                print(f"  (A pushed {msg['type']})")
    print("no update reached instance A within 15 s  ✘ (are worker-entry and outbox-relay healthy?)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

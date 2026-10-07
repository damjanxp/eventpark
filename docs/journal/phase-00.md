# Phase 00 — Foundations & a safe AWS account

## Before building: my understanding

### The real gate system at work (you are the domain expert)
Write it in your own words. Don't look anything up and don't open `01-product-and-domain.md` first.
Being wrong or incomplete is fine.

1. **A car arrives, step by step:** loop detector → … → barrier closes. What does the driver see on the display at each step?

   > A car arrives and it is on a loop, the loop activates signaling to the controller and pi that the car is there and
   > that the ticket can be taken, if there's no car no walking human can take the ticket just like that. Then he can scan
   > the QR code or press the button for a ticket and then a barrier opens. QR code gets processed via some software, Pi
   > sends instructions to ticket printer to print a correct ticket.

   Follow-up (2026-10-07):
   > Ticket button doesn't work if there's no vehicle on the loop.
   > Barrier opens only when the person pulls out the ticket.

2. **Who decides what:** what does the Pi do locally, and what does it ask the server? What does a request look like
   (roughly), and how long does the server usually take to answer?

   > I'm not sure.

3. **Printer / drive-up tickets:** what's printed on the ticket? What happens on a paper jam or when the printer is empty?

   > There's a QR code printed. Printer has to be refilled manually

   Follow-up (2026-10-07):
   > Paper low we can make a signal I guess

4. **Paid exit:** how does paying at the exit lane terminal work? What happens when a card is declined, or the terminal hangs?

   > Usually the person working just lets them go so there's no jams..

5. **Network drops:** what does the Pi do when the server doesn't answer? Does the barrier stay shut? Is there an
   offline mode or a local whitelist?

   > Not sure

6. **Things that go wrong in practice:** cars reversing out, tailgating (two cars, one opening), lost tickets, a
   loop detector misfiring, people scanning the same QR twice, a barrier hitting a car…

   > Everything of that

   Follow-up (2026-10-07):
   > Tailgating is handled - We have an exit loop that closes the barrier once the vehicle passes, however if a new
   > vehicle appears the barrier will stop closing so it doesn't hit the vehicle - It relies more on people being well
   > behaved and not going full speed after some vehicle.

   > Just so you know this is a made up project based on something i did in reality so we don't need to follow
   > everything that closely, we can take some shortcuts and make stuff up as we go.

7. **Operators:** what can staff do remotely (manual open, etc.)? Is it logged?

   > They can open it, it is logged.

8. **Pricing in real installations:** are free minutes / per-started-hour / daily cap normal? Our defaults are
   15 free min, 300 RSD per started hour, 1,500 RSD daily cap, 2,000 RSD lost-ticket fee. Realistic?

   > Prices are okay.

### Concepts in my words (2–3 sentences each)
- **Region vs Availability Zone:** Region is a geographical place where we rent our stuff from AWS is located at. We
  need to look for the closest location to us aswell to our customers. AZ is Availability Zones which are something
  like backups to the main stuff, we need to have atleast 2 AZs for ALB to be working when creating it.
- **Shared responsibility:** Im not sure.
  - *Revised (2026-10-07):* AWS ensures that there's security on the cloud while we make sure that our own stuff is
    secured. If someone get ahold of our database it is our fault that that has happened.
- **Why the root user goes "in the safe":** We create an IAM user and then use it instead of the root user. Root user
  is used only when neceseryy absolutely. Root user is like a master key that we keep somewhere just in case of
  emergency.
- **What a budget does (and doesn't do):** It reminds us if we spend a certain amount - It doesn't kill the processes
  if it exceedes that amount only we can shut it down.

## Agent's corrections

*(Review 2026-10-07. Damjan's text above is unchanged.)*

### Real gate system: compared with `01-product-and-domain.md` §6

| # | What you wrote | What the spec says | Verdict |
|---|---|---|---|
| 1 | The loop **arms** the gate: only with a car on the loop can a ticket be taken or a QR scanned, so a pedestrian can't pull tickets | A.1 "loop detector → device wakes up", but B.1 lets the ticket button work with no presence check | **Spec gap → proposed change A** |
| 1 | Scan or press → barrier opens; the Pi tells the printer what to print | B.3 "device prints ticket, opens barrier" | Matches. **Open question:** does the barrier open when the ticket is *printed*, or only when the driver *pulls it out* of the slot? |
| 2 | Not sure | ADR-006: the Pi asks the server on **every** scan (online validation, no local cache). This came from your own description in the planning chat (`kickoff-notes.md`) | **Verify at work.** ADR-006 depends on it |
| 3 | A QR code is printed; the printer is refilled manually | Ticket carries a QR/barcode ✅. The spec says nothing about paper-low / paper-out | **Spec gap → proposed change B** |
| 4 | Staff usually just let the car out, to avoid a queue | Decline → driver retries; timeout → "please wait" + retry; manual open exists | Realistic and important: in practice **manual open is the fallback for payment trouble**. The spec doesn't record the **unpaid fee** when that happens → **proposed change C** |
| 5 | Not sure | Fail closed: no network = no entry; operator manual open; offline mode is a stretch goal | **Verify at work** |
| 6 | All of these happen | Reversing → passage `ABORTED`; misfiring loop → hold `EXPIRED` after 60 s; QR twice → `ALREADY_INSIDE` / `ENTRY_IN_PROGRESS`; lost ticket → operator flow + fee. **Tailgating is not handled at all** | Next step: one concrete example each from work. Tailgating → **proposed change D** |
| 7 | Staff can open remotely; it's logged | §6 F: manual open, audited with user + reason ✅ | Matches |
| 8 | Prices are okay | 15 free min, 300 RSD/started hour, 1,500 RSD daily cap, 2,000 RSD lost ticket | Confirmed → open item in `kickoff-notes.md` closed |

**Spec changes. Status after Damjan's follow-ups (2026-10-07):**
- ✅ **A applied** (Damjan confirmed: the button doesn't work without a vehicle on the loop). The gate-controller
  state machine in `02` §10 already had `IDLE ─car on loop─▶ WAITING_FOR_INPUT`; `01` §6 A/B now say it explicitly.
- ✅ **New, applied:** the barrier opens only when the driver **pulls the ticket out** (ticket-taken sensor):
  `01` §6 B.3 and the `PRINTING` state in `02` §10. Ticket never taken → hold expires after 60 s.
- ✅ **B applied, simplified:** printer status `OK`/`PAPER_LOW`/`PAPER_OUT` in the heartbeat (no `JAM`), a dashboard
  alert, and drive-up refused locally on `PAPER_OUT`.
- ✅ **C applied:** manual exit with a fee due → passage `manual`, "released unpaid" amount, new metric in `01` §10.
- ❌ **D dropped:** the safety loop stops the barrier closing on a car (hardware); tailgating isn't detected, as in
  the real installation. Written down as an accepted limitation (`01` §6 G).
- Damjan's stance recorded in `kickoff-notes.md`: the domain may be simplified or invented; the architecture is not
  simplified.

Original proposals:
- **A. Arming loop:** the ticket button and the QR scanner are only accepted while the arming (presence) loop
  detects a vehicle; otherwise the Pi ignores them locally, and the server is never asked. Affects the
  gate-controller state machine (Phase 4).
- **B. Printer status:** the device reports printer state (`OK` / `PAPER_LOW` / `PAPER_OUT` / `JAM`) in its
  heartbeat. With `PAPER_OUT`/`JAM`, drive-up issue is refused locally ("Please use another lane"), pre-booked QR
  scans still work, and staff get an alert on the dashboard.
- **C. Unpaid exit via manual open:** a manual open at an exit lane with a fee due records the passage as
  `manual`, with the reason and the **outstanding amount**. The revenue report shows it as "released unpaid", so the
  cashed and lost amounts reconcile.
- **D. Tailgating:** the exit-side loop counts vehicles. A second vehicle passing on one authorization is recorded as
  a `TAILGATE` event for the operator, which shows that the zone counter may be off by one. (Detection and logging
  only; we can't stop a car.)

### Concepts
- **Region vs AZ (partly right).** ✅ Region = a geographic area; pick it for latency to you and your users (also law
  and price). ✅ The ALB needs subnets in ≥ 2 AZs to be created. ❌ **AZs are not "backups to the main stuff."**
  There is no "main" AZ. Each AZ is an independent group of data centres (own power, cooling, network) inside the
  region, and you run copies **in parallel** in several AZs at the same time, so if one AZ fails the others keep
  serving. "Backup" suggests something sitting idle until needed; multi-AZ is "active in all of them at once".
- **Shared responsibility (not answered).** AWS secures **the cloud itself**: buildings, hardware, network, the
  virtualisation layer, and the software of managed services (e.g. patching Postgres on RDS). **You** secure what you
  put **in** it: who has access (IAM), network rules (security groups), your data (public or private, encryption),
  secrets, and your code and container images. Example: a public S3 bucket leak is the customer's fault, not AWS's.
  See `study/phase-00.md` §3.
- **Root user (right).** ✅ Master key, used only when necessary, daily work as an IAM user. Add the **why**: root
  can't be restricted by any IAM policy, because it's the account owner, not an IAM identity. So its only protection is
  a strong password + MFA + no access keys + a secured email account. (You did all of that.)
- **Budget (right).** ✅ An alert, not a limit; only you stop the spending. Add: alerts arrive **hours late** (budgets
  update a few times a day); what actually stops charges is **deleting resources** (`terraform destroy`), and the
  final guarantee is the **Free plan**: worst case the account closes, never a bill. Also the budget must **exclude
  credits**, or it always shows $0.

## Experiments

## Checkpoint answers
1. What's the difference between a region and an AZ, and why does the ALB need two AZs?
   > Region is a geographical location of the server - We choose it based on our location aswell as the location of
   > our users for low latency. ALB needs two AZs to work so if one fails for some reason there's a backup and nothing
   > crashes ( something may crash but the damage will be reduced ) AZ is a group of data centres.
2. Why should you never use the root user day-to-day?
   > Because root user acts like a Master key in a hotel - You don't use it unless absolutely necessary.  You create
   > an IAM user and use it instead. Root user cannot be restricted by any IAM policy, the only protection for the Root
   > user is MFA and a strong password.
3. Does a budget stop AWS from charging you? What does?
   > It does not. Budget only serves us as an alert when we hit a certain amount of credits spent. The only way we can
   > stop charging is to shut down our AWS services ourselves.
4. In one minute, explain the EventPark architecture from gate scan to dashboard update.
   Damjan filled in a sentence scaffold (his words in **bold**; ⚠️ = blank he left open, filled by the agent):
   1. The gate (Pi) sends the scan to the **API**, together with a unique ⚠️ *`scan_id` (idempotency key)* so a retry is safe.
   2. The API decides in one **transaction in PostgreSQL**, and in that same transaction also writes ⚠️ *an outbox row
      (the "note")*.
   3. It answers **OPEN** and the barrier opens. When the car has passed, the ⚠️ *loop detector* (via the Pi) tells the API.
   4. A separate program, the **outbox relay**, picks up the note and sends it to **SQS**.
   5. **Workers** take the message, update the statistics and publish the news through **Redis** (pub/sub).
   6. Every API instance hears it and pushes it over a **WebSocket** to the browser: the dashboard changes **1–2** s later.
   7. ⚠️ *Why: the gate decision is synchronous, fast and correct (one Postgres transaction, the source of truth);
      everything else is asynchronous through outbox → SQS → workers, so slow side effects can't slow the barrier.*

   **Agent's review:** ✅ every component in the right order: API → Postgres → outbox relay → SQS → worker → Redis →
   WebSocket → dashboard. Missing: the **idempotency key**, the **outbox row** written in the same transaction (the
   heart of the design), the **loop detector** completing the passage, and the **why**. Practise saying all 7 lines
   out loud as one story, without the scaffold.

### Agent's review of the checkpoint answers (2026-10-07)
1. 🟨 **Mostly right.** ✅ Region chosen for latency to you and your users; ✅ an AZ is a group of data centres;
   ✅ two AZs reduce the damage when one fails. Two fixes: (a) a region is a geographic area with **several AZs**, not
   "the location of the server"; your servers are in specific AZs inside it. (b) It's still not a **backup**: both AZs
   serve traffic **at the same time**, so when one fails the other is already running. And precisely, the ALB needs
   two AZs **to be created at all**; AWS refuses a single-AZ load balancer.
2. ✅ **Right**, including the key point that no IAM policy can restrict root. Add **no root access keys** and **securing
   the email account** (root's password reset goes there).
3. ✅ **Right:** an alert, not a limit; only deleting resources stops charges. Two refinements: the budget measures
   **cost** (with credits *excluded*, otherwise it shows $0), not "credits spent"; and in this project the way to shut
   things down is **`terraform destroy`**, with the **Free plan** as the final guarantee (account closes, never a bill).
   Remember the alerts arrive **hours late**.
- **Shared responsibility (revised).** ✅ Right idea. The standard wording is **"security OF the cloud"** (AWS:
  buildings, hardware, network, managed-service software) vs **"security IN the cloud"** (you: access, network rules,
  data, secrets, code). ✅ A leaked database is almost always the customer's fault: a public endpoint, a weak or
  leaked password, an open security group.

## Things I'm still unsure about
- What the Pi decides locally vs what it asks the server (gate question 2)
- What happens at the gate when the network drops (gate question 5)
- How to explain the whole architecture in one minute (checkpoint 4)

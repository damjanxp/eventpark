# Phase 00 — Foundations & a safe AWS account

## Before building: my understanding

### The real gate system at work (you are the domain expert)
Write it in your own words. Don't look anything up and don't open `01-product-and-domain.md` first.
Being wrong or incomplete is fine.

1. **A car arrives, step by step:** loop detector → … → barrier closes. What does the driver see on the display at each step?
2. **Who decides what:** what does the Pi do locally, and what does it ask the server? What does a request look like
   (roughly), and how long does the server usually take to answer?
3. **Printer / drive-up tickets:** what's printed on the ticket? What happens on a paper jam or when the printer is empty?
4. **Paid exit:** how does paying at the exit lane terminal work? What happens when a card is declined, or the terminal hangs?
5. **Network drops:** what does the Pi do when the server doesn't answer? Does the barrier stay shut? Is there an
   offline mode or a local whitelist?
6. **Things that go wrong in practice:** cars reversing out, tailgating (two cars, one opening), lost tickets, a
   loop detector misfiring, people scanning the same QR twice, a barrier hitting a car…
7. **Operators:** what can staff do remotely (manual open, etc.)? Is it logged?
8. **Pricing in real installations:** are free minutes / per-started-hour / daily cap normal? Our defaults are
   15 free min, 300 RSD per started hour, 1,500 RSD daily cap, 2,000 RSD lost-ticket fee. Realistic?

### Concepts in my words (2–3 sentences each)
- Region vs Availability Zone:
- Shared responsibility:
- Why the root user goes "in the safe":
- What a budget does (and doesn't do):

## Agent's corrections

## Experiments

## Checkpoint answers
1. What's the difference between a region and an AZ, and why does the ALB need two AZs?
2. Why should you never use the root user day-to-day?
3. Does a budget stop AWS from charging you? What does?
4. In one minute, explain the EventPark architecture from gate scan to dashboard update.

## Things I'm still unsure about

# Phase 02 — Domain model, auth & admin API

## Before building: my understanding
<!-- Answer in your own words, before looking anything up. Being wrong here is useful. -->

**Design the tables yourself, before opening `02-architecture.md` §4 (Data model).**
Think about one venue (a stadium car park) that hosts many events. Bullet points are fine, no SQL needed.

1. **Entities:** which "things" need their own table? (Hint: where do cars park, where do they drive in and out,
   what physical box sits at each gate, what is an event, what does parking cost at an event?)

2. **Keys and links:** for each table, what points at what? (e.g. "a zone belongs to one venue")

3. **Rules the database itself should enforce:** pick at least 3 rules that must *never* be broken, even if the
   Python code has a bug. (e.g. "two gates at the same venue can't have the same code")

4. **One question about auth:** a JWT can be read by anyone who has it. So why can't a driver just edit their token
   to say `"role": "ADMIN"`?

## Agent's corrections

## Experiments

## Checkpoint answers
1. …

## Things I'm still unsure about

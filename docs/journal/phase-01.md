# Phase 01 — Local walking skeleton + CI

## Before building: my understanding
<!-- Answer in your own words, before looking anything up. Being wrong here is useful. -->

*(Answered in chat on 2026-10-07, copied here word for word.)*

1. **Image vs container:** what is each one, and how are they related?
   > Image is a set of rules that define our container, container is the instance that is running.

2. **How the API container finds Postgres:** the API runs in one container and Postgres in another. How does the
   API reach it, and what address does it use?
   > API containter finds it via its own DNS inside of the Docker.

3. **Why config comes from env vars:** why not put the database URL (or a password) in the code or in a config file
   inside the image?
   > I dont know the config answer.

## Agent's corrections

1. 🟨 **Container ✅, image half right.** "The instance that is running" is exactly right: one image can run as many
   containers at the same time (later: 3 API tasks + workers, all from one image). But the **set of rules** is the
   **Dockerfile**, not the image. Three different things:
   - **Dockerfile** = the recipe (text: "start from python-slim, install deps, copy code, run uvicorn").
   - **Image** = what `docker build` produces from the recipe: a frozen, read-only stack of **filesystem layers**
     (one per step) plus metadata (which command to start, env vars, user). Nothing runs; it's a file you can push
     to a registry (ECR in Phase 10).
   - **Container** = a process started from the image, isolated by the Linux kernel (namespaces: own filesystem,
     network, PIDs; cgroups: CPU/memory limits), with a thin **writable layer** on top. Delete the container and
     that layer is gone; that's why Postgres data lives in a **volume**.
   Programming analogy: image = class, container = object.

2. 🟨 **Right idea (DNS), add the details.** Compose creates a private network for the project and an embedded DNS
   server. Every **service name** in `docker-compose.yml` becomes a hostname, so the API connects to
   **`postgres:5432`**: hostname `postgres` (resolved to that container's IP), port 5432 = the port *inside* the
   Postgres container. Two traps:
   - `localhost` inside the API container means **the API container itself**, not your laptop and not Postgres.
   - `ports: "5432:5432"` only matters for **your laptop** (e.g. a DB GUI on `localhost:5432`). Container-to-container
     traffic doesn't need it at all.

3. ❌ **Didn't know; here's the answer.** Three reasons:
   - **One image, every environment.** The same image runs on your laptop, in CI and on AWS. Only the *values*
     differ: locally `DATABASE_URL=...@postgres:5432/...`, on AWS `...@eventpark-dev.xxxx.rds.amazonaws.com:5432/...`.
     If the URL were in code, you'd need a different build per environment, and you'd deploy something different
     from what you tested.
   - **Secrets stay out of git and out of the image.** Anyone who can pull the image can read every file and every
     layer in it (even a file you deleted in a later step is still in the earlier layer). Passwords come in at
     runtime: `.env` locally (gitignored), SSM Parameter Store on AWS (ADR-014).
   - **Change settings without rebuilding.** Change a log level or a queue URL by restarting with a new env var,
     no new build.
   We read them with **pydantic-settings**, which also **validates** them at startup: if `DATABASE_URL` is missing or
   malformed, the app refuses to start with a clear error ("fail fast") instead of crashing on the first request.
   This is CLAUDE.md §3 "same code, different config".
   👉 **Your turn:** after step 1 (you'll see `config.py`), rewrite this answer in your own words in 2–3 sentences
   under "Things I'm still unsure about", or move it to the checkpoint.

### Follow-up questions (2026-10-13)
*(Damjan's replies copied word for word.)*

**Q1. Why can Valkey run without a volume, while Postgres must have one?**
> What's Valkey? Can you explain that first question and all that i need to know before i can answer it

**Q2. After Postgres came back, the first `/readyz` logged `connection is closed`, the next was fine. Why?**
> I'm not exactly sure, please explain.

**Q3. Why does config come from environment variables?**
> Config is just a configuration of our env, it has the same skeleton but it differs on every device. (?)

**Agent's notes:**
- Q1 and Q2 were explained in chat (Valkey/Redis, container filesystem vs volume, connection pool). Answer them
  again in your own words under "Checkpoint answers" when you're ready.
- Q3 🟨 **Right core idea**: same code ("skeleton"), different values. Two fixes: it differs per **environment**
  (your laptop, CI, AWS), not per device; and you're missing the two other reasons: **secrets stay out of git and
  out of the image**, and you can **change a setting without rebuilding**. Why env vars specifically: every platform
  (Docker Compose, GitHub Actions, ECS) knows how to set them, so the app doesn't care who started it.

## Experiments

## Checkpoint answers
1. …

## Things I'm still unsure about

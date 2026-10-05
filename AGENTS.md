# AGENTS.md

All agent instructions for this repository live in **[CLAUDE.md](./CLAUDE.md)**. Read it fully before
doing anything. It is the single source of truth for both Claude Code and Codex/Astra, so there is
nothing here to keep in sync.

Short version:

- This is a **learning project**. Damjan must understand every part. Explain (ELI5 + real
  explanation) before building, let him write `YOU WRITE` tasks himself, and review his journal
  entries in `docs/journal/`.
- Follow the active phase in `docs/progress.md` / `docs/04-learning-path.md`. Don't jump ahead.
- **Do not simplify the architecture to make implementation easier.** Reasons are in
  `docs/decisions.md`.
- Never run `terraform apply`/`destroy` without explicit approval; the AWS dev environment is
  destroyed at the end of every session.

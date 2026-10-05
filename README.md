# Pocketful Factory

> **WeAreDevelopers × BAND Dark Factory Hackathon** — All 4 stages passing.

A fully autonomous AI factory that built a production-grade payments platform from scratch — zero human code written.

---

## What was built

A containerized payments API and web UI inspired by apps like Revolut and Wise. Three AI agents collaborated autonomously to implement, test and ship it across four progressive stages.

| Stage | What was added | Status |
|---|---|---|
| 1 | Payments API — wallets, send money, request money, bill splits, settlements, idempotent writes, export/import | ✅ Pass |
| 2 | Web UI — dark fintech UI with Tailwind CSS, authorizations and holds, available/held balance display | ✅ Pass |
| 3 | Statements and corrections — historical balances, paginated statements, payment correction history, snapshot pagination, `as_of` and `known_at` queries | ✅ Pass |
| 4 | Refunds and batch corrections — receivers can refund payments, operators can correct multiple payments atomically | ✅ Pass |

---

## The factory

Three Band agents ran on Claude Code and worked without any human input after the initial task message.

| Seat | Handle | Role |
|---|---|---|
| Coordinator | `coordinator` | Read the spec, made the plan, dispatched work, accepted results |
| Builder | `builder` | Implemented every requirement, committed under `builder@factory.local` |
| Reviewer | `reviewer` | Built from scratch, ran the official harness, posted APPROVED or BLOCKED |

**Runtime:** Claude Code · **Model:** claude-sonnet-4-5 · **Permission mode:** Auto

---

## How to run any stage

Each stage is self-contained. To run stage 4 (the most complete version):

```sh
cd stage-4
docker build -t pocketful .
docker run -p 8080:8080 -e PORT=8080 pocketful
```

See `stage-4/RUN.md` for the exact commands.

To run the official harness against any stage:

```sh
cd ~/dark-factory-wearedevs
.venv/bin/python -m harness run --track pocketful --repo /path/to/this/repo --stage 4 --out /tmp/results
```

---

## What the app does

- **Wallets** — every user has a balance in a single currency (EUR, JPY, BHD etc.)
- **Payments** — send money to any user by handle, instantly and atomically
- **Requests** — ask someone for money; they can pay, decline or cancel
- **Splits** — split a bill among multiple people, with exact minor-unit rounding
- **Settlements** — operators can move money across multiple wallets in one atomic batch
- **Authorizations** — place a hold on funds for later capture (like a card pre-auth)
- **Captures** — collect held funds in one or multiple partial captures
- **Statements** — paginated history with opening/closing balances and per-entry deltas
- **Corrections** — amend a payment's amount after the fact with full revision history
- **Refunds** — receivers can refund payments back to the sender
- **Batch corrections** — operators can correct multiple payments atomically
- **Export/import** — full state transfer between containers
- **Idempotent writes** — 10 write paths are safe to retry with the same key

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python |
| Web framework | Flask / FastAPI |
| UI | Tailwind CSS (compiled, self-hosted) · Inter font (self-hosted) |
| Storage | In-memory (no database required by spec) |
| Container | Docker (single container, no compose required at runtime) |

---

## Commits

| Stage | Commit |
|---|---|
| Stage 1 | `4ab8d5bff34a56a9e26e3cd03772428a1945d0ba` |
| Stage 2 | `2c93883` |
| Stage 3 | `37614cfb887f4e38dd5c59151f5b9e9454a2f994` |
| Stage 4 | `e118bf33b7d50d7ad2d6ceed0b51b6a3d148a9d4` |## Architecture

![DARK-FACTORY Architecture](./AI%20Agent%20Factory%20Architecture%20and%20Stage%20Timeline.png)

---

## Team

- **Band account:** sarshiyashah.25iot
- **Hackathon:** WeAreDevelopers × BAND Dark Factory
- **Track:** Pocketful
- **Deadline:** 5 October 2026, 23:59 PDT

# Evergreen Bot -- Rebuild Plan

Decision (2026-11-07): rebuild the bot from scratch, starting with the database,
so any competent developer can understand and maintain it without the original author.

## Ground rules (from the project procedure)
- The live bot (v1) is **not touched** until v2 is built and tested. v1 is tagged and kept as the backup.
- Understand first, plan, get approval, build, test, then deploy. No untested replacement goes live.
- Database changes are additive and written out in chat; the owner runs them in the Supabase SQL Editor.
- Evergreen Econ shares the Supabase project and must keep working.
- Nothing is pushed without the owner's authorisation. Changes arrive as ZIPs with Termux commands.
- Every finished feature gets documented before the next one starts.

## Key dates
- **Dec 5, 2026: the NAP ends.** The war starts after that. v1 must stay stable through it.
- **Nov 28: go / no-go.** If v2 is not proven by then, v1 runs through the war and cutover waits until after it.

## Phases

### Phase 0 -- Safety net (Nov 7-9)
- Tag the current code (`v1-final`) so it can be restored in minutes.
- Export the bot tables as CSV from Supabase (small: the largest has ~1.7k rows).
- Apply migration 002 (Row Level Security, `delete_at`). Optionally 003 for `econ_grants`.
- *Exit:* tag exists, backup saved, verify queries pass, bot and Econ still work.

### Phase 1 -- Database foundation (Nov 7-12)  <- current
- Deliver `schema.sql`, `DATABASE.md`, migrations 002 and 003.
- Decide the open design questions (nation links per user or per server; CHECK constraints; pruning).
- Specify the new data-access layer.
- *Exit:* docs reviewed; further migrations (004+) written, applied and verified.

### Phase 2 -- Core platform (Nov 12-19)
- New project skeleton: configuration and environment validation, logging, health endpoint for Render.
- Database module with one checked request path (no failure that looks like success).
- Politics & War API client: caching, retries, rate-limit handling, "empty answer is an error".
- Discord base: command registration, government-role permissions, error reporting.
- Test harness and a separate **test Discord application and test server**.
- *Exit:* the v2 skeleton starts on Render, connects, answers one command, writes to the test guild's rows.

### Phase 3 -- Features, in order of war-criticality (Nov 19 - Dec 1)
Each feature: audit the old code, agree the behaviour, build, test, document.
1. 3a War alerts, espionage detection, Federal Sphere monitor
2. 3b War rooms, orders, war advisor, simulator
3. 3c Coalitions, DNR, treaty sync
4. 3d Nation linking, government roles
5. 3e Counter requests, raid finder, war finder, revenue
6. 3f AI assistant

### Phase 4 -- Testing and rehearsal (runs alongside Phase 3)
- Unit tests for pure logic (battle simulation, parsers, order builder).
- Test server runs v2 against the shared database using only the test guild's rows.
- Written cutover and rollback checklists.
- **Nov 28 go / no-go.**

### Phase 5 -- Cutover (only if "go"; not during active war days)
- Stop v1, start v2 with the production Discord token, re-sync commands, watch the logs.
- Rollback: redeploy the `v1-final` tag. Target: under 10 minutes.

### Phase 6 -- Handover
- README and operations runbook (Render, Supabase, Discord portal settings, tokens, recovering access).
- "If I disappear" checklist: who holds each account, key and password, and how to rotate them.
- Final check: a developer who has never seen the project can set it up from the docs alone.

## Open decisions
1. New repository (`Evergreen-bot-v2`, recommended) or a `v2/` folder in this repo?
2. `nation_links`: global per Discord user, or per server?
3. Test environment: a second Discord application plus a test server (needed so v2 never shares a token with the live bot).

## Hard lessons already learned (do not repeat)
- Two copies of a bot on one token cause silent failures. One token = one running instance.
- An **Interactions Endpoint URL** on the Discord application sends all slash commands and
  buttons to a web address, and the bot stops receiving them. Keep it empty on the bot's application.
- Render's `print()` output was buffered and hidden from logs. Use logging and flush.
- Never treat "the API returned nothing" as "there are no wars".

# Evergreen Bot -- Database Guide

Authoritative structure: `database/schema.sql`. Change history: `database/migrations/`.
Inspected against the live database on 2026-11-07.

## 1. The basics

- **Database:** Supabase (PostgreSQL). One project shared by two applications:
  - **Evergreen Bot** (this repo) -- the 18 tables documented below.
  - **Evergreen Econ** (repo `evergreen-econ`) -- 18 tables starting with `econ_`.
    **Never alter, rename or drop `econ_` tables from this repo.**
- **How the bot talks to it:** `utils/database.py` calls Supabase's REST API
  (PostgREST) with aiohttp, using the **service key** (`SUPABASE_SERVICE_KEY`).
  The service key bypasses Row Level Security, so the bot always has full access.
- **Row Level Security:** enabled on every bot table by migration 002, with no
  policies. That means the public "anon" key can do nothing. Only the service key works.
  **Keep the service key secret. It is the only thing protecting this data.**
- **Multi-server:** nearly every table is keyed by `guild_id` (a Discord server),
  so each server has its own settings, lists and war rooms. Exceptions are noted below.

## 2. Tables

Row counts are from 2026-11-07 and only give a sense of scale.

### Core settings
| Table | Key | Purpose | Rows |
|---|---|---|---|
| `alerts_config` | `guild_id` | Per-server settings: which P&W alliance the server manages, alert channels (defense, offensive, espionage, MA notify), allied-alliance inclusion, treaty auto-sync (default **off**), auto-counter DMs (default on). | 2 |
| `guild_roles` | `guild_id`, `role_type` | Which Discord role is MA, EA, FA or IA (government). Used for command permissions. | 5 |
| `nation_links` | `discord_user_id` | Links a Discord user to a P&W nation. | 85 |

### War alerts and espionage
| Table | Key | Purpose | Rows |
|---|---|---|---|
| `seen_wars` | `guild_id`, `war_id` | Remembers which wars were already announced (defense / offensive) so alerts are not repeated. Grows forever. | 871 |
| `nation_spy_tracking` | `guild_id`, `nation_id` | Last known spy count per nation; a drop reveals a spy operation. | 87 |

### Coalitions and Do-Not-Raid
| Table | Key | Purpose | Rows |
|---|---|---|---|
| `coalitions` | `guild_id`, `alliance_id`, `bloc_type` | Alliance lists. `bloc_type` is `ALLIES`, `DNR` or `EXTENSION`. `auto_synced = true` means a treaty sync added it. | 29 |
| `dnr_settings` | `guild_id` | `top_x`: protect the top X alliances from raids (0 = off). | 0 |
| `bot_flags` | `guild_id`, `flag_name` | One-time command markers (currently `bloc_syncnow`). | 1 |

### War rooms
| Table | Key | Purpose | Rows |
|---|---|---|---|
| `warroom_settings` | `guild_id` | Whether war rooms are enabled; category name prefix. | 1 |
| `warroom_brackets` | `guild_id`, `min_cities`, `max_cities` | City-count ranges; `label` is the Discord category name (e.g. `c1-20`). | 4 |
| `war_rooms` | `guild_id`, `enemy_nation_id` | One row per room: channel, category, pinned message, objective, last orders, per-member assignments (jsonb), `missing_count` (loop cycles the enemy was absent; the room auto-closes at a threshold), `delete_at` (scheduled deletion; added by migration 002). | 14 |
| `war_room_participants` | `channel_id`, `discord_user_id` | Members granted access to a room. No `guild_id`. | 633 |
| `seen_attacks` | `guild_id`, `attack_id` | Attacks already posted to a room. Grows forever. | 1733 |

### AI
| Table | Key | Purpose | Rows |
|---|---|---|---|
| `ai_knowledge` | `id` | Text the AI can draw on (source, content, url). Indexed by guild and date. | 7 |

### Federal Sphere (migration 001)
Used by `cogs/sphere.py` and `utils/sphere_db.py`. *These files have not yet been
audited for this guide.*

| Table | Key | Purpose | Rows |
|---|---|---|---|
| `sphere_config` | `guild_id` | Sphere alert channels (offensive, defensive, spy). | 1 |
| `sphere_alliances` | `id`; unique (`guild_id`, `alliance_id`) | Alliances in the sphere. | 2 |
| `sphere_wars` | `id`; unique (`guild_id`, `alliance_id`, `war_id`) | Wars involving sphere alliances, with alert flags and first/last seen times. | 73 |
| `sphere_spy_tracking` | `id`; unique (`guild_id`, `nation_id`) | Spy counts for sphere nations. | 0 |

### Econ tables (hands off)
`econ_audit_log, econ_bank_deposit_records, econ_grants, econ_ledger, econ_loans,
econ_locutus_pending, econ_locutus_recovery, econ_locutus_recovery_imports,
econ_members, econ_pending_deposits, econ_requests, econ_security_logs,
econ_settings, econ_staff, econ_tax_rates, econ_warchest_overrides,
econ_warchest_rules, econ_withdrawals`
(`econ_grants` has Row Level Security off; see migration 003, optional.)

## 3. Known issues (as of 2026-11-07)

1. **Failed lookups read as "yes".** In `utils/database.py`, read functions do not
   check the HTTP status. An error response counts as true in `is_attack_seen`,
   `is_flag_used`, `is_alliance_in_bloc` and `is_room_participant`.
2. **Inconsistent errors.** Some functions raise, some print, some return an empty result.
3. **`delete_at` was missing** until migration 002, so scheduled war-room deletes
   could not be saved. The bot still keeps those timers in memory until the code is updated.
4. **`nation_links` is global per Discord user.** Linking in a second server overwrites
   the first. `nation_id` is not unique: two accounts can link the same nation.
5. **No foreign keys.** Deleting a `war_rooms` row leaves `war_room_participants`
   rows behind.
6. **Unbounded growth:** `seen_wars`, `seen_attacks` (and `sphere_wars`) are never pruned,
   and have no timestamps to prune by.
7. **Free-text type columns** (`bloc_type`, `role_type`, `war_type`) have no CHECK constraint.
8. `order_assignments` is jsonb but the code stores a JSON *string* inside it.

## 4. Planned for the rebuild (not yet applied)

- One database module with a single request function: status checks, retries,
  consistent logging and errors; failures must never look like "true".
- CHECK constraints for the type columns; `created_at` on `seen_*` plus a pruning job.
- A decision on `nation_links`: stay global per user, or become per server?
- Persist scheduled deletes using `war_rooms.delete_at` and recover them after a restart.
- Foreign keys where a unique parent key exists (needs a unique `channel_id` on `war_rooms`).

## 5. Rules for changing the database

1. Inspect the live structure first. Never assume a table or column exists.
2. Write the change as a numbered file in `database/migrations/` and keep it **additive**.
3. Run it in the Supabase SQL Editor, then run its VERIFY queries.
4. Update `database/schema.sql` and this guide in the same change.
5. Never drop or rename a table, and never delete rows, without explicit approval.
6. Never touch `econ_` tables from this repo.

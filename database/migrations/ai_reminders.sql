-- Reminders / scheduled channel actions for the AI, plus each person's own timezone.
-- Run once in the Supabase SQL editor. Safe to run again.

create table if not exists public.ai_reminders (
    id            bigint generated always as identity primary key,
    guild_id      bigint      not null,
    channel_id    bigint      not null,
    requester_id  bigint      not null,
    kind          text        not null check (kind in ('remind', 'ping', 'delete', 'lock', 'unlock')),
    target_ids    bigint[]    not null default '{}',
    message       text        not null default '',
    due_at        timestamptz not null,
    status        text        not null default 'pending'
                  check (status in ('pending', 'done', 'cancelled', 'failed', 'missed')),
    created_at    timestamptz not null default now()
);

create index if not exists ai_reminders_pending_idx
    on public.ai_reminders (status, due_at);

create table if not exists public.ai_user_timezones (
    user_id     bigint primary key,
    tz          text        not null,
    updated_at  timestamptz not null default now()
);

-- Same as every other bot table: RLS on (the bot's service key bypasses it, anonymous access is blocked).
alter table public.ai_reminders enable row level security;
alter table public.ai_user_timezones enable row level security;

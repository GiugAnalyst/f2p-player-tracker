-- Run once in Supabase: SQL Editor > New query > paste > Run
-- Then run setup_pg_cron.sql to start the hourly collection.

create table public.player_counts (
    id            bigint generated always as identity primary key,
    app_id        integer     not null,             -- Steam app ID (570 = Dota 2, 2707930 = Palia)
    game          text        not null,
    player_count  integer     not null,             -- concurrent players on Steam at collection time
    collected_at  timestamptz not null default now()  -- stored in UTC
);

-- Fast lookups by game and time
create index player_counts_app_time_idx
    on public.player_counts (app_id, collected_at);

-- Lock the table: no access through the public API.
-- The pg_cron jobs run as the table owner, which bypasses this.
alter table public.player_counts enable row level security;

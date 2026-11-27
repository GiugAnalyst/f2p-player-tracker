-- =====================================================================
-- Hourly Steam player counts, collected inside Supabase (PostgreSQL)
-- with pg_cron (scheduling) and pg_net (asynchronous HTTP requests).
--
-- Run this AFTER setup_table.sql, in the Supabase SQL Editor.
--
-- How it works:
--   minute 0 of every hour  -> request_player_counts() sends one HTTP
--                              request per game to the Steam Web API and
--                              remembers each request in steam_requests.
--   minute 2 of every hour  -> store_player_counts() reads the answers,
--                              saves the player counts in player_counts
--                              (timestamped with the request time), and
--                              cleans up steam_requests.
--
-- pg_net requests are asynchronous: the answer arrives a moment after the
-- request, which is why requesting and storing are two separate jobs.
--
-- The Steam endpoint used here needs no API key, so no secrets are stored
-- in the database or in this file.
--
-- History: the first version of this collector ran as a Python script on
-- GitHub Actions. Scheduled runs there proved unreliable (delayed or
-- skipped hours), so collection moved into the database itself.
-- Clean hourly data starts on 2026-10-28 10:00 UTC.
-- =====================================================================


-- 1. Extensions (in Supabase they can also be enabled under
--    Database > Extensions)
create extension if not exists pg_cron;
create extension if not exists pg_net;


-- 2. Helper table: requests sent to Steam that are waiting for an answer
create table if not exists public.steam_requests (
    request_id   bigint primary key,           -- id returned by net.http_get
    app_id       integer not null,
    game         text not null,
    requested_at timestamptz not null default now()
);

-- Row level security with no policies: the table cannot be read or changed
-- through the public API. The cron jobs run as the table owner, which
-- bypasses RLS, so collection is not affected.
alter table public.steam_requests enable row level security;


-- 3. Minute 0: send one request per game
create or replace function public.request_player_counts()
returns void
language plpgsql
as $function$
declare
    g record;
begin
    for g in select * from (values (570, 'Dota 2'), (2707930, 'Palia')) as t(app_id, game) loop
        insert into public.steam_requests (request_id, app_id, game)
        values (
            net.http_get(
                url := 'https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=' || g.app_id
            ),
            g.app_id, g.game
        );
    end loop;
end;
$function$;


-- 4. Minute 2: store the answers, then clean up
create or replace function public.store_player_counts()
returns void
language plpgsql
as $function$
begin
    -- keep only successful answers (HTTP 200, JSON, Steam result = 1)
    insert into public.player_counts (app_id, game, player_count, collected_at)
    select r.app_id,
           r.game,
           case when resp.content_type like 'application/json%'
                then (resp.content::json -> 'response' ->> 'player_count')::int end,
           r.requested_at
    from public.steam_requests r
    join net._http_response resp on resp.id = r.request_id
    where resp.status_code = 200
      and case when resp.content_type like 'application/json%'
               then (resp.content::json -> 'response' ->> 'result') = '1'
               else false end;

    -- clean up: answered requests, and any older than 1 hour that never got an answer
    delete from public.steam_requests r
    where exists (select 1 from net._http_response resp where resp.id = r.request_id)
       or r.requested_at < now() - interval '1 hour';
end;
$function$;


-- 5. Only the database itself may run these functions
revoke execute on function public.request_player_counts() from public, anon, authenticated;
revoke execute on function public.store_player_counts()   from public, anon, authenticated;


-- 6. Schedules (times are UTC)
select cron.schedule('request-player-counts', '0 * * * *', $$select public.request_player_counts()$$);
select cron.schedule('store-player-counts',   '2 * * * *', $$select public.store_player_counts()$$);


-- =====================================================================
-- Health checks (run between minute 3 and 59 of an hour, so both jobs
-- show the same number of runs)
-- =====================================================================

-- Total rows and latest saved hour
-- select count(*) as total_rows, max(collected_at) as latest_row
-- from public.player_counts;

-- Hourly runs per day (24 on every full day)
-- select date(collected_at) as day, count(*) / 2 as hourly_runs
-- from public.player_counts
-- where collected_at >= '2026-10-28 10:00+00'
-- group by day order by day;

-- Job runs by status (no 'failed' rows expected)
-- select jobid, status, count(*) as runs, max(start_time) as last_run
-- from cron.job_run_details
-- group by jobid, status order by jobid, status;

-- Requests still waiting for an answer (0 expected)
-- select count(*) from public.steam_requests;

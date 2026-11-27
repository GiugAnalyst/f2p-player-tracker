"""
Collect a sample of public Dota 2 matches from OpenDota, spread over the
last year, for match-level analysis (match length, ranks, game modes,
regions, hero picks and win rates, change over time).

How the sample is spread:
  For each day going back DAYS days, the script jumps to a match played that
  day (at a different hour each day, so all hours of the day get covered)
  and saves PAGES_PER_DAY pages of 100 matches.

Match data contains no player information, so there is nothing personal
to protect here.

The script can be stopped and run again: it remembers which days are done.

Usage:
  python opendota_matches.py --days 30         # quick test: last 30 days
  python opendota_matches.py                   # full year
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API = "https://api.opendota.com/api"
DATA_DIR = Path("data")
MATCHES_FILE = DATA_DIR / "dota_matches.csv"
HEROES_FILE = DATA_DIR / "dota_heroes.csv"
STATE_FILE = DATA_DIR / "dota_matches_progress.json"

SECONDS_BETWEEN_CALLS = 1.1   # stays under the free tier's 60 calls/minute
TIMEOUT = 30

FIELDS = [
    "match_id", "start_time_utc", "duration_sec", "lobby_type", "game_mode",
    "avg_rank_tier", "num_rank_tier", "cluster", "radiant_win",
    "radiant_heroes", "dire_heroes",
]


class BudgetExhausted(Exception):
    pass


class Budget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0


def get(path: str, budget: Budget, params: dict | None = None):
    """GET from OpenDota with rate limiting, retries, and budget tracking."""
    params = dict(params or {})
    key = os.getenv("OPENDOTA_API_KEY")
    if key:
        params["api_key"] = key
    for attempt in range(4):
        if budget.used >= budget.limit:
            raise BudgetExhausted
        time.sleep(SECONDS_BETWEEN_CALLS)
        budget.used += 1
        try:
            r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            print(f"  network error ({exc}); retrying")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:
            print("  rate limited; waiting 60s")
            time.sleep(60)
            continue
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    return None


def to_utc(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""


def heroes_text(team) -> str:
    """Hero IDs as '1|14|35|...' (the API returns a list or a comma string)."""
    if isinstance(team, list):
        return "|".join(str(h) for h in team)
    if isinstance(team, str):
        return team.replace(",", "|")
    return ""


def to_row(m: dict) -> dict:
    return {
        "match_id": m.get("match_id"),
        "start_time_utc": to_utc(m.get("start_time")),
        "duration_sec": m.get("duration"),
        "lobby_type": m.get("lobby_type"),
        "game_mode": m.get("game_mode"),
        "avg_rank_tier": m.get("avg_rank_tier"),
        "num_rank_tier": m.get("num_rank_tier"),
        "cluster": m.get("cluster"),
        "radiant_win": int(bool(m.get("radiant_win"))),
        "radiant_heroes": heroes_text(m.get("radiant_team")),
        "dire_heroes": heroes_text(m.get("dire_team")),
    }


def save_rows(rows: list[dict]) -> None:
    new_file = not MATCHES_FILE.exists()
    with MATCHES_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def load_seen_ids() -> set:
    if not MATCHES_FILE.exists():
        return set()
    with MATCHES_FILE.open(newline="") as f:
        return {row["match_id"] for row in csv.DictReader(f)}


def save_heroes(budget: Budget) -> None:
    """Hero ID -> name list, downloaded once."""
    if HEROES_FILE.exists():
        return
    heroes = get("/heroes", budget) or []
    with HEROES_FILE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hero_id", "hero_name", "primary_attr", "attack_type"])
        for h in heroes:
            w.writerow([h.get("id"), h.get("localized_name"), h.get("primary_attr"), h.get("attack_type")])
    print(f"Saved {len(heroes)} heroes to {HEROES_FILE}")


def page_before(match_id: int | None, budget: Budget) -> list[dict]:
    params = {"less_than_match_id": match_id} if match_id else None
    return get("/publicMatches", budget, params) or []


def oldest(page: list[dict]) -> dict:
    return min(page, key=lambda m: m["match_id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365, help="how many days back to cover")
    ap.add_argument("--pages-per-day", type=int, default=2, help="pages of 100 matches per day")
    ap.add_argument("--budget", type=int, default=1500, help="max API calls this run")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    budget = Budget(args.budget)
    seen = load_seen_ids()

    # Progress: which day comes next, plus the last known (match ID, time) point
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    next_day = state.get("next_day", 0)
    if next_day >= args.days:
        print(f"All {args.days} days already collected ({len(seen)} matches). "
              "Use a larger --days to go further back.")
        return 0

    saved = 0
    try:
        save_heroes(budget)

        # Two reference points tell us how fast match IDs grow (IDs per second)
        if "ref_id" not in state:
            latest = page_before(None, budget)
            if not latest:
                print("OpenDota returned no public matches.", file=sys.stderr)
                return 1
            a = oldest(latest)
            b_page = page_before(a["match_id"] - 2_000_000, budget)
            b = oldest(b_page)
            state.update(ref_id=a["match_id"], ref_time=a["start_time"],
                         rate=(a["match_id"] - b["match_id"]) / max(a["start_time"] - b["start_time"], 1))

        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        print(f"Collecting days {next_day + 1} to {args.days} "
              f"(already have {len(seen)} matches)")

        for day in range(next_day, args.days):
            hour = (day * 7) % 24  # rotate the hour so every hour gets covered
            target = today - timedelta(days=day) + timedelta(hours=hour)
            if target > datetime.now(timezone.utc):
                target -= timedelta(days=1)
            target_ts = target.timestamp()

            # Jump toward the target time; if we land more than 2 hours off,
            # use what we just learned to aim again (up to 3 tries).
            page = None
            for _attempt in range(3):
                guess = int(state["ref_id"] - (state["ref_time"] - target_ts) * state["rate"])
                page = page_before(guess, budget)
                if not page:
                    break
                o = oldest(page)
                # Learn from where we landed, so the next jump is more accurate
                if o["start_time"] != state["ref_time"]:
                    new_rate = (state["ref_id"] - o["match_id"]) / (state["ref_time"] - o["start_time"])
                    if new_rate > 0:
                        state["rate"] = new_rate
                state["ref_id"], state["ref_time"] = o["match_id"], o["start_time"]
                if abs(o["start_time"] - target_ts) <= 2 * 3600:
                    break
            if not page:
                print(f"No public matches before {target.date()}; stopping here.")
                break

            pages = [page]
            for _ in range(args.pages_per_day - 1):
                more = page_before(oldest(pages[-1])["match_id"], budget)
                if not more:
                    break
                pages.append(more)

            rows = [to_row(m) for p in pages for m in p if str(m.get("match_id")) not in seen]
            save_rows(rows)
            seen.update(str(r["match_id"]) for r in rows)
            saved += len(rows)
            print(f"  day {day + 1:>3}: aimed at {target:%Y-%m-%d %H:00}, "
                  f"got matches from {to_utc(o['start_time'])[:16]} -> {len(rows)} saved")

            state["next_day"] = day + 1
            STATE_FILE.write_text(json.dumps(state))
    except BudgetExhausted:
        print("Call budget reached; stopping. Run again to continue.")
    finally:
        if state:
            STATE_FILE.write_text(json.dumps(state))

    print(f"Done: {saved} new matches this run, {len(seen)} in total, {budget.used} API calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())

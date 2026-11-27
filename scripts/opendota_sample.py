"""
Sample Dota 2 players from recent public matches (OpenDota) and download
their match history, pseudonymized, for engagement / churn / segmentation
analysis.

Run locally, as often as you like (e.g. once a day). Each run:
  1. takes recent public matches from /publicMatches,
  2. opens each match and picks the players whose match data is public,
  3. downloads each new player's matches from the last HISTORY_DAYS days,
  4. saves rows to data/ with the account ID replaced by a salted hash.

It stops on its own before the daily call budget is used up, and the next
run continues where it left off (finished matches are remembered).

Privacy:
  - Raw Steam account IDs are never written to disk.
  - Match IDs are dropped from player history, so rows can't be looked up
    on OpenDota to re-identify a player.
  - The data/ folder is in .gitignore. Publish only aggregated results.

Usage:
  python opendota_sample.py               # default budget
  python opendota_sample.py --budget 300  # smaller test run
"""

import argparse
import csv
import hashlib
import hmac
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API = "https://api.opendota.com/api"
DATA_DIR = Path("data")
PLAYERS_FILE = DATA_DIR / "dota_players.csv"
MATCHES_FILE = DATA_DIR / "dota_player_matches.csv"
DONE_FILE = DATA_DIR / "dota_processed_matches.txt"

HISTORY_DAYS = 365          # how far back each player's history goes
SECONDS_BETWEEN_CALLS = 1.1 # stays under the free tier's 60 calls/minute
DEFAULT_BUDGET = 1500       # stays under the ~2,000 calls/day free limit
TIMEOUT = 30

PLAYER_FIELDS = ["player_hash", "sampled_at", "sample_match_avg_rank_tier"]
MATCH_FIELDS = [
    "player_hash", "start_time_utc", "duration_sec", "game_mode", "lobby_type",
    "hero_id", "win", "kills", "deaths", "assists", "party_size",
    "average_rank", "leaver_status",
]


class Budget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def left(self) -> int:
        return self.limit - self.used


def get(path: str, budget: Budget, params: dict | None = None):
    """GET from OpenDota with rate limiting, retries, and budget tracking."""
    params = dict(params or {})
    key = os.getenv("OPENDOTA_API_KEY")
    if key:
        params["api_key"] = key

    for attempt in range(4):
        if budget.left() <= 0:
            raise BudgetExhausted
        time.sleep(SECONDS_BETWEEN_CALLS)
        budget.used += 1
        try:
            r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            print(f"  network error ({exc}); retrying")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:  # rate limited: wait longer and retry
            print("  rate limited; waiting 60s")
            time.sleep(60)
            continue
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    return None


class BudgetExhausted(Exception):
    pass


def pseudonymize(account_id: int, salt: bytes) -> str:
    """Salted hash: same player -> same code, but not reversible without the salt."""
    return hmac.new(salt, str(account_id).encode(), hashlib.sha256).hexdigest()[:16]


def load_set(path: Path, column: str | None = None) -> set:
    if not path.exists():
        return set()
    if column is None:
        return set(path.read_text().split())
    with path.open(newline="") as f:
        return {row[column] for row in csv.DictReader(f)}


def append_rows(path: Path, fields: list, rows: list[dict]) -> None:
    if not rows:
        return
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def history_rows(player_hash: str, matches: list[dict]) -> list[dict]:
    rows = []
    for m in matches:
        if m.get("start_time") is None or m.get("player_slot") is None:
            continue
        on_radiant = m["player_slot"] < 128
        rows.append({
            "player_hash": player_hash,
            "start_time_utc": datetime.fromtimestamp(m["start_time"], timezone.utc).isoformat(),
            "duration_sec": m.get("duration"),
            "game_mode": m.get("game_mode"),
            "lobby_type": m.get("lobby_type"),
            "hero_id": m.get("hero_id"),
            "win": int(on_radiant == bool(m.get("radiant_win"))),
            "kills": m.get("kills"),
            "deaths": m.get("deaths"),
            "assists": m.get("assists"),
            "party_size": m.get("party_size"),
            "average_rank": m.get("average_rank"),
            "leaver_status": m.get("leaver_status"),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help="max API calls this run")
    args = parser.parse_args()

    salt = os.getenv("ANON_SALT")
    if not salt or len(salt) < 16:
        print("Set ANON_SALT in .env (a long random string, see README).", file=sys.stderr)
        return 1
    salt = salt.encode()

    DATA_DIR.mkdir(exist_ok=True)
    done_matches = load_set(DONE_FILE)
    known_players = load_set(PLAYERS_FILE, "player_hash")
    budget = Budget(args.budget)
    new_players = new_rows = 0

    try:
        public = get("/publicMatches", budget) or []
        print(f"Got {len(public)} recent public matches")

        for pm in public:
            match_id = str(pm.get("match_id"))
            if match_id in done_matches:
                continue
            match = get(f"/matches/{match_id}", budget)
            if match:
                for p in match.get("players", []):
                    account_id = p.get("account_id")
                    if not account_id:  # anonymous / private profile: skip
                        continue
                    player_hash = pseudonymize(account_id, salt)
                    if player_hash in known_players:
                        continue
                    history = get(f"/players/{account_id}/matches", budget,
                                  {"date": HISTORY_DAYS})
                    if history is None:  # request failed: try this player another time
                        continue
                    rows = history_rows(player_hash, history)
                    append_rows(MATCHES_FILE, MATCH_FIELDS, rows)
                    append_rows(PLAYERS_FILE, PLAYER_FIELDS, [{
                        "player_hash": player_hash,
                        "sampled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                        "sample_match_avg_rank_tier": pm.get("avg_rank_tier"),
                    }])
                    known_players.add(player_hash)
                    new_players += 1
                    new_rows += len(rows)
                    print(f"  player {player_hash}: {len(rows)} matches")
            with DONE_FILE.open("a") as f:
                f.write(match_id + "\n")
            done_matches.add(match_id)
    except BudgetExhausted:
        print("Call budget reached; stopping. Run again later to continue.")

    print(f"Done: {new_players} new players, {new_rows} match rows, "
          f"{budget.used} API calls, {len(known_players)} players in total")
    return 0


if __name__ == "__main__":
    sys.exit(main())

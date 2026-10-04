"""
NFL Sharp-Wallet Consensus (Polymarket)
=======================================

What it does
1. Pulls every Polymarket NFL *game* event this season (moneylines, spreads,
   totals, props, halves, etc.). Season futures are excluded (no game ID).
2. Tags each event to an NFL week (Tuesday 00:00 ET -> Monday 23:59 ET).
3. Pulls every trade in those events (maker and taker fills).
4. Keeps wallets with at least one BUY in every completed week.
5. Computes each wallet's REALIZED NFL profit for the season and ranks by
   dollars (with a minimum-volume floor), keeping the top 10.
6. For the current week, lists the outcomes those 10 wallets hold, ranked by
   number of wallets and by dollars.

Run
    pip install requests pandas
    python nfl_sharps.py

Outputs go to ./output/ ; raw trades are cached in ./cache/ so later runs only
re-download events that are still open.
"""

from __future__ import annotations

import gzip
import json
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
SEASON_START = "2026-09-01"     # games before this date (preseason) are ignored
TOP_N = 10                      # number of wallets to keep
MIN_VOLUME = 1_000              # minimum season buy volume ($) to be ranked
MIN_POSITION_DOLLARS = 1.0      # ignore dust positions in the current-week table
TAKER_ONLY = False              # False = include maker fills (recommended)
WORKERS = 4                     # parallel event downloads
CACHE_DIR = Path("cache")
OUTPUT_DIR = Path("output")

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
ET = ZoneInfo("America/New_York")

TRADE_PAGE = 1000               # rows per trades request
TRADE_OFFSET_CAP = 10_000       # Data API rejects offsets above this

# ----------------------------------------------------------------------------
# HTTP helper with retries
# ----------------------------------------------------------------------------
_session = requests.Session()
_session.headers["User-Agent"] = "nfl-sharps/1.0"


def get_json(url: str, params: dict | None = None, tries: int = 6):
    for attempt in range(tries):
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                raise
            wait = min(60, 2 ** attempt)
            print(f"  retry {attempt + 1}/{tries} in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"Failed after {tries} tries: {url} {params}")


# ----------------------------------------------------------------------------
# Small parsing helpers
# ----------------------------------------------------------------------------
def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []


# ----------------------------------------------------------------------------
# 1. Events and markets
# ----------------------------------------------------------------------------
def nfl_tag_id() -> int:
    return int(get_json(f"{GAMMA}/tags/slug/nfl")["id"])


def fetch_nfl_events(tag_id: int, season_start: datetime) -> list[dict]:
    events: dict[str, dict] = {}
    for closed in ("true", "false"):
        cursor = None
        while True:
            params = {"tag_id": tag_id, "closed": closed, "limit": 100,
                      "start_time_min": season_start.isoformat()}
            if cursor:
                params["after_cursor"] = cursor
            page = get_json(f"{GAMMA}/events/keyset", params)
            for ev in page.get("events", []):
                events[str(ev["id"])] = ev
            cursor = page.get("next_cursor")
            if not cursor:
                break
    return list(events.values())


def game_id_of(ev: dict):
    if ev.get("gameId"):
        return ev["gameId"]
    for m in ev.get("markets") or []:
        if m.get("gameId"):
            return m["gameId"]
        # Live API has no gameId field. Single-game events (incl. props) have
        # gameStartTime set on their markets; season futures do not.
        if m.get("gameStartTime"):
            return m["gameStartTime"]
    return None


def game_time_of(ev: dict) -> datetime | None:
    for key in ("startTime", "eventDate"):
        t = parse_ts(ev.get(key))
        if t:
            return t
    for m in ev.get("markets") or []:
        for key in ("gameStartTime", "eventStartTime"):
            t = parse_ts(m.get(key))
            if t:
                return t
    return None


def tuesday_on_or_before(dt: datetime) -> datetime:
    local = dt.astimezone(ET)
    back = (local.weekday() - 1) % 7          # Monday=0, Tuesday=1
    day = (local - timedelta(days=back)).date()
    return datetime(day.year, day.month, day.day, tzinfo=ET)


def week_of(game_time: datetime, week1_start: datetime) -> int:
    delta = game_time.astimezone(ET) - week1_start
    return int(delta.total_seconds() // (7 * 86400)) + 1


def build_game_events(raw_events: list[dict], season_start: datetime):
    """Keep only single-game events (have a game ID) in the regular season."""
    games = []
    for ev in raw_events:
        if game_id_of(ev) is None:
            continue                                   # futures / season props
        gt = game_time_of(ev)
        if gt is None or gt < season_start:
            continue
        games.append({**ev, "_game_time": gt})
    if not games:
        raise SystemExit("No NFL game events found. Check SEASON_START / API.")
    week1_start = tuesday_on_or_before(min(g["_game_time"] for g in games))
    for g in games:
        g["_week"] = week_of(g["_game_time"], week1_start)
    return games, week1_start


def build_token_map(games: list[dict]) -> dict[str, dict]:
    """asset (token id) -> market info incl. week and settlement payout."""
    tokens = {}
    for ev in games:
        for m in ev.get("markets") or []:
            ids = parse_list(m.get("clobTokenIds"))
            outcomes = parse_list(m.get("outcomes"))
            prices = parse_list(m.get("outcomePrices"))
            resolved = bool(m.get("closed")) and (
                str(m.get("umaResolutionStatus") or "").lower() == "resolved"
                or (prices and all(float(p) in (0.0, 0.5, 1.0) for p in prices))
            )
            for i, tok in enumerate(ids):
                payout = float(prices[i]) if resolved and i < len(prices) else None
                tokens[str(tok)] = {
                    "event_id": str(ev["id"]),
                    "event_title": ev.get("title"),
                    "condition_id": m.get("conditionId"),
                    "question": m.get("question"),
                    "outcome": outcomes[i] if i < len(outcomes) else str(i),
                    "market_type": m.get("sportsMarketType"),
                    "week": ev["_week"],
                    "game_time": ev["_game_time"],
                    "payout": payout,
                    "current_price": _to_float(prices[i]) if i < len(prices) else None,
                }
    return tokens


# ----------------------------------------------------------------------------
# 2. Trades (with time-window splitting past the offset cap)
# ----------------------------------------------------------------------------
def fetch_trades_window(event_id: str, start: int, end: int) -> list[dict]:
    rows, offset = [], 0
    while True:
        page = get_json(f"{DATA}/trades", {
            "eventId": event_id, "takerOnly": str(TAKER_ONLY).lower(),
            "limit": TRADE_PAGE, "offset": offset, "start": start, "end": end})
        rows.extend(page)
        if len(page) < TRADE_PAGE:
            return rows
        offset += TRADE_PAGE
        if offset > TRADE_OFFSET_CAP:
            if end - start <= 1:
                print(f"  WARNING event {event_id}: >{TRADE_OFFSET_CAP} trades "
                      f"in one second, some rows skipped")
                return rows
            mid = (start + end) // 2
            return (fetch_trades_window(event_id, start, mid)
                    + fetch_trades_window(event_id, mid + 1, end))


def fetch_event_trades(ev: dict, now_ts: int) -> list[dict]:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"trades_{ev['id']}.json.gz"
    if path.exists() and ev.get("closed"):
        with gzip.open(path, "rt") as f:
            return json.load(f)
    created = parse_ts(ev.get("creationDate") or ev.get("createdAt"))
    start = int((created - timedelta(days=1)).timestamp()) if created else 1
    rows = fetch_trades_window(str(ev["id"]), max(start, 1), now_ts)
    with gzip.open(path, "wt") as f:
        json.dump(rows, f)
    return rows


def fetch_all_trades(games: list[dict]) -> pd.DataFrame:
    now_ts = int(time.time())
    all_rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_event_trades, ev, now_ts): ev for ev in games}
        for i, fut in enumerate(as_completed(futures), 1):
            ev = futures[fut]
            try:
                all_rows.extend(fut.result())
            except Exception as e:                      # keep going on one bad event
                print(f"  ERROR event {ev['id']} ({ev.get('title')}): {e}")
            if i % 25 == 0 or i == len(futures):
                print(f"  trades: {i}/{len(futures)} events")
    df = pd.DataFrame(all_rows)
    if df.empty:
        raise SystemExit("No trades returned.")
    key = ["transactionHash", "proxyWallet", "asset", "side", "size", "price", "timestamp"]
    df = df.drop_duplicates(subset=[k for k in key if k in df.columns])
    df["asset"] = df["asset"].astype(str)
    df["proxyWallet"] = df["proxyWallet"].str.lower()
    return df


# ----------------------------------------------------------------------------
# 3. Position accounting (average-cost method)
# ----------------------------------------------------------------------------
def account_positions(trades: pd.DataFrame, tokens: dict) -> pd.DataFrame:
    """One row per (wallet, asset): realized P&L, open shares, open cost."""
    out = []
    trades = trades.sort_values("timestamp", kind="stable")
    for (wallet, asset), grp in trades.groupby(["proxyWallet", "asset"], sort=False):
        shares = cost = realized = buy_dollars = 0.0
        oversold = False
        for side, size, price in zip(grp["side"], grp["size"].astype(float),
                                     grp["price"].astype(float)):
            if side == "BUY":
                shares += size
                cost += size * price
                buy_dollars += size * price
            else:
                sell = size
                if sell > shares + 1e-9:          # shares came from a split/convert
                    oversold = True
                    extra = sell - shares
                    realized += extra * price      # unknown basis -> treated as 0
                    sell = shares
                if sell > 0:
                    avg = cost / shares if shares else 0.0
                    realized += sell * (price - avg)
                    cost -= avg * sell
                    shares -= sell
        info = tokens.get(asset, {})
        payout = info.get("payout")
        open_shares, open_cost = shares, cost
        if payout is not None and shares > 1e-9:   # settled: remaining shares realize
            realized += shares * payout - cost
            open_shares, open_cost = 0.0, 0.0
        out.append({
            "wallet": wallet, "asset": asset, "week": info.get("week"),
            "realized_pnl": realized, "buy_dollars": buy_dollars,
            "end_shares": shares, "end_cost": cost,
            "open_shares": open_shares, "open_cost": open_cost,
            "oversold": oversold,
        })
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------
# 4. Analysis pipeline (used by both the command line and the Streamlit app)
# ----------------------------------------------------------------------------
def run_analysis(min_volume: float = MIN_VOLUME, top_n: int = TOP_N, log=print) -> dict:
    OUTPUT_DIR.mkdir(exist_ok=True)
    season_start = datetime.fromisoformat(SEASON_START).replace(tzinfo=ET)
    now = datetime.now(timezone.utc)

    log("1/5 Finding NFL game events...")
    raw = fetch_nfl_events(nfl_tag_id(), season_start)
    games, week1_start = build_game_events(raw, season_start)
    current_week = week_of(now, week1_start)
    completed = list(range(1, current_week))
    tokens = build_token_map(games)
    log(f"  {len(games)} game events; Week 1 starts {week1_start:%a %b %d}; "
        f"current week = {current_week}")
    if not completed:
        raise SystemExit("No completed weeks yet.")

    log("2/5 Downloading trades (settled games load from cache)...")
    trades = fetch_all_trades(games)
    trades = trades[trades["asset"].isin(tokens)].copy()
    trades["week"] = trades["asset"].map(lambda a: tokens[a]["week"])
    log(f"  {len(trades):,} trades, {trades['proxyWallet'].nunique():,} wallets")

    log("3/5 Filtering wallets with a buy in every completed week...")
    buys = trades[trades["side"] == "BUY"]
    weeks_hit = buys.groupby("proxyWallet")["week"].apply(set)
    need = set(completed)
    consistent = weeks_hit[weeks_hit.apply(lambda s: need <= s)].index
    log(f"  {len(consistent):,} wallets bet every week {completed[0]}-{completed[-1]}")

    log("4/5 Computing realized profit...")
    pos = account_positions(trades[trades["proxyWallet"].isin(consistent)], tokens)
    name_cols = [c for c in ("name", "pseudonym") if c in trades.columns]
    summary = pos.groupby("wallet").agg(
        realized_pnl=("realized_pnl", "sum"),
        season_buy_volume=("buy_dollars", "sum"),
        markets_traded=("asset", "nunique"),
        had_unmatched_sells=("oversold", "any"),
    )
    summary["roi_pct"] = 100 * summary["realized_pnl"] / summary["season_buy_volume"]
    if name_cols:
        summary = summary.join(trades.sort_values("timestamp")
                               .groupby("proxyWallet")[name_cols].last(), how="left")
    summary["profile_url"] = "https://polymarket.com/profile/" + summary.index
    eligible = summary[summary["season_buy_volume"] >= min_volume] \
        .sort_values("realized_pnl", ascending=False)
    top = eligible.head(top_n)
    eligible.to_csv(OUTPUT_DIR / "eligible_wallets.csv", index_label="wallet")
    top.to_csv(OUTPUT_DIR / "top_wallets.csv", index_label="wallet")
    log(f"  {len(eligible):,} wallets clear ${min_volume:,.0f} volume")

    log(f"5/5 Building Week {current_week} consensus...")
    cons = build_consensus(pos, top.index, current_week, tokens, now)
    if not cons.empty:
        cons.sort_values(["wallets", "dollars"], ascending=False).to_csv(
            OUTPUT_DIR / f"week{current_week}_by_wallet_count.csv", index=False)
        cons.sort_values(["dollars", "wallets"], ascending=False).to_csv(
            OUTPUT_DIR / f"week{current_week}_by_dollars.csv", index=False)

    res = {
        "run_time": now, "current_week": current_week, "completed_weeks": completed,
        "n_games": len(games), "n_trades": len(trades),
        "n_wallets": int(trades["proxyWallet"].nunique()),
        "n_consistent": len(consistent), "n_eligible": len(eligible),
        "min_volume": min_volume, "top_n": top_n,
        "top": top, "consensus": cons,
    }
    save_results(res)
    return res


# ----------------------------------------------------------------------------
# Saving / loading results (the hosted app reads results/latest.json)
# ----------------------------------------------------------------------------
RESULTS_FILE = Path("results") / "latest.json"


def save_results(res: dict, path: Path = RESULTS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {k: v for k, v in res.items() if k not in ("top", "consensus", "run_time")}
    out["run_time"] = res["run_time"].isoformat()
    out["top"] = json.loads(res["top"].reset_index().rename(
        columns={"index": "wallet"}).to_json(orient="records"))
    cons = res["consensus"].copy()
    if not cons.empty:
        cons["kickoff"] = cons["kickoff"].map(lambda t: t.isoformat())
    out["consensus"] = json.loads(cons.to_json(orient="records"))
    path.write_text(json.dumps(out, indent=1))


def load_results(path: Path = RESULTS_FILE) -> dict | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    raw["run_time"] = datetime.fromisoformat(raw["run_time"])
    top = pd.DataFrame(raw["top"])
    raw["top"] = top.set_index("wallet") if "wallet" in top.columns else top
    cons = pd.DataFrame(raw["consensus"])
    if not cons.empty:
        cons["kickoff"] = pd.to_datetime(cons["kickoff"], utc=True)
    raw["consensus"] = cons
    return raw


def build_consensus(pos, top_wallets, week, tokens, now) -> pd.DataFrame:
    wk = pos[(pos["wallet"].isin(top_wallets)) & (pos["week"] == week)]
    wk = wk[(wk["end_shares"] > 1e-9) & (wk["end_cost"] >= MIN_POSITION_DOLLARS)]
    rows = []
    for asset, grp in wk.groupby("asset"):
        info = tokens[asset]
        payout = info["payout"]
        if payout is not None:
            status = "WON" if payout == 1 else "LOST" if payout == 0 else "push"
        elif info["game_time"] <= now:
            status = "in progress"
        else:
            status = "open"
        rows.append({
            "game": info["event_title"],
            "event_id": info["event_id"],
            "condition_id": info["condition_id"],
            "market": info["question"],
            "pick": info["outcome"],
            "market_type": info["market_type"],
            "kickoff": info["game_time"],
            "kickoff_et": info["game_time"].astimezone(ET).strftime("%a %m/%d %I:%M %p"),
            "wallets": grp["wallet"].nunique(),
            "dollars": round(grp["end_cost"].sum(), 2),
            "avg_entry_price": round(grp["end_cost"].sum() / grp["end_shares"].sum(), 3),
            "current_price": info.get("current_price"),
            "status": status,
            "wallet_list": ", ".join(sorted(grp["wallet"])),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 5. Recommendations
# ----------------------------------------------------------------------------
MIN_NET_WALLETS = 2             # a pick needs at least this many more backers than fades


def recommend(cons: pd.DataFrame, n: int = 3,
              min_net_wallets: int = MIN_NET_WALLETS) -> pd.DataFrame:
    """
    Pick up to n bets from the consensus table:
    1. Only games that haven't kicked off.
    2. Net out disagreement: top wallets on the other side of the same market
       are subtracted (net_wallets, net_dollars).
    3. Require net_wallets >= min_net_wallets.
    4. Rank by net_wallets, then net_dollars; at most one pick per game.
    """
    if cons.empty:
        return cons
    df = cons.copy()
    per_market = df.groupby("condition_id")[["wallets", "dollars"]].transform("sum")
    df["fade_wallets"] = per_market["wallets"] - df["wallets"]
    df["fade_dollars"] = per_market["dollars"] - df["dollars"]
    df["net_wallets"] = df["wallets"] - df["fade_wallets"]
    df["net_dollars"] = df["dollars"] - df["fade_dollars"]
    not_started = pd.to_datetime(df["kickoff"], utc=True) > pd.Timestamp.now(tz="UTC")
    df = df[(df["status"] == "open") & not_started & (df["net_wallets"] >= min_net_wallets)]
    df = df.sort_values(["net_wallets", "net_dollars"], ascending=False)
    return df.drop_duplicates(subset="event_id").head(n).reset_index(drop=True)


# ----------------------------------------------------------------------------
# Command-line entry point
# ----------------------------------------------------------------------------
def main():
    res = run_analysis()
    top, cons, wk = res["top"], res["consensus"], res["current_week"]
    pd.set_option("display.width", 200, "display.max_colwidth", 60)
    print("\n=== TOP WALLETS (realized NFL profit, season) ===")
    cols = [c for c in ["name", "pseudonym", "realized_pnl", "season_buy_volume",
                        "roi_pct", "markets_traded"] if c in top.columns]
    print(top[cols].round(2).to_string())
    if cons.empty:
        print("\nTop wallets hold no positions this week yet.")
        return
    show = ["game", "pick", "market_type", "wallets", "dollars", "avg_entry_price", "status"]
    print(f"\n=== WEEK {wk}: MOST COMMON (by number of wallets) ===")
    print(cons.sort_values(["wallets", "dollars"], ascending=False)[show].head(25)
          .to_string(index=False))
    print(f"\n=== WEEK {wk}: BIGGEST (by dollars) ===")
    print(cons.sort_values(["dollars", "wallets"], ascending=False)[show].head(25)
          .to_string(index=False))
    recs = recommend(cons)
    print("\n=== RECOMMENDED BETS ===")
    if recs.empty:
        print("No pick has enough agreement among the top wallets.")
    for i, r in recs.iterrows():
        print(f"{i + 1}. {r['pick']} ({r['market']}) - {r['game']}, {r['kickoff_et']}: "
              f"{r['wallets']} of top wallets, net {r['net_wallets']}, ${r['dollars']:,.0f}")
    print(f"\nCSV files written to {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()

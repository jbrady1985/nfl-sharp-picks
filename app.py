"""
NFL Sharp Picks - Streamlit front end.

Hosted: reads results/latest.json, which the scheduled GitHub Action refreshes.
Local:  python nfl_sharps.py   (writes results/latest.json)
        streamlit run app.py
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import nfl_sharps as ns

st.set_page_config(page_title="NFL Sharp Picks", page_icon="🏈", layout="wide")

st.markdown("""
<style>
.pick-rank {font-size: 0.9rem; letter-spacing: .08em; text-transform: uppercase; opacity: .65;}
.pick-name {font-size: 1.7rem; font-weight: 700; line-height: 1.2; margin: .2rem 0;}
.pick-market {font-size: 1rem; opacity: .8; margin-bottom: .6rem;}
.pick-stat {font-size: 1rem; margin: .15rem 0;}
.pick-good {color: #1a9e55; font-weight: 600;}
.pick-warn {color: #d08a00; font-weight: 600;}
</style>
""", unsafe_allow_html=True)


@st.cache_data
def _load(mtime: float):
    return ns.load_results()


def refresh_url() -> str | None:
    try:
        return st.secrets.get("REFRESH_URL")
    except Exception:          # no secrets configured
        return None


res = _load(ns.RESULTS_FILE.stat().st_mtime) if ns.RESULTS_FILE.exists() else None

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    min_net = st.number_input("Minimum net agreement (wallets)", 1, 10,
                              ns.MIN_NET_WALLETS, step=1,
                              help="Backers minus top wallets on the other side "
                                   "of the same market.")
    if res:
        st.caption(f"Data built with: top {res.get('top_n', ns.TOP_N)} wallets, "
                   f"${res.get('min_volume', ns.MIN_VOLUME):,.0f} minimum volume. "
                   "Change these in nfl_sharps.py.")
    url = refresh_url()
    if url:
        st.link_button("Refresh data now", url, width="stretch")
        st.caption("Opens GitHub. Click **Run workflow**; new data appears "
                   "here in about 10 minutes.")

# ---------------------------------------------------------------- header
st.title("🏈 NFL Sharp Picks")
st.write("Follows the most profitable Polymarket NFL wallets that have bet every "
         "week this season, and recommends where they agree for this week's games.")

if not res:
    st.info("No results yet. The first scheduled refresh will create them, or "
            "start one from the GitHub **Actions** tab.")
    st.stop()

top, cons, week = res["top"], res["consensus"], res["current_week"]
recs = ns.recommend(cons, n=3, min_net_wallets=int(min_net))

# ---------------------------------------------------------------- summary
age_hours = (datetime.now(timezone.utc) - res["run_time"]).total_seconds() / 3600
if age_hours > 24:
    st.warning(f"Data is {age_hours / 24:.1f} days old. Prices and positions "
               "may have changed.")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Current week", week)
c2.metric("Wallets in NFL markets", f"{res['n_wallets']:,}")
c3.metric(f"Bet every week 1–{week - 1}", f"{res['n_consistent']:,}")
c4.metric("Followed wallets", len(top))
st.caption(f"Data as of {res['run_time'].astimezone(ns.ET):%a %b %d, %I:%M %p} ET · "
           f"{res['n_trades']:,} trades across {res['n_games']} game events")
# ---------------------------------------------------------------- picks
st.subheader(f"Recommended bets · Week {week}")
if recs.empty:
    st.warning("No upcoming pick has enough agreement among the followed wallets. "
               "Try lowering the minimum net agreement in the sidebar.")
else:
    if len(recs) < 3:
        st.caption(f"Only {len(recs)} pick(s) meet the agreement threshold.")
    cols = st.columns(3)
    for i, r in recs.iterrows():
        entry, now_px = r["avg_entry_price"], r["current_price"]
        if now_px is None or pd.isna(now_px):
            price_line = f"Their avg entry: <b>{entry:.0%}</b>"
        else:
            move = now_px - entry
            cls = "pick-warn" if move > 0.05 else "pick-good"
            note = "price has moved up since they bet" if move > 0.05 else "near their entry"
            price_line = (f"Entry {entry:.0%} → now <span class='{cls}'>{now_px:.0%}</span>"
                          f" ({note})")
        fade = (f" · {int(r['fade_wallets'])} on the other side"
                if r["fade_wallets"] else " · none on the other side")
        with cols[i].container(border=True):
            st.markdown(f"""
<div class="pick-rank">Pick {i + 1}</div>
<div class="pick-name">{r['pick']}</div>
<div class="pick-market">{r['market']}<br>{r['game']} · {r['kickoff_et']} ET</div>
<div class="pick-stat"><b>{int(r['wallets'])} of {len(top)}</b> followed wallets{fade}</div>
<div class="pick-stat"><b>${r['dollars']:,.0f}</b> backing it</div>
<div class="pick-stat">{price_line}</div>
""", unsafe_allow_html=True)

st.caption("Based on a few weeks of results from a handful of wallets; past profit "
           "can be luck. Treat these as signals, not certainties.")

# ---------------------------------------------------------------- detail tabs
tab1, tab2, tab3 = st.tabs(["All Week positions", "Followed wallets", "Download"])

with tab1:
    if cons.empty:
        st.write("The followed wallets hold no positions this week yet.")
    else:
        sort_by = st.radio("Sort by", ["Number of wallets", "Dollars"], horizontal=True)
        order = ["wallets", "dollars"] if sort_by == "Number of wallets" else ["dollars", "wallets"]
        st.dataframe(
            cons.sort_values(order, ascending=False)[
                ["game", "market", "pick", "market_type", "kickoff_et", "wallets",
                 "dollars", "avg_entry_price", "current_price", "status"]],
            hide_index=True, width="stretch",
            column_config={
                "dollars": st.column_config.NumberColumn("dollars", format="$%.0f"),
                "avg_entry_price": st.column_config.NumberColumn("avg entry", format="%.3f"),
                "current_price": st.column_config.NumberColumn("current", format="%.3f"),
            })

with tab2:
    show = [c for c in ["name", "pseudonym", "realized_pnl", "season_buy_volume",
                        "roi_pct", "markets_traded", "had_unmatched_sells",
                        "profile_url"] if c in top.columns]
    st.dataframe(
        top[show], width="stretch",
        column_config={
            "realized_pnl": st.column_config.NumberColumn("realized P&L", format="$%.0f"),
            "season_buy_volume": st.column_config.NumberColumn("volume", format="$%.0f"),
            "roi_pct": st.column_config.NumberColumn("ROI", format="%.1f%%"),
            "profile_url": st.column_config.LinkColumn("profile", display_text="open"),
        })

with tab3:
    st.download_button("Week positions (CSV)", cons.to_csv(index=False),
                       f"week{week}_positions.csv", "text/csv")
    st.download_button("Followed wallets (CSV)", top.to_csv(index_label="wallet"),
                       "top_wallets.csv", "text/csv")
    if not recs.empty:
        st.download_button("Recommended bets (CSV)", recs.to_csv(index=False),
                           f"week{week}_picks.csv", "text/csv")

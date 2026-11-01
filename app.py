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
.st-key-run_live button {font-size: 1.6rem; font-weight: 700; padding: 1.1rem 0;
                         min-height: 4.5rem; border-radius: 12px;}
.st-key-run_live button p {font-size: 1.6rem; font-weight: 700;}
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

# ---------------------------------------------------------------- live refresh
if st.button("🔄  GET LATEST PICKS", type="primary", width="stretch",
             key="run_live"):
    with st.status("Getting the latest bets from Polymarket (about 1–2 minutes)...",
                   expanded=True) as status:
        try:
            st.session_state["live"] = ns.refresh_current_week(res, log=st.write)
            status.update(label="Picks updated.", state="complete", expanded=False)
        except Exception as e:
            status.update(label="Couldn't reach Polymarket. Showing the last "
                                "saved picks instead.", state="error")
            st.write(f"Details: {e}")

live = st.session_state.get("live")
if live and live["run_time"] == res["run_time"]:
    res = live
else:
    live = None

top, cons, week = res["top"], res["consensus"], res["current_week"]
recs = ns.recommend(cons, n=3, min_net_wallets=int(min_net))

# ---------------------------------------------------------------- summary
as_of = live["live_time"] if live else res["run_time"]
age_hours = (datetime.now(timezone.utc) - as_of).total_seconds() / 3600
if age_hours > 24:
    st.warning(f"Data is {age_hours / 24:.1f} days old. Prices and positions "
               "may have changed.")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Current week", week)
c2.metric("Wallets in NFL markets", f"{res['n_wallets']:,}")
c3.metric(f"Bet every week 1–{week - 1}", f"{res['n_consistent']:,}")
c4.metric("Followed wallets", len(top))
if live:
    st.caption(f"Picks live as of {as_of.astimezone(ns.ET):%a %b %d, %I:%M %p} ET · "
               f"wallet rankings from {res['run_time'].astimezone(ns.ET):%a %b %d, %I:%M %p} ET")
else:
    st.caption(f"Data as of {as_of.astimezone(ns.ET):%a %b %d, %I:%M %p} ET · "
               f"{res['n_trades']:,} trades across {res['n_games']} game events · "
               "press **Get latest picks** for up-to-the-minute bets")
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

def wallet_label(wallet, row):
    name = str(row.get("name") or "")
    if not name or name.lower().startswith("0x"):
        name = str(row.get("pseudonym") or "") or f"{wallet[:6]}…{wallet[-4:]}"
    return name if len(name) <= 22 else name[:21] + "…"


with tab2:
    st.markdown("**Click a wallet to see its picks for this week.**")
    wallets = list(top.index)
    if st.session_state.get("pick_wallet") not in wallets:
        st.session_state["pick_wallet"] = None
    per_row = 5
    for start in range(0, len(wallets), per_row):
        cols = st.columns(per_row)
        for col, w in zip(cols, wallets[start:start + per_row]):
            row = top.loc[w]
            rank = wallets.index(w) + 1
            selected = st.session_state["pick_wallet"] == w
            label = f"{rank}. {wallet_label(w, row)}\n\n${row['realized_pnl']:,.0f}"
            if col.button(label, key=f"wallet_{w}", width="stretch",
                          type="primary" if selected else "secondary"):
                st.session_state["pick_wallet"] = None if selected else w
                st.rerun()

    chosen = st.session_state.get("pick_wallet")
    if chosen:
        row = top.loc[chosen]
        st.subheader(f"Week {week} picks · {wallet_label(chosen, row)}")
        st.caption(f"Season realized P&L ${row['realized_pnl']:,.0f} · ROI {row['roi_pct']:.1f}% · "
                   f"[Polymarket profile](https://polymarket.com/profile/{chosen})")
        wp = ns.wallet_picks(cons, chosen)
        if wp.empty:
            st.info("This wallet holds no positions in this week's games yet.")
        else:
            has_own = wp["their_dollars"].notna().any()
            if has_own:
                st.metric("Their money on this week's games", f"${wp['their_dollars'].sum():,.0f}",
                          help=f"Across {len(wp)} positions")
            else:
                st.caption("Per-wallet amounts appear after the next data refresh "
                           "(or press **Get latest picks** above).")
            cols = ["game", "market", "pick", "kickoff_et", "status", "their_dollars", "their_entry",
                    "current_price", "with_them", "against_them"]
            if not has_own:
                cols = [c for c in cols if c not in ("their_dollars", "their_entry")]
            st.dataframe(
                wp[cols], hide_index=True, width="stretch",
                column_config={
                    "kickoff_et": "kickoff (ET)",
                    "their_dollars": st.column_config.NumberColumn("their $", format="$%.0f"),
                    "their_entry": st.column_config.NumberColumn("their entry", format="%.3f"),
                    "current_price": st.column_config.NumberColumn("current", format="%.3f"),
                    "with_them": st.column_config.NumberColumn(
                        "others with them", help="Other followed wallets holding the same pick"),
                    "against_them": st.column_config.NumberColumn(
                        "others against", help="Followed wallets on the other side of the same market"),
                })

    st.markdown("---")
    st.caption("All followed wallets")
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

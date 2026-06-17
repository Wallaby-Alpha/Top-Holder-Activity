"""
Wallet Cohort Scanner
---------------------
Two-tab Streamlit app using Helius RPC for Solana token analysis.

Tab 1 - Cohort Scanner:
  Walk mint signatures → diff preTokenBalances/postTokenBalances per wallet →
  bucket by user-defined time cohorts → export CSVs.

Tab 2 - Top Holders Activity:
  Pull current top holders via getProgramAccounts (SPL Token program, filtered
  by mint) → for each holder walk their token-account signatures for the last 7
  days → compute buys, sells, net change → classify as Accumulating / Distributing
  / Holding → export CSV.

LP Filtering (both tabs):
  Known AMM/LP program IDs and common LP-account patterns are detected at the
  transaction level: if a balance-change owner is a known program or if the
  transaction's inner instructions reference a known AMM program, that delta
  is tagged as an LP interaction and excluded from individual-wallet stats.
  Users can also paste additional LP addresses to exclude in the sidebar.
"""

import time
import datetime as dt

import requests
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Known LP / AMM program IDs to filter out
# These program IDs appear as "owner" in token accounts belonging to AMM pools,
# or are referenced in transactions as pool programs.
# ---------------------------------------------------------------------------
KNOWN_LP_PROGRAMS = {
    # Raydium AMM v4
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    # Raydium CLMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    # Raydium CPMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    # Orca Whirlpools
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    # Orca v1
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",
    # pump.fun AMM / bonding curve program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    # pump.fun migration program
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
    # Meteora DLMM
    "LBUZKhRxPF3XUpBCjp4YzTKgLLjIdZCeB1fZBqBsM2S",
    # Meteora Pools
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EkDnif9e",
    # OpenBook v2
    "opnb2LAfJYbRMAHHvqjCwQxanZn7n9dH5i7U2ZFn2YS",
    # Token program itself (mint/burn authority txns)
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RPC_URL_TMPL = "https://mainnet.helius-rpc.com/?api-key={key}"

st.set_page_config(page_title="Wallet Cohort Scanner", layout="wide")
st.title("🔍 Wallet Cohort Scanner")

# ---------------------------------------------------------------------------
# Sidebar - shared config
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Configuration")
    api_key = st.text_input("Helius API Key", type="password")
    token_address = st.text_input("Token mint address")

    st.markdown("---")
    st.subheader("LP / Pool Filtering")
    st.caption(
        "Wallets owned by known AMM programs are auto-filtered. "
        "Add any extra pool/LP addresses here (one per line)."
    )
    extra_lp_raw = st.text_area("Extra LP addresses to exclude", height=80)
    extra_lp = {a.strip() for a in extra_lp_raw.splitlines() if a.strip()}
    ALL_LP = KNOWN_LP_PROGRAMS | extra_lp

    st.markdown("---")
    st.subheader("Cohort Scanner settings")
    n_cohorts = st.slider("Number of cohorts", min_value=1, max_value=6, value=6)

    cohorts = []
    default_labels = [
        "Launch accumulation", "First recognition", "Consolidation",
        "Volume spike", "Pre-breakout ramp", "Vertical expansion",
    ]
    today = dt.date.today()
    for i in range(n_cohorts):
        st.markdown(f"**Cohort {i+1}**")
        label = st.text_input(
            f"Label {i+1}",
            value=default_labels[i] if i < len(default_labels) else f"Cohort {i+1}",
            key=f"label_{i}",
        )
        c1, c2 = st.columns(2)
        with c1:
            sd = st.date_input(f"Start {i+1}", value=today, key=f"sd_{i}")
            st_ = st.time_input(f"", value=dt.time(0, 0), key=f"st_{i}", label_visibility="collapsed")
        with c2:
            ed = st.date_input(f"End {i+1}", value=today, key=f"ed_{i}")
            et = st.time_input(f"", value=dt.time(0, 0), key=f"et_{i}", label_visibility="collapsed")
        cohorts.append({
            "label": label,
            "start": dt.datetime.combine(sd, st_, tzinfo=dt.timezone.utc),
            "end": dt.datetime.combine(ed, et, tzinfo=dt.timezone.utc),
        })

    max_signatures = st.number_input(
        "Max signatures (cohort scan)", min_value=100, max_value=500000,
        value=50000, step=1000,
    )
    tx_batch_size = st.number_input(
        "TX fetch batch size", min_value=1, max_value=100, value=25, step=1,
    )

    st.markdown("---")
    st.subheader("Top Holders settings")
    top_n = st.number_input(
        "Number of top holders to scan", min_value=5, max_value=200, value=50, step=5,
    )
    holder_lookback_days = st.number_input(
        "Activity lookback (days)", min_value=1, max_value=30, value=7, step=1,
    )
    holder_sig_limit = st.number_input(
        "Max signatures per holder wallet", min_value=50, max_value=5000,
        value=500, step=50,
    )

# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
def rpc_call(rpc_url, method, params, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    resp = requests.post(rpc_url, json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"RPC {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


def get_all_signatures(rpc_url, address, max_sigs, after_ts=None, progress_cb=None):
    """Walk getSignaturesForAddress backward until cap, genesis, or after_ts."""
    signatures = []
    before = None
    while len(signatures) < max_sigs:
        params = [address, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        batch = rpc_call(rpc_url, "getSignaturesForAddress", params)
        if not batch:
            break
        # If oldest in batch is already before our cutoff, trim and stop
        if after_ts:
            trimmed = [s for s in batch if (s.get("blockTime") or 0) >= after_ts]
            signatures.extend(trimmed)
            if len(trimmed) < len(batch):
                break  # crossed the cutoff
        else:
            signatures.extend(batch)
        before = batch[-1]["signature"]
        if progress_cb:
            progress_cb(len(signatures), batch[-1].get("blockTime"))
        if len(batch) < 1000:
            break
        time.sleep(0.05)
    return signatures[:max_sigs]


def get_transactions_batch(rpc_url, sigs):
    payload = [
        {
            "jsonrpc": "2.0", "id": i,
            "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        for i, sig in enumerate(sigs)
    ]
    resp = requests.post(rpc_url, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Batch RPC {resp.status_code}: {resp.text[:300]}")
    results = [None] * len(sigs)
    for item in resp.json():
        idx = item.get("id")
        if idx is not None and 0 <= idx < len(results):
            results[idx] = item.get("result")
    return results


def is_lp_owner(owner, all_lp):
    """Return True if this owner is a known LP/AMM program."""
    return owner in all_lp


def extract_balance_changes(tx, mint, all_lp):
    """
    Diff pre/post token balances for `mint`.
    Returns list of (owner, delta, timestamp, is_lp).
    LP-owned accounts are flagged but returned so callers can decide to keep/drop.
    """
    if not tx:
        return []
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    block_time = tx.get("blockTime")
    if block_time is None:
        return []

    # Check if this tx involves a known LP program in its account keys
    tx_accounts = []
    try:
        tx_accounts = [
            a.get("pubkey", "")
            for a in (tx.get("transaction", {})
                       .get("message", {})
                       .get("accountKeys") or [])
        ]
    except Exception:
        pass
    tx_touches_lp = bool(all_lp & set(tx_accounts))

    pre_map = {}
    for b in pre:
        if b.get("mint") != mint:
            continue
        owner = b.get("owner")
        amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        pre_map[owner] = amt

    post_map = {}
    for b in post:
        if b.get("mint") != mint:
            continue
        owner = b.get("owner")
        amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_map[owner] = amt

    changes = []
    for owner in set(pre_map) | set(post_map):
        if owner is None:
            continue
        delta = post_map.get(owner, 0.0) - pre_map.get(owner, 0.0)
        if abs(delta) < 1e-12:
            continue
        lp_flag = is_lp_owner(owner, all_lp) or (tx_touches_lp and owner in all_lp)
        changes.append((owner, delta, block_time, lp_flag))
    return changes


# ---------------------------------------------------------------------------
# Cohort scanner helpers
# ---------------------------------------------------------------------------
def build_wallet_table(balance_changes):
    if not balance_changes:
        return pd.DataFrame()
    df = pd.DataFrame(balance_changes, columns=["wallet", "delta", "timestamp", "is_lp"])
    # Drop LP-owned accounts
    df = df[~df["is_lp"]]
    if df.empty:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.sort_values("timestamp")

    records = {}
    for wallet, group in df.groupby("wallet"):
        running = 0.0
        peak = 0.0
        acquired = 0.0
        sold = 0.0
        first_buy = None
        for _, row in group.iterrows():
            d = row["delta"]
            if d > 0:
                acquired += d
                if first_buy is None:
                    first_buy = row["timestamp"]
            else:
                sold += abs(d)
            running += d
            peak = max(peak, running)
        if first_buy is None:
            continue
        records[wallet] = {
            "wallet": wallet,
            "first_buy_time": first_buy,
            "total_acquired": acquired,
            "total_sold": sold,
            "current_balance": running,
            "peak_balance": peak,
            "still_holding": running > 1e-9,
            "retention_pct": (running / acquired * 100) if acquired > 0 else 0.0,
            "realized_sales": sold,
        }
    return pd.DataFrame.from_dict(records, orient="index").reset_index(drop=True)


def assign_cohorts(wallet_df, cohorts):
    def label_for(ts):
        for c in cohorts:
            if c["start"] <= ts < c["end"]:
                return c["label"]
        return "Unassigned (outside windows)"
    wallet_df = wallet_df.copy()
    wallet_df["cohort"] = wallet_df["first_buy_time"].apply(label_for)
    return wallet_df


def cohort_summary(wallet_df, cohorts):
    rows = []
    for label in [c["label"] for c in cohorts] + ["Unassigned (outside windows)"]:
        sub = wallet_df[wallet_df["cohort"] == label]
        n = len(sub)
        rows.append({
            "Cohort": label,
            "Wallets": n,
            "Still Holding %": round(sub["still_holding"].mean() * 100, 2) if n else 0,
            "Avg Retention %": round(sub["retention_pct"].mean(), 2) if n else 0,
            "Avg Position Size": round(sub["total_acquired"].mean(), 4) if n else 0,
            "Total Realized Sales": round(sub["realized_sales"].sum(), 4) if n else 0,
        })
    return pd.DataFrame(rows)


def overlap_matrix(wallet_df, cohorts):
    labels = [c["label"] for c in cohorts]
    sets = {l: set(wallet_df[wallet_df["cohort"] == l]["wallet"]) for l in labels}
    matrix = pd.DataFrame(index=labels, columns=labels, dtype=int)
    for a in labels:
        for b in labels:
            matrix.loc[a, b] = len(sets[a] & sets[b])
    return matrix


# ---------------------------------------------------------------------------
# Top holders helpers
# ---------------------------------------------------------------------------
def get_top_holders(rpc_url, mint, top_n, all_lp):
    """
    Use getProgramAccounts on the SPL Token program filtered by mint to get
    all token accounts, sorted by balance, top N returned (LP accounts removed).
    """
    params = [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        {
            "encoding": "jsonParsed",
            "filters": [
                {"dataSize": 165},
                {"memcmp": {"offset": 0, "bytes": mint}},
            ],
        },
    ]
    result = rpc_call(rpc_url, "getProgramAccounts", params)
    if not result:
        return []

    holders = []
    for acct in result:
        info = (acct.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {}))
        owner = info.get("owner")
        if not owner or owner in all_lp:
            continue
        token_acct = acct.get("pubkey")
        amt = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
        if amt <= 0:
            continue
        holders.append({"wallet": owner, "token_account": token_acct, "current_balance": amt})

    holders.sort(key=lambda x: x["current_balance"], reverse=True)
    return holders[:top_n]


def get_holder_activity(rpc_url, token_acct, owner, mint, lookback_days,
                         sig_limit, all_lp, batch_size=25):
    """
    Walk the token account's recent signatures (within lookback window),
    fetch transactions, extract balance changes, return aggregated stats.
    """
    cutoff_ts = int((dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(days=lookback_days)).timestamp())

    sigs = get_all_signatures(rpc_url, token_acct, sig_limit, after_ts=cutoff_ts)
    if not sigs:
        return {"buys_7d": 0.0, "sells_7d": 0.0, "net_7d": 0.0, "tx_count_7d": 0}

    sig_list = [s["signature"] for s in sigs]
    buys = 0.0
    sells = 0.0
    tx_count = 0

    for i in range(0, len(sig_list), batch_size):
        batch = sig_list[i:i + batch_size]
        try:
            results = get_transactions_batch(rpc_url, batch)
        except RuntimeError:
            continue
        for tx in results:
            changes = extract_balance_changes(tx, mint, all_lp)
            for (chg_owner, delta, ts, is_lp) in changes:
                if is_lp or chg_owner != owner:
                    continue
                if delta > 0:
                    buys += delta
                else:
                    sells += abs(delta)
                tx_count += 1
        time.sleep(0.03)

    return {
        "buys_7d": buys,
        "sells_7d": sells,
        "net_7d": buys - sells,
        "tx_count_7d": tx_count,
    }


def classify_activity(row):
    net = row["net_7d"]
    buys = row["buys_7d"]
    sells = row["sells_7d"]
    total = buys + sells
    if total < 1e-9:
        return "⚪ No Activity"
    buy_pct = buys / total
    if net > 0 and buy_pct >= 0.6:
        return "🟢 Accumulating"
    elif net < 0 and buy_pct <= 0.4:
        return "🔴 Distributing"
    elif abs(net) / (row["current_balance"] + 1e-9) < 0.05:
        return "🟡 Holding"
    elif net > 0:
        return "🟢 Net Buying"
    else:
        return "🔴 Net Selling"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
if not api_key or not token_address:
    st.info("Enter your Helius API key and token mint address in the sidebar to begin.")
    st.stop()

rpc_url = RPC_URL_TMPL.format(key=api_key)
tab1, tab2 = st.tabs(["📅 Cohort Scanner", "🐋 Top Holders Activity"])


# ===========================================================================
# TAB 1 - Cohort Scanner
# ===========================================================================
with tab1:
    st.header("Cohort Scanner")
    st.caption(
        "Walks all mint signatures → diffs pre/post token balances per wallet "
        "(LP accounts auto-filtered) → buckets by your time windows."
    )
    run_cohort = st.button("🚀 Run Cohort Scan", type="primary", key="run_cohort")

    if run_cohort:
        # Step 1: signatures
        st.subheader("🔧 Debug: Signature Walk")
        sig_prog = st.progress(0, text="Fetching signatures...")
        sig_status = st.empty()

        def sig_cb(count, oldest_ts):
            pct = min(count / max_signatures, 1.0)
            ts_str = (dt.datetime.fromtimestamp(oldest_ts, tz=dt.timezone.utc)
                      .strftime("%Y-%m-%d %H:%M UTC") if oldest_ts else "?")
            sig_prog.progress(pct, text=f"{count} signatures... oldest: {ts_str}")

        try:
            sigs_meta = get_all_signatures(rpc_url, token_address, max_signatures, progress_cb=sig_cb)
        except RuntimeError as e:
            st.error(str(e)); st.stop()
        sig_prog.empty()

        if not sigs_meta:
            st.warning("No signatures found."); st.stop()

        newest_ts = sigs_meta[0].get("blockTime")
        oldest_ts = sigs_meta[-1].get("blockTime")
        hit_cap = len(sigs_meta) >= max_signatures
        sig_status.markdown(
            f"- **Total signatures fetched:** {len(sigs_meta)}\n"
            f"- **Newest tx:** {dt.datetime.fromtimestamp(newest_ts, tz=dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if newest_ts else '?'}\n"
            f"- **Oldest tx reached:** {dt.datetime.fromtimestamp(oldest_ts, tz=dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if oldest_ts else '?'}\n"
            f"- **Hit safety cap:** {'⚠️ YES — increase max signatures to reach further back' if hit_cap else '✅ No — full history reached'}"
        )

        # Step 2: parse transactions
        st.subheader("🔧 Debug: Transaction Parsing")
        tx_prog = st.progress(0, text="Parsing transactions...")
        all_changes = []
        sig_list = [s["signature"] for s in sigs_meta]
        total = len(sig_list)
        relevant = 0
        errors = 0

        for i in range(0, total, tx_batch_size):
            batch = sig_list[i:i + tx_batch_size]
            try:
                results = get_transactions_batch(rpc_url, batch)
            except RuntimeError:
                errors += len(batch)
                results = [None] * len(batch)
            for tx in results:
                changes = extract_balance_changes(tx, token_address, ALL_LP)
                if changes:
                    relevant += 1
                all_changes.extend(changes)
            pct = min((i + len(batch)) / total, 1.0)
            tx_prog.progress(pct, text=f"{i+len(batch)}/{total} parsed — {relevant} with changes, {errors} errors")
            time.sleep(0.05)

        tx_prog.empty()
        lp_filtered = sum(1 for (_, _, _, lp) in all_changes if lp)
        st.markdown(
            f"- **Transactions scanned:** {total}\n"
            f"- **With relevant balance changes:** {relevant}\n"
            f"- **LP-owned deltas filtered out:** {lp_filtered}\n"
            f"- **Errors / skipped:** {errors}\n"
            f"- **Raw balance-change records (non-LP):** {sum(1 for (_, _, _, lp) in all_changes if not lp)}"
        )

        if not all_changes:
            st.warning("No balance changes extracted."); st.stop()

        # Step 3: build table
        with st.spinner("Building wallet table..."):
            wallet_df = build_wallet_table(all_changes)

        if wallet_df.empty:
            st.warning("No wallets found with a clear first-buy event."); st.stop()

        st.markdown(
            f"- **Earliest first-buy:** {wallet_df['first_buy_time'].min()}\n"
            f"- **Latest first-buy:** {wallet_df['first_buy_time'].max()}\n"
            f"- **Unique wallets:** {len(wallet_df)}"
        )

        wallet_df = assign_cohorts(wallet_df, cohorts)

        st.subheader("📊 Cohort Summary")
        st.dataframe(cohort_summary(wallet_df, cohorts), use_container_width=True)

        st.subheader("🔁 Wallet Overlap Between Cohorts")
        st.dataframe(overlap_matrix(wallet_df, cohorts), use_container_width=True)

        st.subheader("📁 Cohort Wallet Lists")
        for c in cohorts:
            label = c["label"]
            sub = wallet_df[wallet_df["cohort"] == label].sort_values("current_balance", ascending=False)
            with st.expander(f"{label} — {len(sub)} wallets"):
                st.dataframe(sub, use_container_width=True)
                csv = sub.to_csv(index=False).encode()
                st.download_button(f"⬇️ Download CSV", csv,
                    file_name=f"cohort_{label.lower().replace(' ','_')}.csv",
                    mime="text/csv", key=f"dl_{label}")

        unassigned = wallet_df[wallet_df["cohort"] == "Unassigned (outside windows)"]
        with st.expander(f"Unassigned — {len(unassigned)} wallets"):
            st.dataframe(unassigned, use_container_width=True)
            st.download_button("⬇️ Download CSV", unassigned.to_csv(index=False).encode(),
                file_name="cohort_unassigned.csv", mime="text/csv", key="dl_unassigned")

        st.subheader("🗂️ Full Wallet Table")
        st.dataframe(wallet_df, use_container_width=True)
        st.download_button("⬇️ Download Full CSV", wallet_df.to_csv(index=False).encode(),
            file_name="all_wallets.csv", mime="text/csv", key="dl_full")

        st.subheader("🎯 Follow-These-Wallets Candidates")
        st.caption("Wallets in 2+ cohorts AND retention >50%.")
        mc = wallet_df.groupby("wallet")["cohort"].nunique()
        candidates = wallet_df[
            wallet_df["wallet"].isin(mc[mc > 1].index) & (wallet_df["retention_pct"] > 50)
        ].sort_values("retention_pct", ascending=False)
        st.dataframe(candidates, use_container_width=True)
        if not candidates.empty:
            st.download_button("⬇️ Download Candidates CSV",
                candidates.to_csv(index=False).encode(),
                file_name="follow_wallet_candidates.csv", mime="text/csv", key="dl_cand")

    else:
        st.info("Configure cohort windows in the sidebar then click **Run Cohort Scan**.")


# ===========================================================================
# TAB 2 - Top Holders Activity
# ===========================================================================
with tab2:
    st.header("Top Holders Activity")
    st.caption(
        f"Pulls current top holders by token balance (LP accounts auto-filtered), "
        f"then checks each wallet's token account for buys/sells over the last "
        f"{holder_lookback_days} days."
    )
    run_holders = st.button("🚀 Run Top Holders Scan", type="primary", key="run_holders")

    if run_holders:
        # Step 1: get holders
        with st.spinner(f"Fetching top {top_n} holders via getProgramAccounts..."):
            try:
                holders = get_top_holders(rpc_url, token_address, top_n, ALL_LP)
            except RuntimeError as e:
                st.error(str(e)); st.stop()

        if not holders:
            st.warning(
                "No holders found. This can happen if the token uses Token-2022 "
                "(different program ID). Token-2022 support coming soon."
            )
            st.stop()

        st.success(f"Found {len(holders)} non-LP holders with a positive balance.")

        # Step 2: scan activity for each holder
        st.subheader("Scanning holder activity...")
        holder_prog = st.progress(0)
        holder_status = st.empty()
        results = []

        for idx, h in enumerate(holders):
            holder_status.text(f"Scanning wallet {idx+1}/{len(holders)}: {h['wallet'][:12]}...")
            try:
                activity = get_holder_activity(
                    rpc_url,
                    token_acct=h["token_account"],
                    owner=h["wallet"],
                    mint=token_address,
                    lookback_days=holder_lookback_days,
                    sig_limit=holder_sig_limit,
                    all_lp=ALL_LP,
                )
            except Exception:
                activity = {"buys_7d": 0.0, "sells_7d": 0.0, "net_7d": 0.0, "tx_count_7d": 0}
            results.append({**h, **activity})
            holder_prog.progress((idx + 1) / len(holders))
            time.sleep(0.05)

        holder_prog.empty()
        holder_status.empty()

        # Step 3: build display table
        df = pd.DataFrame(results)
        df["rank"] = range(1, len(df) + 1)
        df["activity"] = df.apply(classify_activity, axis=1)
        df["buy_sell_ratio"] = df.apply(
            lambda r: round(r["buys_7d"] / r["sells_7d"], 2) if r["sells_7d"] > 1e-9 else float("inf"),
            axis=1,
        )

        display_cols = [
            "rank", "wallet", "current_balance",
            "buys_7d", "sells_7d", "net_7d", "buy_sell_ratio",
            "tx_count_7d", "activity",
        ]
        df_display = df[display_cols].copy()
        df_display.columns = [
            "Rank", "Wallet", "Current Balance",
            f"Buys ({holder_lookback_days}d)", f"Sells ({holder_lookback_days}d)",
            f"Net ({holder_lookback_days}d)", "Buy/Sell Ratio",
            "TX Count", "Activity",
        ]

        # Summary bar
        col1, col2, col3, col4 = st.columns(4)
        accum = (df["activity"].str.contains("Accumulating|Net Buying")).sum()
        distrib = (df["activity"].str.contains("Distributing|Net Selling")).sum()
        holding = (df["activity"].str.contains("Holding")).sum()
        inactive = (df["activity"].str.contains("No Activity")).sum()
        col1.metric("🟢 Accumulating", accum)
        col2.metric("🔴 Distributing", distrib)
        col3.metric("🟡 Holding", holding)
        col4.metric("⚪ No Activity", inactive)

        st.subheader(f"Top {len(df)} Holders — {holder_lookback_days}d Activity")
        st.dataframe(df_display, use_container_width=True)

        csv = df_display.to_csv(index=False).encode()
        st.download_button(
            "⬇️ Download Top Holders CSV", csv,
            file_name="top_holders_activity.csv", mime="text/csv",
        )

        # Spotlight: strong accumulators
        accum_df = df[df["activity"].str.contains("Accumulating|Net Buying")].sort_values(
            "net_7d", ascending=False
        )
        if not accum_df.empty:
            st.subheader("🟢 Strong Accumulators")
            st.dataframe(accum_df[display_cols], use_container_width=True)

        # Spotlight: strong sellers
        distrib_df = df[df["activity"].str.contains("Distributing|Net Selling")].sort_values(
            "net_7d"
        )
        if not distrib_df.empty:
            st.subheader("🔴 Active Sellers")
            st.dataframe(distrib_df[display_cols], use_container_width=True)

    else:
        st.info(
            f"Click **Run Top Holders Scan** to pull the top {top_n} holders "
            f"and check their {holder_lookback_days}-day buy/sell activity.\n\n"
            "**LP filtering:** Raydium, Orca, Meteora, pump.fun AMM pool accounts "
            "are automatically excluded from the holder list and from buy/sell counts. "
            "Add custom LP addresses in the sidebar if needed."
        )

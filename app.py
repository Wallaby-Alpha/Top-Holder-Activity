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
SPL_TOKEN_PROGRAM    = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM   = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

def _query_holders_for_program(rpc_url, program_id, mint, all_lp, datasize=None):
    """
    Call getProgramAccounts for `program_id` filtered by mint.
    `datasize` is optional — Token-2022 accounts vary in size (extensions),
    so we omit the dataSize filter for that program and rely solely on the
    memcmp filter at offset 0 (mint address).
    """
    filters = [{"memcmp": {"offset": 0, "bytes": mint}}]
    if datasize:
        filters.insert(0, {"dataSize": datasize})

    params = [program_id, {"encoding": "jsonParsed", "filters": filters}]
    try:
        result = rpc_call(rpc_url, "getProgramAccounts", params)
    except RuntimeError:
        return []

    holders = []
    for acct in (result or []):
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
        holders.append({
            "wallet": owner,
            "token_account": token_acct,
            "current_balance": amt,
            "token_program": program_id,
        })
    return holders


def get_top_holders(rpc_url, mint, top_n, all_lp):
    """
    Try standard SPL Token program first (account size = 165 bytes).
    If that returns nothing, fall back to Token-2022 program (variable
    account size — no dataSize filter applied).
    Returns (holders_list, program_label).
    """
    # Standard SPL
    holders = _query_holders_for_program(
        rpc_url, SPL_TOKEN_PROGRAM, mint, all_lp, datasize=165
    )
    program_label = "SPL Token (standard)"

    if not holders:
        # Token-2022 fallback — no fixed dataSize
        holders = _query_holders_for_program(
            rpc_url, TOKEN_2022_PROGRAM, mint, all_lp, datasize=None
        )
        program_label = "Token-2022"

    holders.sort(key=lambda x: x["current_balance"], reverse=True)
    return holders[:top_n], program_label


def get_holder_activity(rpc_url, token_acct, owner, mint, lookback_days,
                         sig_limit, all_lp, batch_size=25):
    """
    Walk the OWNER WALLET address for recent signatures (not the token account).
    Token account addresses rarely appear as signers — the wallet is always the
    fee payer/signer, so getSignaturesForAddress on the wallet gives us every
    swap/transfer this wallet has participated in, which we then filter for
    balance changes on this specific mint.
    """
    cutoff_ts = int((dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(days=lookback_days)).timestamp())

    # Walk the wallet address, not the token account
    sigs = get_all_signatures(rpc_url, owner, sig_limit, after_ts=cutoff_ts)
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
            # Only count a transaction once even if it has multiple change records
            tx_had_activity = False
            for (chg_owner, delta, ts, is_lp) in changes:
                if is_lp or chg_owner != owner:
                    continue
                if delta > 0:
                    buys += delta
                else:
                    sells += abs(delta)
                tx_had_activity = True
            if tx_had_activity:
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
# TAB 2 - Top Holders Activity (Solscan CSV upload)
# ===========================================================================
with tab2:
    st.header("Top Holders Activity")

    st.info(
        "**How to get the CSV from Solscan:**\n"
        "1. Go to `https://solscan.io/token/<mint_address>#transfers`\n"
        "2. Click **Export** (top-right of the transfers table) → download the CSV\n"
        "3. Upload it below. The CSV contains every transfer with sender, receiver, "
        "amount, and timestamp — far more reliable than RPC for high-volume tokens."
    )

    uploaded_csv = st.file_uploader(
        "Upload Solscan token transfers CSV", type=["csv"], key="solscan_csv"
    )

    # Optional: also pull current holder balances from RPC to enrich the output
    fetch_balances = st.checkbox(
        "Also fetch current holder balances from RPC (slower but adds Current Balance column)",
        value=True,
    )

    run_holders = st.button("🚀 Analyse", type="primary", key="run_holders")

    if run_holders and uploaded_csv:

        # ------------------------------------------------------------------
        # Step 1: parse the Solscan CSV
        # Solscan transfer exports typically have these columns (may vary):
        #   Signature, Block, Time, From, To, Amount, Token, Decimals
        # Some exports use "Source" / "Destination" or "Sender" / "Receiver"
        # We normalise whatever we find.
        # ------------------------------------------------------------------
        raw = pd.read_csv(uploaded_csv)
        st.caption(f"Raw CSV columns: `{list(raw.columns)}`")

        # Normalise column names — lowercase + strip spaces
        raw.columns = [c.strip().lower().replace(" ", "_") for c in raw.columns]

        # Detect wallet columns
        col_map = {}
        for candidate in ["from", "source", "sender", "from_address"]:
            if candidate in raw.columns:
                col_map["from"] = candidate
                break
        for candidate in ["to", "destination", "receiver", "to_address"]:
            if candidate in raw.columns:
                col_map["to"] = candidate
                break
        for candidate in ["amount", "token_amount", "quantity", "value"]:
            if candidate in raw.columns:
                col_map["amount"] = candidate
                break
        for candidate in ["time", "block_time", "timestamp", "date", "datetime"]:
            if candidate in raw.columns:
                col_map["time"] = candidate
                break

        missing = [k for k in ["from", "to", "amount"] if k not in col_map]
        if missing:
            st.error(
                f"Could not find columns for: {missing}. "
                f"Columns in your CSV: `{list(raw.columns)}`. "
                "Please check the export format and try again."
            )
            st.stop()

        # Build a clean transfers dataframe
        transfers = pd.DataFrame()
        transfers["from"]   = raw[col_map["from"]].astype(str).str.strip()
        transfers["to"]     = raw[col_map["to"]].astype(str).str.strip()
        transfers["amount"] = pd.to_numeric(raw[col_map["amount"]], errors="coerce").fillna(0)

        if col_map.get("time"):
            transfers["time"] = pd.to_datetime(raw[col_map["time"]], utc=True, errors="coerce")
        else:
            transfers["time"] = pd.NaT

        # Apply LP filter: drop rows where from OR to is a known LP address
        lp_mask = transfers["from"].isin(ALL_LP) | transfers["to"].isin(ALL_LP)
        lp_removed = lp_mask.sum()
        transfers = transfers[~lp_mask].copy()

        st.markdown(
            f"- **Total transfer rows in CSV:** {len(raw)}\n"
            f"- **LP / pool rows filtered out:** {lp_removed}\n"
            f"- **Clean transfer rows remaining:** {len(transfers)}"
        )

        if transfers.empty:
            st.warning("No transfers remain after LP filtering.")
            st.stop()

        # ------------------------------------------------------------------
        # Step 2: apply lookback window
        # ------------------------------------------------------------------
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=holder_lookback_days)
        if transfers["time"].notna().any():
            recent = transfers[transfers["time"] >= cutoff].copy()
            st.caption(
                f"Transfers within last {holder_lookback_days} days: {len(recent)} "
                f"(from {cutoff.strftime('%Y-%m-%d %H:%M UTC')} to now)"
            )
        else:
            recent = transfers.copy()
            st.warning("No timestamp column found — using all rows for activity (no time filter applied).")

        # ------------------------------------------------------------------
        # Step 3: aggregate buys and sells per wallet
        # A wallet "buys" when it appears in the TO column (receives tokens).
        # A wallet "sells" when it appears in the FROM column (sends tokens).
        # ------------------------------------------------------------------
        buys = (
            recent.groupby("to")["amount"].sum()
            .rename("buys")
            .reset_index()
            .rename(columns={"to": "wallet"})
        )
        sells = (
            recent.groupby("from")["amount"].sum()
            .rename("sells")
            .reset_index()
            .rename(columns={"from": "wallet"})
        )
        tx_counts = (
            recent.assign(wallet=recent["to"])
            .groupby("wallet")["amount"].count()
            .add(
                recent.assign(wallet=recent["from"])
                .groupby("wallet")["amount"].count(),
                fill_value=0,
            )
            .rename("tx_count")
            .reset_index()
        )

        activity_df = (
            buys.merge(sells, on="wallet", how="outer")
            .merge(tx_counts, on="wallet", how="outer")
            .fillna(0)
        )
        activity_df["net"] = activity_df["buys"] - activity_df["sells"]

        # ------------------------------------------------------------------
        # Step 4: compute all-time balance per wallet from full CSV
        # (not just the lookback window) for a "current balance" estimate
        # ------------------------------------------------------------------
        all_buys  = transfers.groupby("to")["amount"].sum().rename("all_buys").reset_index().rename(columns={"to": "wallet"})
        all_sells = transfers.groupby("from")["amount"].sum().rename("all_sells").reset_index().rename(columns={"from": "wallet"})
        balance_df = all_buys.merge(all_sells, on="wallet", how="outer").fillna(0)
        balance_df["csv_balance"] = balance_df["all_buys"] - balance_df["all_sells"]

        activity_df = activity_df.merge(balance_df[["wallet", "csv_balance"]], on="wallet", how="left").fillna(0)

        # ------------------------------------------------------------------
        # Step 5: optionally enrich with live RPC balances
        # ------------------------------------------------------------------
        if fetch_balances and api_key and token_address:
            with st.spinner("Fetching current holder balances from RPC..."):
                try:
                    holders, program_label = get_top_holders(rpc_url, token_address, 2000, ALL_LP)
                    rpc_balance_map = {h["wallet"]: h["current_balance"] for h in holders}
                    activity_df["current_balance"] = activity_df["wallet"].map(rpc_balance_map).fillna(activity_df["csv_balance"])
                    st.caption(f"RPC balances fetched via {program_label} for {len(holders)} holders.")
                except Exception as e:
                    st.warning(f"RPC balance fetch failed ({e}) — using CSV-derived balances.")
                    activity_df["current_balance"] = activity_df["csv_balance"]
        else:
            activity_df["current_balance"] = activity_df["csv_balance"]

        # ------------------------------------------------------------------
        # Step 6: filter to top N by current balance, classify activity
        # ------------------------------------------------------------------
        # Remove dust wallets (zero or negative balance from the CSV)
        activity_df = activity_df[activity_df["current_balance"] > 1e-9].copy()

        # Remove LP addresses that slipped through (belt-and-suspenders)
        activity_df = activity_df[~activity_df["wallet"].isin(ALL_LP)]

        activity_df = activity_df.sort_values("current_balance", ascending=False).head(top_n).copy()
        activity_df["rank"] = range(1, len(activity_df) + 1)

        # Classify using the same logic, adapted for column names
        def classify_row(row):
            net   = row["net"]
            buys  = row["buys"]
            sells = row["sells"]
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

        activity_df["activity"]       = activity_df.apply(classify_row, axis=1)
        activity_df["buy_sell_ratio"] = activity_df.apply(
            lambda r: round(r["buys"] / r["sells"], 2) if r["sells"] > 1e-9 else float("inf"),
            axis=1,
        )

        # ------------------------------------------------------------------
        # Step 7: display
        # ------------------------------------------------------------------
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("🟢 Accumulating", (activity_df["activity"].str.contains("Accumulating|Net Buying")).sum())
        col2.metric("🔴 Distributing", (activity_df["activity"].str.contains("Distributing|Net Selling")).sum())
        col3.metric("🟡 Holding",      (activity_df["activity"].str.contains("Holding")).sum())
        col4.metric("⚪ No Activity",  (activity_df["activity"].str.contains("No Activity")).sum())

        display_cols   = ["rank", "wallet", "current_balance", "buys", "sells", "net", "buy_sell_ratio", "tx_count", "activity"]
        display_labels = ["Rank", "Wallet", "Current Balance",
                          f"Buys ({holder_lookback_days}d)", f"Sells ({holder_lookback_days}d)",
                          f"Net ({holder_lookback_days}d)", "Buy/Sell Ratio", "TX Count", "Activity"]

        df_display = activity_df[display_cols].copy()
        df_display.columns = display_labels

        st.subheader(f"Top {len(activity_df)} Holders — {holder_lookback_days}d Activity")
        st.dataframe(df_display, use_container_width=True)
        st.download_button("⬇️ Download CSV", df_display.to_csv(index=False).encode(),
            file_name="top_holders_activity.csv", mime="text/csv")

        accum_df = activity_df[activity_df["activity"].str.contains("Accumulating|Net Buying")].sort_values("net", ascending=False)
        if not accum_df.empty:
            st.subheader("🟢 Strong Accumulators")
            st.dataframe(accum_df[display_cols].rename(columns=dict(zip(display_cols, display_labels))), use_container_width=True)

        dist_df = activity_df[activity_df["activity"].str.contains("Distributing|Net Selling")].sort_values("net")
        if not dist_df.empty:
            st.subheader("🔴 Active Sellers")
            st.dataframe(dist_df[display_cols].rename(columns=dict(zip(display_cols, display_labels))), use_container_width=True)

    elif run_holders and not uploaded_csv:
        st.warning("Please upload a Solscan CSV first.")

"""
app.py — E-commerce Conversion Funnel Analytics dashboard.

Division of labour (the point of this project):
  * SQLite + queries.sql  -> ALL extraction and aggregation. Every count and
    rate on screen is produced by a named query in queries.sql.
  * Python (this file)    -> ALL statistical inference (confidence intervals,
    two-proportion z-tests, chi-square tests, multiple-comparison caution)
    and the UI. pandas only ever holds query RESULT SETS — never raw events,
    and it never aggregates anything SQL could have aggregated.

Data source: data/funnel.db if you've run load_data.py on the real Kaggle
dataset; otherwise a small synthetic demo database is generated on first run
so the app renders out of the box.
"""

from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from scipy import stats

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent
QUERIES_FILE = ROOT / "queries.sql"
REAL_DB = ROOT / "data" / "funnel.db"    # produced by load_data.py
DEMO_DB = ROOT / "data" / "demo.db"      # synthesised below if needed

ALPHA = 0.05                              # base significance level
STEP_LABELS = ["Viewed", "Added to cart", "Purchased"]

st.set_page_config(page_title="E-commerce Funnel Analytics",
                   page_icon="🛒", layout="wide")


# ---------------------------------------------------------------------------
# SQL plumbing: parse queries.sql into {name: sql} and execute against SQLite
# ---------------------------------------------------------------------------
@st.cache_data
def load_queries() -> dict[str, str]:
    """Split queries.sql on '-- name: <x>' markers.

    Keeping SQL in its own file (instead of Python strings) makes it
    reviewable as SQL, runnable in any SQLite client, and lets the app show
    the *exact* text it executes.
    """
    text = QUERIES_FILE.read_text()
    parts = re.split(r"^--\s*name:\s*(\w+)\s*$", text, flags=re.MULTILINE)
    # parts = [preamble, name1, sql1, name2, sql2, ...]
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def get_db_path() -> Path:
    """Prefer the real sampled DB; fall back to the baked-in demo."""
    if REAL_DB.exists():
        return REAL_DB
    if not DEMO_DB.exists():
        build_demo_db(DEMO_DB)
    return DEMO_DB


@st.cache_data
def run_query(db_path: str, name: str) -> pd.DataFrame:
    """Execute one named query. This is the ONLY place SQL results enter
    pandas — everything downstream is inference and presentation."""
    sql = load_queries()[name]
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(sql, conn)


def sql_expander(name: str, label: str = "Show the SQL behind this view") -> None:
    """Every view exposes its query — the SQL is part of the product."""
    with st.expander(f"🧾 {label} — `{name}`"):
        st.code(load_queries()[name], language="sql")


# ---------------------------------------------------------------------------
# Demo data: a small synthetic clickstream so the app renders before any
# real data is loaded. Mirrors the Kaggle schema exactly, with deliberate
# category/price effects so the significance tests have something to find,
# plus injected quality problems so the data-quality panel isn't empty.
# ---------------------------------------------------------------------------
def build_demo_db(path: Path) -> None:
    rng = np.random.default_rng(42)          # deterministic demo
    path.parent.mkdir(parents=True, exist_ok=True)

    cats = {   # top_category: (subcats, base cart prob, base purchase|cart prob, price scale)
        "electronics": (["smartphone", "audio.headphone", "tv"], 0.32, 0.48, 300),
        "appliances":  (["kitchen.refrigerator", "environment.vacuum"], 0.24, 0.40, 180),
        "apparel":     (["shoes", "jacket"], 0.20, 0.30, 60),
        "furniture":   (["bedroom.bed", "kitchen.table"], 0.12, 0.22, 250),
        "computers":   (["notebook", "desktop"], 0.28, 0.44, 500),
    }
    cat_names = list(cats)
    brands = ["acme", "globex", "initech", "umbrella", "stark", None]

    rows = []
    base_ts = 1572566400  # 2019-11-01 00:00:00 UTC — matches dataset epoch
    n_users = 4000
    for u in range(n_users):
        user_id = 500_000_000 + u
        n_sessions = rng.integers(1, 5)
        # user's home category (users mostly browse one category)
        cat = cat_names[rng.integers(0, len(cat_names))]
        subcats, p_cart, p_buy, price_scale = cats[cat]
        day0 = int(rng.integers(0, 25)) * 86400
        for s in range(n_sessions):
            session = f"demo-{user_id}-{s}"
            t = base_ts + day0 + s * int(rng.integers(3600, 4 * 86400)) \
                + int(rng.integers(0, 86400))
            n_views = int(rng.integers(1, 8))
            # small product pool so per-product stats (abandoned products
            # table) have enough cart-adds per product to clear thresholds
            product = int(rng.integers(1_000_000, 1_000_120))
            for _ in range(n_views):
                sub = subcats[rng.integers(0, len(subcats))]
                price = round(float(rng.lognormal(0, 0.5) * price_scale), 2)
                brand = brands[rng.integers(0, len(brands))]
                rows.append((int(t), "view", product, f"{cat}.{sub}", brand,
                             price, user_id, session))
                t += int(rng.integers(5, 300))
            # cheap items convert better than expensive ones (price effect)
            price_factor = 1.25 if price < price_scale else 0.75
            if rng.random() < p_cart * price_factor:
                rows.append((int(t), "cart", product, f"{cat}.{subcats[0]}",
                             brands[rng.integers(0, len(brands))],
                             price, user_id, session))
                t += int(rng.integers(10, 600))
                if rng.random() < p_buy * price_factor:
                    # Mix of buyer timing so the timing tab has real spread:
                    # ~55% buy in the same session, ~20% later the same day,
                    # ~25% come back days later in a new session.
                    roll = rng.random()
                    if roll < 0.55:
                        session_p = session
                    elif roll < 0.75:
                        t += int(rng.integers(3600, 20 * 3600))
                        session_p = f"demo-{user_id}-ret{s}"
                    else:
                        t += int(rng.integers(86400, 9 * 86400))
                        session_p = f"demo-{user_id}-ret{s}"
                    rows.append((int(t), "purchase", product,
                                 f"{cat}.{subcats[0]}", brands[0],
                                 price, user_id, session_p))

    # Inject quality problems on purpose:
    rows.extend(rows[:40])                                   # 40 exact duplicates
    for j in range(25):                                      # purchase, no view
        uid = 600_000_000 + j
        rows.append((base_ts + j * 3600, "purchase", 1_000_001,
                     "electronics.smartphone", "acme", 199.99,
                     uid, f"demo-ghost-{j}"))

    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS events;
            CREATE TABLE events (
                event_time TEXT, event_ts INTEGER, event_type TEXT,
                product_id INTEGER, category_code TEXT, brand TEXT,
                price REAL, user_id INTEGER, user_session TEXT
            );
            """
        )
        conn.executemany(
            """INSERT INTO events
               (event_ts, event_type, product_id, category_code, brand,
                price, user_id, user_session)
               VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            "UPDATE events SET event_time = "
            "strftime('%Y-%m-%d %H:%M:%S', event_ts, 'unixepoch') || ' UTC'"
        )
        conn.executescript(
            """
            CREATE INDEX idx_demo_user ON events (user_id);
            CREATE INDEX idx_demo_session ON events (user_session);
            """
        )


# ---------------------------------------------------------------------------
# Statistics — ALL inference lives here, in Python, on SQL result sets.
# ---------------------------------------------------------------------------
def wilson_ci(successes: int, n: int, alpha: float = ALPHA) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the naive Wald interval (p ± z*sqrt(p(1-p)/n)) because
    Wilson behaves sensibly at the extremes funnels live in: very small
    proportions and (in tiny segments) very small n. Wald collapses to a
    zero-width interval when successes = 0; Wilson does not.
    """
    if n == 0:
        return (0.0, 0.0)                      # empty segment guard
    z = stats.norm.ppf(1 - alpha / 2)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (max(0.0, centre - half), min(1.0, centre + half))


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Two-proportion z-test: is conversion rate 1 different from rate 2?

    H0: p1 == p2. Uses the pooled standard error, which is correct under H0.
    Returns (z, two-sided p-value). Guards: if either denominator is 0, or
    the pooled rate is 0 or 1 (no variance — e.g. zero conversions in BOTH
    groups), the test is undefined and we return (nan, nan) so the caller
    can report "not testable" instead of a fake result.
    """
    if n1 == 0 or n2 == 0:
        return (float("nan"), float("nan"))
    p_pool = (k1 + k2) / (n1 + n2)
    if p_pool in (0.0, 1.0):
        return (float("nan"), float("nan"))
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    z = (k1 / n1 - k2 / n2) / se
    p = 2 * (1 - stats.norm.cdf(abs(z)))
    return (z, p)


def chi_square_segments(counts: pd.DataFrame, success_col: str,
                        total_col: str) -> tuple[float, float, bool]:
    """Chi-square test of homogeneity across ALL segments at once.

    Answers the omnibus question: "is conversion the same in every segment?"
    before we look at any individual pair. Builds a (segments x 2) table of
    [converted, not converted]. Returns (chi2, p, valid) where valid=False
    if the classic rule of thumb (all expected counts >= 5) fails — in that
    case the asymptotic p-value shouldn't be trusted and the UI says so.
    """
    table = np.column_stack([
        counts[success_col].to_numpy(),
        (counts[total_col] - counts[success_col]).to_numpy(),
    ])
    table = table[table.sum(axis=1) > 0]      # drop empty segments entirely
    if len(table) < 2 or table[:, 0].sum() == 0:
        return (float("nan"), float("nan"), False)
    chi2, p, _, expected = stats.chi2_contingency(table)
    return (chi2, p, bool((expected >= 5).all()))


def segment_vs_rest_tests(df: pd.DataFrame, seg_col: str, success_col: str,
                          total_col: str) -> pd.DataFrame:
    """For each segment, z-test its conversion rate against ALL OTHER
    segments pooled ("this segment vs the rest").

    Multiple comparisons: with k segments we run k tests, so by chance alone
    ~5% of true-null tests cross p < .05. We apply a Bonferroni-corrected
    threshold alpha/k — conservative, but the honest default when the
    audience will act on 'significant' flags. Both raw p and the corrected
    verdict are returned so the UI can show its working.
    """
    k = len(df)
    bonferroni_alpha = ALPHA / max(k, 1)
    out = []
    total_success = df[success_col].sum()
    total_n = df[total_col].sum()
    for _, row in df.iterrows():
        k1, n1 = int(row[success_col]), int(row[total_col])
        k2, n2 = int(total_success - k1), int(total_n - n1)
        z, p = two_proportion_z(k1, n1, k2, n2)
        rate = k1 / n1 if n1 else float("nan")
        rest = k2 / n2 if n2 else float("nan")
        lo, hi = wilson_ci(k1, n1)
        if math.isnan(p):
            verdict = "not testable (empty/zero-variance segment)"
        elif p < bonferroni_alpha:
            verdict = "significant (survives Bonferroni)"
        elif p < ALPHA:
            verdict = "borderline — significant only before correction"
        else:
            verdict = "no evidence of a real difference"
        out.append({
            "segment": row[seg_col],
            "n": n1,
            "rate": rate,
            "rate vs rest": (rate - rest) if not math.isnan(rest) else float("nan"),
            "ci_low": lo,
            "ci_high": hi,
            "z": z,
            "p_value": p,
            "verdict": verdict,
        })
    res = pd.DataFrame(out)
    res.attrs["bonferroni_alpha"] = bonferroni_alpha
    res.attrs["n_tests"] = k
    return res


def funnel_steps(row: pd.Series) -> list[tuple[str, int, int]]:
    """Turn a funnel query row into [(label, successes, denominator), ...].

    Step 1's denominator is viewers themselves (rate vs total units is shown
    separately); step 2 = carters / viewers; step 3 = purchasers / carters.
    """
    v, c, p = int(row["step_view"]), int(row["step_cart"]), int(row["step_purchase"])
    return [("Viewed", v, v), ("Added to cart", c, v), ("Purchased", p, c)]


def fmt_pct(x: float) -> str:
    return "—" if (x is None or math.isnan(x)) else f"{100 * x:.1f}%"


def fmt_p(p: float) -> str:
    if math.isnan(p):
        return "—"
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


# ---------------------------------------------------------------------------
# Chart builders (presentation only — numbers arrive pre-aggregated from SQL)
# ---------------------------------------------------------------------------
def funnel_figure(row: pd.Series, title: str) -> go.Figure:
    v, c, p = int(row["step_view"]), int(row["step_cart"]), int(row["step_purchase"])
    fig = go.Figure(go.Funnel(
        y=STEP_LABELS,
        x=[v, c, p],
        textinfo="value+percent previous",
        marker={"color": ["#4C78A8", "#F58518", "#54A24B"]},
    ))
    fig.update_layout(title=title, height=380, margin=dict(t=50, b=10))
    return fig


def grouped_rate_bars(df: pd.DataFrame, seg_col: str, title: str) -> go.Figure:
    """Side-by-side step-conversion bars per segment, with CI error bars."""
    fig = go.Figure()
    steps = [("view → cart", "step_cart", "step_view"),
             ("cart → purchase", "step_purchase", "step_cart")]
    colors = ["#F58518", "#54A24B"]
    for (label, num, den), color in zip(steps, colors):
        rates, err_lo, err_hi = [], [], []
        for _, r in df.iterrows():
            n, k = int(r[den]), int(r[num])
            rate = k / n if n else 0.0
            lo, hi = wilson_ci(k, n)
            rates.append(100 * rate)
            err_lo.append(100 * (rate - lo))
            err_hi.append(100 * (hi - rate))
        fig.add_trace(go.Bar(
            name=label, x=df[seg_col].astype(str), y=rates,
            marker_color=color,
            error_y=dict(type="data", array=err_hi, arrayminus=err_lo),
        ))
    fig.update_layout(barmode="group", title=title, yaxis_title="conversion %",
                      height=420, margin=dict(t=50, b=10))
    return fig


# ---------------------------------------------------------------------------
# App body
# ---------------------------------------------------------------------------
def main() -> None:
    db_path = str(get_db_path())
    using_demo = db_path.endswith("demo.db")

    st.title("🛒 E-commerce Conversion Funnel Analytics")
    st.caption("SQL (SQLite) does every aggregation · Python does every "
               "statistical test · expand the 🧾 panels to see each query.")

    # Sidebar: data source + headline dataset stats (from SQL, of course)
    with st.sidebar:
        st.header("Data source")
        if using_demo:
            st.warning("Running on the **built-in synthetic demo dataset**. "
                       "Load the real Kaggle data with:\n\n"
                       "`python load_data.py 2019-Nov.csv`", icon="🧪")
        else:
            st.success("Using `data/funnel.db` (sampled real data).", icon="🗄️")
        summary = run_query(db_path, "dataset_summary").iloc[0]
        st.metric("Events", f"{int(summary['events']):,}")
        st.metric("Users", f"{int(summary['users']):,}")
        st.metric("Sessions", f"{int(summary['sessions']):,}")
        st.caption(f"{summary['first_event']} → {summary['last_event']}")
        sql_expander("dataset_summary", "SQL for these stats")

    tabs = st.tabs(["📉 Funnel overview", "🧩 Segments", "🛒 Abandonment",
                    "⏱️ Timing", "🔍 Data quality"])

    # ------------------------------------------------------------------ #
    # TAB 1 — Funnel overview                                             #
    # ------------------------------------------------------------------ #
    with tabs[0]:
        unit = st.radio("Funnel unit", ["Per user", "Per session"],
                        horizontal=True)
        qname = "funnel_user" if unit == "Per user" else "funnel_session"
        row = run_query(db_path, qname).iloc[0]

        left, right = st.columns([3, 2])
        with left:
            st.plotly_chart(funnel_figure(row, f"{unit} funnel"),
                            width="stretch")
        with right:
            st.subheader("Step conversion with 95% CIs")
            for label, k, n in funnel_steps(row)[1:]:
                rate = k / n if n else float("nan")
                lo, hi = wilson_ci(k, n)
                prev = "viewers" if label == "Added to cart" else "carters"
                st.metric(
                    f"{label} (of {prev})",
                    fmt_pct(rate),
                    help=f"{k:,} / {n:,} · 95% CI [{fmt_pct(lo)}, {fmt_pct(hi)}] "
                         "(Wilson interval)",
                )
                st.caption(f"95% CI: **{fmt_pct(lo)} – {fmt_pct(hi)}** "
                           f"({k:,} of {n:,})")
            end_to_end = (int(row["step_purchase"]) / int(row["step_view"])
                          if int(row["step_view"]) else float("nan"))
            st.metric("End-to-end (view → purchase)", fmt_pct(end_to_end))

        st.info(
            "**Why per-user and per-session rates differ:** a session only "
            "converts if the whole journey fits inside one visit. Shoppers "
            "often research in one session and buy in a later one, so the "
            "same purchase counts as a *user* conversion but leaves several "
            "*sessions* unconverted. Per-user answers *'do we eventually "
            "convert people?'*; per-session answers *'how good is a single "
            "visit at closing?'* — different questions, so keep both.",
            icon="💡",
        )
        sql_expander(qname)

    # ------------------------------------------------------------------ #
    # TAB 2 — Segment comparison (categories & price quartiles)           #
    # ------------------------------------------------------------------ #
    with tabs[1]:
        seg_kind = st.radio("Split by", ["Top-level category", "Price quartile"],
                            horizontal=True)

        if seg_kind == "Top-level category":
            qname = "funnel_by_category"
            df = run_query(db_path, qname)
            seg_col = "top_category"
            df[seg_col] = df[seg_col].astype(str)
        else:
            qname = "funnel_by_price_quartile"
            df = run_query(db_path, qname)
            seg_col = "quartile_label"
            df[seg_col] = df.apply(
                lambda r: f"Q{int(r['price_quartile'])} "
                          f"(${r['price_min']:.0f}–${r['price_max']:.0f})",
                axis=1,
            )

        if df.empty:
            st.warning("No segments met the minimum-size threshold in this "
                       "sample. Load more data with load_data.py.")
        else:
            st.plotly_chart(
                grouped_rate_bars(df, seg_col,
                                  f"Step conversion by {seg_kind.lower()} "
                                  "(error bars = 95% Wilson CIs)"),
                width="stretch")

            step_choice = st.selectbox(
                "Which step to test for differences?",
                ["view → cart", "cart → purchase"])
            num, den = (("step_cart", "step_view")
                        if step_choice == "view → cart"
                        else ("step_purchase", "step_cart"))

            # --- omnibus chi-square first: any difference at all? -------
            chi2, chi_p, chi_valid = chi_square_segments(df, num, den)
            st.subheader("Are the differences real or noise?")
            if math.isnan(chi_p):
                st.warning("Chi-square not computable (too few usable "
                           "segments or zero conversions everywhere).")
            else:
                msg = (f"**Omnibus chi-square across all segments:** "
                       f"χ² = {chi2:.1f}, p {'' if chi_p >= 0.001 else ''}= "
                       f"{fmt_p(chi_p)}. ")
                if chi_p < ALPHA:
                    msg += ("At least one segment's conversion genuinely "
                            "differs from the others — see which below.")
                else:
                    msg += ("No evidence that any segment differs — treat "
                            "the per-segment gaps below as noise.")
                if not chi_valid:
                    msg += (" ⚠️ Some expected cell counts are < 5, so this "
                            "p-value is approximate.")
                st.markdown(msg)

            # --- per-segment z-tests vs the rest, Bonferroni-corrected --
            tests = segment_vs_rest_tests(df, seg_col, num, den)
            st.caption(
                f"Each segment is z-tested against all other segments pooled. "
                f"**Multiple-comparisons caution:** {tests.attrs['n_tests']} "
                f"tests are being run, so the significance threshold is "
                f"Bonferroni-corrected to α = 0.05/{tests.attrs['n_tests']} "
                f"= {tests.attrs['bonferroni_alpha']:.4f}. Differences marked "
                f"'borderline' would look significant in isolation but are "
                f"exactly the kind of result that appears by chance when you "
                f"test many segments."
            )
            show = tests.copy()
            show["rate"] = show["rate"].map(fmt_pct)
            show["rate vs rest"] = show["rate vs rest"].map(
                lambda d: "—" if math.isnan(d) else f"{100 * d:+.1f} pp")
            show["95% CI"] = tests.apply(
                lambda r: f"[{fmt_pct(r['ci_low'])}, {fmt_pct(r['ci_high'])}]",
                axis=1)
            show["p_value"] = show["p_value"].map(fmt_p)
            st.dataframe(
                show[["segment", "n", "rate", "95% CI", "rate vs rest",
                      "p_value", "verdict"]],
                width="stretch", hide_index=True)

            real = tests[tests["verdict"].str.startswith("significant")]
            if len(real):
                names = ", ".join(f"**{s}**" for s in real["segment"])
                st.success(f"**Plain-English verdict:** for {step_choice}, "
                           f"the difference is real (not sampling noise) for: "
                           f"{names}. Everything else is within the range "
                           f"you'd expect from chance.", icon="✅")
            else:
                st.info("**Plain-English verdict:** none of the segment "
                        "differences survive multiple-comparison correction "
                        "for this step — don't build a strategy on them.",
                        icon="🎲")

        sql_expander(qname)

    # ------------------------------------------------------------------ #
    # TAB 3 — Abandonment explorer                                        #
    # ------------------------------------------------------------------ #
    with tabs[2]:
        overall = run_query(db_path, "abandonment_overall")
        cols = st.columns(len(overall))
        for col, (_, r) in zip(cols, overall.iterrows()):
            carters, abandoned = int(r["carters"]), int(r["abandoned"])
            rate = abandoned / carters if carters else float("nan")
            lo, hi = wilson_ci(abandoned, carters)
            col.metric(f"Cart abandonment — {r['grain']}", fmt_pct(rate),
                       help=f"{abandoned:,} of {carters:,} carters · "
                            f"95% CI [{fmt_pct(lo)}, {fmt_pct(hi)}]")
        sql_expander("abandonment_overall", "SQL for the overall rates")

        st.subheader("Abandonment by category")
        by_cat = run_query(db_path, "abandonment_by_category")
        if by_cat.empty:
            st.warning("No category reached the 50-carter minimum in this sample.")
        else:
            fig = go.Figure(go.Bar(
                x=by_cat["top_category"], y=by_cat["abandonment_pct"],
                marker_color="#E45756",
                text=by_cat["abandonment_pct"].map(lambda v: f"{v:.0f}%"),
            ))
            fig.update_layout(yaxis_title="abandonment %", height=380,
                              margin=dict(t=30, b=10))
            st.plotly_chart(fig, width="stretch")
        sql_expander("abandonment_by_category", "SQL for abandonment by category")

        st.subheader("Top products added to cart but rarely purchased")
        st.caption("Minimum 20 cart-adds. Low purchase-per-cart on a "
                   "high-cart product usually means a checkout-stage problem: "
                   "shipping shock, stockouts, or price anchoring.")
        prods = run_query(db_path, "top_abandoned_products")
        st.dataframe(prods, width="stretch", hide_index=True)
        sql_expander("top_abandoned_products", "SQL for abandoned products")

    # ------------------------------------------------------------------ #
    # TAB 4 — Timing view                                                 #
    # ------------------------------------------------------------------ #
    with tabs[3]:
        st.subheader("How fast do converters convert?")
        ttc = run_query(db_path, "time_to_convert")
        if ttc.empty:
            st.warning("No converting users in this sample.")
        else:
            c1, c2 = st.columns([2, 3])
            with c1:
                fig = go.Figure(go.Pie(
                    labels=ttc["convert_bucket"], values=ttc["users"],
                    hole=0.45))
                fig.update_layout(title="Same session vs later return",
                                  height=380, margin=dict(t=50, b=10))
                st.plotly_chart(fig, width="stretch")
            with c2:
                dist = run_query(db_path, "time_to_convert_distribution")
                fig = go.Figure(go.Bar(
                    x=dist["delay_bucket"], y=dist["users"],
                    marker_color="#4C78A8"))
                fig.update_layout(
                    title="First view → first purchase delay",
                    yaxis_title="users", height=380, margin=dict(t=50, b=10))
                st.plotly_chart(fig, width="stretch")
            st.dataframe(ttc, width="stretch", hide_index=True)
            st.info("A large 'later return' share is the same phenomenon "
                    "that makes per-session conversion lower than per-user "
                    "conversion: buying journeys span visits.", icon="🔁")
        sql_expander("time_to_convert", "SQL for the conversion buckets")
        sql_expander("time_to_convert_distribution", "SQL for the delay histogram")

        st.subheader("Sessionization sanity checks")
        c1, c2, c3 = st.columns(3)
        with c1:
            eps = run_query(db_path, "events_per_session")
            fig = go.Figure(go.Bar(x=eps["events_bucket"], y=eps["sessions"],
                                   marker_color="#72B7B2"))
            fig.update_layout(title="Events per session", height=330,
                              margin=dict(t=40, b=10))
            st.plotly_chart(fig, width="stretch")
        with c2:
            dur = run_query(db_path, "session_duration")
            fig = go.Figure(go.Bar(x=dur["duration_bucket"], y=dur["sessions"],
                                   marker_color="#B279A2"))
            fig.update_layout(title="Session duration (MAX−MIN)", height=330,
                              margin=dict(t=40, b=10))
            st.plotly_chart(fig, width="stretch")
        with c3:
            gaps = run_query(db_path, "lag_event_gaps")
            fig = go.Figure(go.Bar(x=gaps["gap_bucket"], y=gaps["gap_count"],
                                   marker_color="#FF9DA6"))
            fig.update_layout(title="Gaps between consecutive events (LAG)",
                              height=330, margin=dict(t=40, b=10))
            st.plotly_chart(fig, width="stretch")
        sql_expander("events_per_session", "SQL — events per session")
        sql_expander("session_duration", "SQL — session duration")
        sql_expander("lag_event_gaps", "SQL — LAG() event gaps")

    # ------------------------------------------------------------------ #
    # TAB 5 — Data quality                                                #
    # ------------------------------------------------------------------ #
    with tabs[4]:
        st.subheader("Quality checks run before trusting any funnel number")
        dq = run_query(db_path, "data_quality")
        dq_show = dq.copy()
        dq_show["value"] = dq_show["value"].map(lambda v: f"{int(v):,}")
        dq_show["pct"] = dq_show["pct"].map(
            lambda v: "—" if pd.isna(v) else f"{v:.2f}%")
        st.dataframe(dq_show, width="stretch", hide_index=True)
        st.info(
            "**Why an analyst checks this first:** funnels silently absorb "
            "data problems. Duplicated events inflate every count; NULL "
            "categories mean the category split is really 'the labelled "
            "subset'; purchases with no recorded view mean tracking gaps — "
            "so the true view→purchase rate is *at least* what the funnel "
            "shows, not exactly it. Quantifying these up front turns "
            "'the funnel says 4.2%' into '4.2% with known caveats', which "
            "is the only version worth presenting.",
            icon="🔍",
        )
        sql_expander("data_quality")


if __name__ == "__main__":
    main()

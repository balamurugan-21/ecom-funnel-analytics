"""
load_data.py — load the Kaggle "eCommerce behavior data from multi category
store" CSV into a local SQLite database, SAMPLING BY USER.

Usage:
    python load_data.py path/to/2019-Nov.csv            # default ~1.5M events
    python load_data.py path/to/2019-Nov.csv --target-events 1000000
    python load_data.py path/to/2019-Nov.csv --keep-fraction 0.02

Output: data/funnel.db  (the Streamlit app picks this up automatically)

============================ WHY SAMPLE BY USER ============================
The raw monthly files are several GB (tens of millions of rows) — too big to
ship in a repo or query interactively on a laptop. So we sample. But HOW you
sample determines whether the funnel survives:

  * Sampling BY ROW (e.g. "keep every 20th event") destroys funnels. A user's
    journey is view -> cart -> purchase spread across many rows; random row
    sampling keeps the view but drops the purchase (or vice versa) for most
    users. Conversion rates computed on row-sampled data are garbage — they
    are biased DOWNWARD in unpredictable, stage-dependent ways.

  * Sampling BY USER keeps a random subset of users but ALL events for each
    kept user. Every kept user's funnel is complete, so per-user conversion
    rates are unbiased estimates of the full-population rates (it's a simple
    random sample of users). Sessions also stay intact, because a session
    belongs to exactly one user.

Implementation detail: the file is far too big to load and group, so we
stream it in chunks. To decide "is this user kept?" consistently across ALL
chunks without storing a giant set of every user_id ever seen, we use a
DETERMINISTIC HASH of the user_id: a user is kept iff
        md5(user_id) mod 10_000 < keep_permyriad
The same user_id always hashes the same way, so a user kept in chunk 1 is
also kept in chunk 900 — their journey stays whole. This is the standard
trick for consistent sampling in streaming pipelines.

The script auto-calibrates: it reads the first chunk to estimate events-per-
user density, picks a keep-fraction that lands near --target-events, then
streams the whole file once.
===========================================================================
"""

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "funnel.db"
CHUNK_ROWS = 500_000          # rows per streamed chunk; tune to your RAM
HASH_SPACE = 10_000           # hash buckets; keep-fraction resolution = 0.01%

EXPECTED_COLS = [
    "event_time", "event_type", "product_id", "category_code",
    "brand", "price", "user_id", "user_session",
]


def user_kept(user_id, keep_buckets: int) -> bool:
    """Deterministic per-user coin flip.

    md5 (not Python's built-in hash(), which is salted per-process and NOT
    reproducible across runs) maps the user_id into one of HASH_SPACE
    buckets. Keeping buckets [0, keep_buckets) keeps ~keep_buckets/HASH_SPACE
    of users — the SAME users on every run and in every chunk.
    """
    digest = hashlib.md5(str(user_id).encode()).hexdigest()
    return int(digest[:8], 16) % HASH_SPACE < keep_buckets


def kept_mask(series: pd.Series, keep_buckets: int) -> pd.Series:
    """Vectorised-ish wrapper over user_kept for a chunk's user_id column.

    We memoise per unique user_id within the chunk — chunks contain far
    fewer unique users than rows, so this is much cheaper than hashing
    every row.
    """
    uniques = series.unique()
    lookup = {u: user_kept(u, keep_buckets) for u in uniques}
    return series.map(lookup)


def calibrate_keep_buckets(csv_path: Path, target_events: int) -> int:
    """Estimate what fraction of users we need to hit ~target_events.

    Reads ONE chunk, computes events-per-unique-user, extrapolates using the
    file size ratio to estimate total events, then solves
        keep_fraction = target_events / estimated_total_events.
    Rough is fine — we only need the right order of magnitude, and we clamp
    to at least 1 bucket so tiny files keep something.
    """
    first = pd.read_csv(csv_path, nrows=CHUNK_ROWS)
    file_bytes = csv_path.stat().st_size
    # Estimate bytes per row from the chunk we read (re-serialisation is
    # approximate; the header offset is negligible at this scale).
    sample_bytes = first.memory_usage(deep=True).sum()
    # Safer: estimate rows from average line length in the raw file.
    with open(csv_path, "rb") as fh:
        head = fh.read(2_000_000)
    avg_line = max(len(head) / max(head.count(b"\n"), 1), 1)
    est_total_rows = int(file_bytes / avg_line)
    keep_fraction = min(1.0, target_events / max(est_total_rows, 1))
    buckets = max(1, round(keep_fraction * HASH_SPACE))
    print(f"[calibrate] est. total rows ~{est_total_rows:,}; "
          f"keeping ~{buckets / HASH_SPACE:.2%} of users "
          f"to target ~{target_events:,} events")
    return buckets


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS events;
        CREATE TABLE events (
            event_time    TEXT,     -- original timestamp string (UTC)
            event_ts      INTEGER,  -- unix epoch seconds, precomputed for SQL math
            event_type    TEXT,     -- 'view' | 'cart' | 'purchase'
            product_id    INTEGER,
            category_code TEXT,     -- dot-separated, may be NULL
            brand         TEXT,     -- may be NULL
            price         REAL,
            user_id       INTEGER,
            user_session  TEXT
        );
        """
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    """Indexes AFTER bulk insert (much faster than maintaining them per-row).

    The funnel queries group by user_id and user_session constantly, so
    those two indexes pay for themselves immediately.
    """
    print("[index] building indexes...")
    conn.executescript(
        """
        CREATE INDEX idx_events_user    ON events (user_id);
        CREATE INDEX idx_events_session ON events (user_session);
        CREATE INDEX idx_events_type    ON events (event_type);
        ANALYZE;
        """
    )


def load(csv_path: Path, keep_buckets: int) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=OFF;")   # bulk-load speed; not a server
    conn.execute("PRAGMA synchronous=OFF;")
    create_schema(conn)

    total_kept = 0
    kept_users: set = set()   # only KEPT users are tracked (small), for stats

    reader = pd.read_csv(
        csv_path,
        chunksize=CHUNK_ROWS,
        usecols=EXPECTED_COLS,
        dtype={
            "event_type": "category",
            "category_code": "object",
            "brand": "object",
            "user_session": "object",
        },
    )

    for i, chunk in enumerate(reader):
        # --- per-user sampling: keep ALL rows of kept users, no others ----
        mask = kept_mask(chunk["user_id"], keep_buckets)
        sampled = chunk.loc[mask].copy()
        if sampled.empty:
            continue

        # Normalise the timestamp: the dataset uses "... UTC" suffixed
        # strings. We store the original string AND epoch seconds — the
        # epoch column lets every SQL window/duration computation be plain
        # integer arithmetic (fast, no date parsing inside queries).
        ts = pd.to_datetime(
            sampled["event_time"].str.replace(" UTC", "", regex=False),
            format="%Y-%m-%d %H:%M:%S",
            errors="coerce",
            utc=True,
        )
        # Resolution-independent epoch conversion: pandas 2.x may parse
        # timestamps as ns OR us resolution, so never assume ns and divide
        # by 1e9 — subtracting the epoch and floor-dividing by 1 second is
        # correct at any resolution.
        sampled["event_ts"] = (
            (ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
        )
        sampled = sampled.dropna(subset=["event_ts", "user_id", "user_session"])

        sampled[
            ["event_time", "event_ts", "event_type", "product_id",
             "category_code", "brand", "price", "user_id", "user_session"]
        ].to_sql("events", conn, if_exists="append", index=False)

        total_kept += len(sampled)
        kept_users.update(sampled["user_id"].unique())
        print(f"[chunk {i + 1}] kept {len(sampled):>7,} rows "
              f"(running total {total_kept:,} events, "
              f"{len(kept_users):,} users)")

    create_indexes(conn)
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    size_mb = DB_PATH.stat().st_size / 1e6
    print(f"\n[done] {total_kept:,} events / {len(kept_users):,} users "
          f"-> {DB_PATH} ({size_mb:.1f} MB)")
    if size_mb > 90:
        print("[warn] DB is close to GitHub's 100 MB per-file limit. "
              "Re-run with a lower --target-events to shrink it.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="path to the Kaggle CSV (e.g. 2019-Nov.csv)")
    ap.add_argument("--target-events", type=int, default=1_500_000,
                    help="approximate number of events to keep (default 1.5M)")
    ap.add_argument("--keep-fraction", type=float, default=None,
                    help="override auto-calibration: fraction of USERS to keep, e.g. 0.02")
    args = ap.parse_args()

    if not args.csv.exists():
        sys.exit(f"file not found: {args.csv}")

    if args.keep_fraction is not None:
        buckets = max(1, round(args.keep_fraction * HASH_SPACE))
        print(f"[manual] keeping {buckets / HASH_SPACE:.2%} of users")
    else:
        buckets = calibrate_keep_buckets(args.csv, args.target_events)

    load(args.csv, buckets)


if __name__ == "__main__":
    main()

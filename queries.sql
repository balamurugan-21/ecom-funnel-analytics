-- ============================================================================
-- queries.sql — ALL extraction & aggregation for the funnel dashboard.
--
-- Architecture rule: every number shown in the app comes from one of these
-- queries. Python/pandas only receives the (small) result sets and performs
-- statistical inference + rendering. No aggregation happens in pandas.
--
-- Format: each query starts with a "-- name: <query_name>" line. app.py
-- parses this file into a {name: sql} dictionary and displays the exact SQL
-- in an expander on every dashboard view.
--
-- Schema (built by load_data.py / the demo generator):
--   events(event_time TEXT, event_ts INTEGER, event_type TEXT,
--          product_id INTEGER, category_code TEXT, brand TEXT,
--          price REAL, user_id INTEGER, user_session TEXT)
--   event_ts is unix epoch seconds — precomputed at load time so window
--   functions can do plain integer arithmetic instead of date parsing.
-- ============================================================================


-- name: funnel_user
-- Plain English: For every USER, did they ever view, ever add to cart, and
-- ever purchase? Then count users at each funnel stage.
-- Notes on semantics:
--   * We use "ever did X" flags per user (MAX over a boolean). This is the
--     standard marketing-funnel definition: a user counts as reaching a
--     stage if they performed that action at least once in the window.
--   * Step conversion is conditional on the prior step: carters are counted
--     among viewers, purchasers among viewer-carters. Users who purchased
--     without a recorded view/cart exist in clickstream data (tracking gaps,
--     direct links) — they are surfaced separately in the data-quality query
--     rather than silently inflating the funnel.
WITH user_flags AS (
    SELECT
        user_id,
        MAX(event_type = 'view')     AS viewed,     -- 1 if user has >=1 view
        MAX(event_type = 'cart')     AS carted,     -- 1 if user has >=1 cart add
        MAX(event_type = 'purchase') AS purchased   -- 1 if user has >=1 purchase
    FROM events
    GROUP BY user_id
)
SELECT
    COUNT(*)                                          AS total_units,
    SUM(viewed)                                       AS step_view,
    SUM(viewed AND carted)                            AS step_cart,
    SUM(viewed AND carted AND purchased)              AS step_purchase
FROM user_flags;


-- name: funnel_session
-- Plain English: Same funnel, but the unit is a SESSION instead of a user.
-- A session only "converts" if the view, cart-add and purchase all happened
-- within that same visit. Session rates are therefore always lower than user
-- rates: a user who browses on Monday and buys on Friday converts as a user
-- but produces two non-converting-looking sessions plus one purchase session.
WITH session_flags AS (
    SELECT
        user_session,
        MAX(event_type = 'view')     AS viewed,
        MAX(event_type = 'cart')     AS carted,
        MAX(event_type = 'purchase') AS purchased
    FROM events
    GROUP BY user_session
)
SELECT
    COUNT(*)                             AS total_units,
    SUM(viewed)                          AS step_view,
    SUM(viewed AND carted)               AS step_cart,
    SUM(viewed AND carted AND purchased) AS step_purchase
FROM session_flags;


-- name: funnel_by_category
-- Plain English: The per-user funnel, split by TOP-LEVEL category. The first
-- dot-separated segment of category_code (e.g. 'electronics' out of
-- 'electronics.smartphone') is parsed in SQL with substr()/instr().
-- A user can appear in several categories (they browse shoes AND phones);
-- each (user, category) pair is an independent funnel unit, which is the
-- honest way to ask "how well does THIS category convert the people who
-- looked at it?". Only categories with >= 200 viewers are returned so the
-- Python significance tests aren't run on hopelessly small samples.
WITH tagged AS (
    SELECT
        user_id,
        event_type,
        CASE
            WHEN category_code IS NULL OR category_code = '' THEN '(unknown)'
            WHEN instr(category_code, '.') > 0
                THEN substr(category_code, 1, instr(category_code, '.') - 1)
            ELSE category_code
        END AS top_category
    FROM events
),
user_cat_flags AS (
    SELECT
        top_category,
        user_id,
        MAX(event_type = 'view')     AS viewed,
        MAX(event_type = 'cart')     AS carted,
        MAX(event_type = 'purchase') AS purchased
    FROM tagged
    GROUP BY top_category, user_id
)
SELECT
    top_category,
    SUM(viewed)                          AS step_view,
    SUM(viewed AND carted)               AS step_cart,
    SUM(viewed AND carted AND purchased) AS step_purchase
FROM user_cat_flags
GROUP BY top_category
HAVING SUM(viewed) >= 200
ORDER BY step_view DESC
LIMIT 10;


-- name: funnel_by_price_quartile
-- Plain English: Split every priced event into four equal-size buckets by
-- price using the NTILE(4) window function (Q1 = cheapest 25% of events,
-- Q4 = most expensive 25%), then run the per-user funnel inside each bucket.
-- NTILE ranks all rows by price and deals them into 4 near-equal groups —
-- this adapts to the actual price distribution instead of hard-coding
-- arbitrary price ranges. The min/max price per quartile is returned so the
-- UI can label buckets in real currency.
WITH priced AS (
    SELECT
        user_id,
        event_type,
        price,
        NTILE(4) OVER (ORDER BY price) AS price_quartile   -- the core window fn
    FROM events
    WHERE price IS NOT NULL AND price > 0
),
user_q_flags AS (
    SELECT
        price_quartile,
        user_id,
        MIN(price)                   AS min_price_seen,
        MAX(price)                   AS max_price_seen,
        MAX(event_type = 'view')     AS viewed,
        MAX(event_type = 'cart')     AS carted,
        MAX(event_type = 'purchase') AS purchased
    FROM priced
    GROUP BY price_quartile, user_id
)
SELECT
    price_quartile,
    MIN(min_price_seen)                  AS price_min,
    MAX(max_price_seen)                  AS price_max,
    SUM(viewed)                          AS step_view,
    SUM(viewed AND carted)               AS step_cart,
    SUM(viewed AND carted AND purchased) AS step_purchase
FROM user_q_flags
GROUP BY price_quartile
ORDER BY price_quartile;


-- name: events_per_session
-- Plain English: Sanity check on sessionization — how many events does a
-- typical session contain? A huge spike at 1-event sessions or absurdly long
-- sessions would suggest broken session IDs. Counts are bucketed in SQL.
WITH per_session AS (
    SELECT user_session, COUNT(*) AS n_events
    FROM events
    GROUP BY user_session
)
SELECT
    CASE
        WHEN n_events = 1  THEN '1'
        WHEN n_events = 2  THEN '2'
        WHEN n_events <= 5  THEN '3-5'
        WHEN n_events <= 10 THEN '6-10'
        WHEN n_events <= 20 THEN '11-20'
        ELSE '21+'
    END AS events_bucket,
    -- explicit sort key because the bucket labels don't sort alphabetically
    MIN(n_events)  AS sort_key,
    COUNT(*)       AS sessions
FROM per_session
GROUP BY events_bucket
ORDER BY sort_key;


-- name: session_duration
-- Plain English: Session length = last event time minus first event time
-- (MAX - MIN of the epoch timestamp within each user_session), bucketed.
-- Zero-duration sessions are usually single-event sessions — normal in
-- clickstream data, but worth seeing the share of.
WITH durations AS (
    SELECT
        user_session,
        MAX(event_ts) - MIN(event_ts) AS duration_sec
    FROM events
    GROUP BY user_session
)
SELECT
    CASE
        WHEN duration_sec = 0          THEN '0s (single event)'
        WHEN duration_sec < 60         THEN '< 1 min'
        WHEN duration_sec < 300        THEN '1-5 min'
        WHEN duration_sec < 1800       THEN '5-30 min'
        WHEN duration_sec < 7200       THEN '30 min - 2 h'
        ELSE '> 2 h (suspicious)'
    END AS duration_bucket,
    MIN(duration_sec) AS sort_key,
    COUNT(*)          AS sessions
FROM durations
GROUP BY duration_bucket
ORDER BY sort_key;


-- name: lag_event_gaps
-- Plain English: For each user, order their events by time and use LAG() to
-- fetch the timestamp of the PREVIOUS event on each row. The difference is
-- the "gap" between consecutive actions. Gap distribution tells you how the
-- 30-minute-style session boundaries in the data actually behave: small gaps
-- = same browsing burst, multi-hour gaps = the user came back later.
-- LAG(event_ts) OVER (PARTITION BY user_id ORDER BY event_ts) returns NULL
-- on each user's first event — those rows are excluded (no previous event).
WITH gaps AS (
    SELECT
        user_id,
        event_ts - LAG(event_ts) OVER (
            PARTITION BY user_id           -- restart the window per user
            ORDER BY event_ts, event_type  -- tiebreak equal timestamps
        ) AS gap_sec
    FROM events
)
SELECT
    CASE
        WHEN gap_sec = 0        THEN '0s (same second)'
        WHEN gap_sec < 60       THEN '< 1 min'
        WHEN gap_sec < 600      THEN '1-10 min'
        WHEN gap_sec < 1800     THEN '10-30 min'
        WHEN gap_sec < 86400    THEN '30 min - 24 h'
        ELSE '> 24 h (returned another day)'
    END AS gap_bucket,
    MIN(gap_sec) AS sort_key,
    COUNT(*)     AS gap_count
FROM gaps
WHERE gap_sec IS NOT NULL          -- first event per user has no previous row
GROUP BY gap_bucket
ORDER BY sort_key;


-- name: time_to_convert
-- Plain English: For users who both viewed and purchased (view first), how
-- long from FIRST view to FIRST purchase — and did it happen inside a single
-- session, the same day, or on a later return visit? "Same session" is
-- detected structurally: does any single user_session contain both a view
-- and a purchase for that user?
WITH firsts AS (
    SELECT
        user_id,
        MIN(CASE WHEN event_type = 'view'     THEN event_ts END) AS first_view_ts,
        MIN(CASE WHEN event_type = 'purchase' THEN event_ts END) AS first_purchase_ts
    FROM events
    GROUP BY user_id
),
same_session_converters AS (
    -- users with at least one session containing BOTH a view and a purchase
    SELECT DISTINCT user_id
    FROM (
        SELECT
            user_id,
            user_session,
            MAX(event_type = 'view')     AS v,
            MAX(event_type = 'purchase') AS p
        FROM events
        GROUP BY user_id, user_session
    )
    WHERE v = 1 AND p = 1
)
SELECT
    CASE
        WHEN f.user_id IN (SELECT user_id FROM same_session_converters)
            THEN 'same session'
        WHEN (f.first_purchase_ts - f.first_view_ts) < 86400
            THEN 'same day (different session)'
        ELSE 'later return (> 1 day)'
    END AS convert_bucket,
    COUNT(*)                                             AS users,
    ROUND(AVG(f.first_purchase_ts - f.first_view_ts) / 3600.0, 2)
                                                         AS avg_hours_to_convert
FROM firsts f
WHERE f.first_view_ts IS NOT NULL
  AND f.first_purchase_ts IS NOT NULL
  AND f.first_purchase_ts >= f.first_view_ts   -- exclude purchase-before-view anomalies
GROUP BY convert_bucket
ORDER BY users DESC;


-- name: time_to_convert_distribution
-- Plain English: Same converting users, but the raw first-view-to-first-
-- purchase delay bucketed on a finer time scale, for the histogram.
WITH firsts AS (
    SELECT
        user_id,
        MIN(CASE WHEN event_type = 'view'     THEN event_ts END) AS first_view_ts,
        MIN(CASE WHEN event_type = 'purchase' THEN event_ts END) AS first_purchase_ts
    FROM events
    GROUP BY user_id
)
SELECT
    CASE
        WHEN delay < 600      THEN '< 10 min'
        WHEN delay < 3600     THEN '10-60 min'
        WHEN delay < 86400    THEN '1-24 h'
        WHEN delay < 604800   THEN '1-7 days'
        ELSE '> 7 days'
    END AS delay_bucket,
    MIN(delay) AS sort_key,
    COUNT(*)   AS users
FROM (
    SELECT first_purchase_ts - first_view_ts AS delay
    FROM firsts
    WHERE first_view_ts IS NOT NULL
      AND first_purchase_ts IS NOT NULL
      AND first_purchase_ts >= first_view_ts
)
GROUP BY delay_bucket
ORDER BY sort_key;


-- name: abandonment_overall
-- Plain English: Cart abandonment at both grains in one result. A user (or
-- session) that added to cart but never purchased is "abandoned". Rate =
-- abandoned carters / all carters. Returned as two labeled rows.
WITH user_flags AS (
    SELECT user_id,
           MAX(event_type = 'cart')     AS carted,
           MAX(event_type = 'purchase') AS purchased
    FROM events GROUP BY user_id
),
session_flags AS (
    SELECT user_session,
           MAX(event_type = 'cart')     AS carted,
           MAX(event_type = 'purchase') AS purchased
    FROM events GROUP BY user_session
)
SELECT 'per user'    AS grain,
       SUM(carted)                    AS carters,
       SUM(carted AND NOT purchased)  AS abandoned
FROM user_flags
UNION ALL
SELECT 'per session' AS grain,
       SUM(carted),
       SUM(carted AND NOT purchased)
FROM session_flags;


-- name: abandonment_by_category
-- Plain English: Within each top-level category, of the users who added that
-- category's products to cart, how many never purchased in that category?
-- Categories with < 50 carters are dropped (rates on tiny denominators are
-- noise). Category parsing reuses the same substr/instr logic as the
-- category funnel.
WITH tagged AS (
    SELECT
        user_id,
        event_type,
        CASE
            WHEN category_code IS NULL OR category_code = '' THEN '(unknown)'
            WHEN instr(category_code, '.') > 0
                THEN substr(category_code, 1, instr(category_code, '.') - 1)
            ELSE category_code
        END AS top_category
    FROM events
),
user_cat AS (
    SELECT
        top_category,
        user_id,
        MAX(event_type = 'cart')     AS carted,
        MAX(event_type = 'purchase') AS purchased
    FROM tagged
    GROUP BY top_category, user_id
)
SELECT
    top_category,
    SUM(carted)                                   AS carters,
    SUM(carted AND NOT purchased)                 AS abandoned,
    ROUND(100.0 * SUM(carted AND NOT purchased) / SUM(carted), 1)
                                                  AS abandonment_pct
FROM user_cat
GROUP BY top_category
HAVING SUM(carted) >= 50
ORDER BY abandonment_pct DESC;


-- name: top_abandoned_products
-- Plain English: Products that get added to cart a lot but rarely bought —
-- classic candidates for shipping-cost, stock or price-shock problems.
-- Minimum 20 cart-adds so a product with 1 cart and 0 purchases doesn't
-- top the list. purchase_per_cart uses NULLIF as a divide-by-zero guard.
SELECT
    product_id,
    MAX(COALESCE(brand, '(no brand)'))          AS brand,
    MAX(COALESCE(category_code, '(unknown)'))   AS category_code,
    ROUND(AVG(price), 2)                        AS avg_price,
    SUM(event_type = 'cart')                    AS cart_adds,
    SUM(event_type = 'purchase')                AS purchases,
    ROUND(1.0 * SUM(event_type = 'purchase')
              / NULLIF(SUM(event_type = 'cart'), 0), 3)
                                                AS purchase_per_cart
FROM events
GROUP BY product_id
HAVING SUM(event_type = 'cart') >= 20
ORDER BY purchase_per_cart ASC, cart_adds DESC
LIMIT 15;


-- name: data_quality
-- Plain English: The checks an analyst runs BEFORE trusting any funnel:
-- how much of the data is missing labels, how much is duplicated, and how
-- many units violate funnel assumptions (purchases with no view). Each row
-- is one metric so the panel can render them as a table.
SELECT 'total events' AS metric,
       COUNT(*)       AS value,
       NULL           AS pct
FROM events
UNION ALL
SELECT 'events with NULL/empty category_code',
       SUM(category_code IS NULL OR category_code = ''),
       ROUND(100.0 * SUM(category_code IS NULL OR category_code = '') / COUNT(*), 2)
FROM events
UNION ALL
SELECT 'events with NULL/empty brand',
       SUM(brand IS NULL OR brand = ''),
       ROUND(100.0 * SUM(brand IS NULL OR brand = '') / COUNT(*), 2)
FROM events
UNION ALL
SELECT 'events with NULL or non-positive price',
       SUM(price IS NULL OR price <= 0),
       ROUND(100.0 * SUM(price IS NULL OR price <= 0) / COUNT(*), 2)
FROM events
UNION ALL
-- Exact duplicate events: identical on every identifying column. For each
-- duplicated combination appearing n times, (n - 1) rows are redundant.
SELECT 'duplicate events (redundant rows)',
       (SELECT COALESCE(SUM(cnt - 1), 0)
        FROM (SELECT COUNT(*) AS cnt
              FROM events
              GROUP BY user_id, user_session, event_ts, event_type, product_id
              HAVING COUNT(*) > 1)),
       ROUND(100.0 *
       (SELECT COALESCE(SUM(cnt - 1), 0)
        FROM (SELECT COUNT(*) AS cnt
              FROM events
              GROUP BY user_id, user_session, event_ts, event_type, product_id
              HAVING COUNT(*) > 1)) / COUNT(*), 2)
FROM events
UNION ALL
-- Sessions that contain a purchase but no view — tracking gaps or users
-- landing straight on checkout from an external link.
SELECT 'sessions with purchase but no view',
       (SELECT COUNT(*)
        FROM (SELECT user_session,
                     MAX(event_type = 'view')     AS v,
                     MAX(event_type = 'purchase') AS p
              FROM events GROUP BY user_session)
        WHERE p = 1 AND v = 0),
       NULL
FROM (SELECT 1)
UNION ALL
-- Users who purchased but have zero view events anywhere in the sample.
SELECT 'users with purchase but no view (ever)',
       (SELECT COUNT(*)
        FROM (SELECT user_id,
                     MAX(event_type = 'view')     AS v,
                     MAX(event_type = 'purchase') AS p
              FROM events GROUP BY user_id)
        WHERE p = 1 AND v = 0),
       NULL
FROM (SELECT 1);


-- name: dataset_summary
-- Plain English: Small header stats for the sidebar — events, users,
-- sessions, and the date range covered by the loaded sample.
SELECT
    COUNT(*)                            AS events,
    COUNT(DISTINCT user_id)             AS users,
    COUNT(DISTINCT user_session)        AS sessions,
    MIN(event_time)                     AS first_event,
    MAX(event_time)                     AS last_event
FROM events;

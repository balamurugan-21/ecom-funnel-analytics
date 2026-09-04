# E-commerce Conversion Funnel Analytics

**🔗 Live demo:** https://ecom-funnel-analytics.streamlit.app/
**💻 Source:** https://github.com/balamurugan-21/ecom-funnel-analytics

An interactive dashboard that traces how online shoppers move through the **view → add-to-cart → purchase** funnel, quantifies where they drop off, and tests which differences between segments are statistically real versus random noise. Built on ~400K real clickstream events, with a deliberate split: **SQL does every aggregation, Python does every statistical test.**

---

## The business question

> Where do shoppers fall out of the purchase funnel, which segment differences (by product category and price) are real rather than noise, how much revenue is lost to cart abandonment — and can we trust these numbers enough to act on them?

Every view in the dashboard answers a piece of this, and every number is traceable to the exact SQL query that produced it (shown in an expander on each tab).

## Why I built it

I wanted a project that shows both halves of a data role honestly: writing real SQL against a warehouse-style database (window functions, sessionization, funnel logic) **and** applying proper statistical inference on top of it (confidence intervals, hypothesis tests, multiple-comparison corrections) rather than eyeballing bar charts. So I drew a hard line down the middle of the architecture and kept each language doing what it's best at.

## Architecture: SQL aggregates, Python infers

The core design rule is a strict boundary:

- **SQLite (`queries.sql`) does ALL extraction and aggregation.** Every count, rate, and bucket on screen comes from a named, plain-English-commented query: the per-user and per-session funnels, the category split (parsed from `category_code` with `substr`/`instr`), price quartiles via the `NTILE(4)` window function, session durations via `MIN`/`MAX`, inter-event gaps via `LAG()`, time-to-convert bucketing, cart abandonment, and a data-quality audit.
- **Python (`app.py`) does ALL statistics and UI.** pandas only ever receives small query *result sets* — it never aggregates raw events. Python adds Wilson confidence intervals on every funnel step, an omnibus chi-square test across segments, per-segment two-proportion z-tests with a Bonferroni multiple-comparisons correction, and a plain-English verdict on which differences are real.

Why this split? It mirrors how production analytics actually works: the database does the heavy row-crunching once (cheap, indexed, reviewable SQL), and the application layer receives kilobytes instead of gigabytes and adds the judgment — inference, uncertainty, and presentation. It also makes both skills independently auditable — you can run every query in any SQLite client, and every statistical function is a small, commented, pure function.

## The data & sampling strategy

Source: Kaggle's [eCommerce behavior data from multi category store](https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store) — one row per event (`view` / `cart` / `purchase`), with product, category, brand, price, user, and session fields.

The raw monthly file is **9 GB** — far too big to query interactively or ship in a repo. So `load_data.py` streams it in chunks and keeps a random ~400K-event sample. The critical decision is that it **samples by user, never by row:**

- **Row sampling breaks funnels.** A journey is view → cart → purchase spread across many rows. Keeping every Nth row keeps the view but drops the purchase for most users, biasing every conversion rate downward in stage-dependent ways.
- **User sampling preserves funnels.** Keep a random subset of users but *all* of each kept user's events, and every retained journey is complete. Per-user conversion rates on the sample are unbiased estimates of the full-population rates, because it's literally a simple random sample of users. Sessions survive too, since each session belongs to one user.
- **Consistency across chunks** comes from a deterministic hash: a user is kept iff `md5(user_id) mod 10000 < k`. The same user hashes the same way in every chunk and every run, so no giant in-memory user set is needed while streaming.

The final sampled SQLite database is ~74 MB — small enough to commit to the repo so the deployed app shows real data.

## Headline findings

On the November 2019 sample (**403,325 events · 22,083 users · 82,795 sessions**):

- **The funnel is steep and front-loaded.** Of users who viewed a product, **22.6%** added to cart (95% CI 22.0–23.1%), and of those, **47.8%** went on to purchase (95% CI 46.4–49.1%). End-to-end, only **10.8%** of viewers became buyers — the biggest single drop-off is at the view→cart step, not at checkout.
- **Per-session conversion runs well below per-user conversion**, because a meaningful share of buyers return in a later session to complete the purchase (visible in the Timing tab's "later return" split). Per-user answers "do we eventually convert people?"; per-session answers "how good is a single visit at closing?"
- **Cart abandonment is the biggest single leak.** 52.3% of users who added to cart never purchased (2,603 of 4,981; 95% CI 50.9–53.6%), rising to 62.3% per session. It varies sharply by category: apparel abandons worst at 69.4%, then furniture (65.2%) and computers (64.8%), against 51.7% for electronics — the largest category by volume. A handful of high-cart, low-purchase products account for a disproportionate share (see the Abandonment tab).
- **Segment differences, tested properly: category matters enormously, price barely matters at all.** Across product categories the omnibus chi-square is overwhelming (χ² = 926.3, p ≈ 1e-193) and *all ten* categories survive Bonferroni correction — electronics converts view→cart at 22.6% against apparel's 5.1%, a four-fold spread that is a business difference and not merely a small p-value. Across price quartiles the same test is only marginal (χ² = 10.5, p = 0.015): just the third quartile ($167–370, 15.4%) survives correction, the fourth is borderline, and the bottom two show no evidence of a real difference. What people shop for predicts cart-adds; what it costs mostly doesn't.


## Limitations

- **Sampling variance:** all rates carry the confidence intervals shown; small segments are excluded below minimum-size thresholds rather than reported noisily.
- **Right-censoring:** users who viewed near the end of the observation window haven't had time to purchase yet, slightly deflating conversion.
- **Tracking gaps:** the data-quality panel quantifies purchases with no recorded view; true funnel rates are *at least* what's shown.
- **NULL categories:** 11,726 of 22,081 viewers (53%) carry no `category_code`, so category-level results describe the labelled minority only. The `(unknown)` bucket is reported alongside the named categories rather than hidden.
- **Bonferroni is conservative:** real differences in small segments may be flagged "borderline" — an intentional trade-off favoring caution.
- **No causality:** category/price differences are associations; product mix, seasonality, and traffic source are all confounded.

## Project layout

```
├── app.py            # Streamlit UI + all statistics (inference only)
├── queries.sql       # ALL aggregation — one named, commented query per view
├── load_data.py      # chunked 9 GB CSV → SQLite loader, samples by user
├── requirements.txt
├── data/
│   └── funnel.db     # sampled real data (~74 MB, committed so the live app has data)
└── README.md
```

## Run it locally

```bash
pip install -r requirements.txt
streamlit run app.py                 # runs on the built-in synthetic demo data

# to use real data:
# 1. download a month (e.g. 2019-Nov.csv) from the Kaggle dataset page
python load_data.py 2019-Nov.csv --target-events 400000
streamlit run app.py                 # now uses data/funnel.db
```

The app ships with a small synthetic dataset that generates on first run, so it renders immediately even before any real data is loaded.

## Deployment

Deployed on Streamlit Community Cloud, which clones this repo and runs `app.py` directly. Because the sampled `data/funnel.db` is committed (kept under 100 MB by tuning `--target-events` in the loader), the live app shows real data with no external setup. Pushing to `main` auto-redeploys.

## Tech stack

Python · SQLite · pandas · SciPy · Plotly · Streamlit

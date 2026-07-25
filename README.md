# E-commerce Conversion Funnel Analytics

A Streamlit dashboard that answers one business question with equal weight on SQL and Python:

> **Where do shoppers drop out of the view → cart → purchase funnel, which segment differences are statistically real, and how much of the cart is being abandoned — and can we trust these numbers?**

Built on the Kaggle [eCommerce behavior data from multi category store](https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store) clickstream dataset (one row per view/cart/purchase event).

## Architecture: SQL aggregates, Python infers

The core design rule of this project is a hard boundary:

- **SQLite (`queries.sql`) does ALL extraction and aggregation.** Every count, rate denominator and bucket on screen comes from a named query in `queries.sql`: the per-user and per-session funnels, the category split (parsed with `substr`/`instr`), price quartiles via `NTILE(4)`, session durations via `MIN`/`MAX`, inter-event gaps via `LAG()`, time-to-convert bucketing, abandonment, and the data-quality audit. Each query carries a plain-English comment, and the app shows the exact SQL in an expander on every view.
- **Python (`app.py`) does ALL statistics and UI.** pandas only ever receives small query *result sets* — it never aggregates raw events. Python contributes Wilson confidence intervals on every funnel step, an omnibus chi-square test across segments, per-segment two-proportion z-tests with a Bonferroni multiple-comparisons correction, and a plain-English verdict on which differences are real vs noise.

Why this split? It mirrors production analytics: the warehouse does the heavy row-crunching once (cheap, indexed, reviewable SQL), and the application layer receives kilobytes, not gigabytes, and adds the judgment — inference, uncertainty, and presentation. It also makes both skills independently auditable: you can run every query in any SQLite client, and every statistical function is a small commented pure function.

## Sampling strategy: by user, never by row

The raw monthly files are several GB, so `load_data.py` streams the CSV in 500k-row chunks and keeps a random ~1–2M-event sample. Critically, it samples **users**, not rows:

- **Row sampling breaks funnels.** A journey is view → cart → purchase spread over many rows. Keeping every Nth row keeps the view but drops the purchase for most users, biasing every conversion rate downward in stage-dependent ways.
- **User sampling preserves funnels.** Keep a random subset of users but *all* of each kept user's events, and every retained journey is complete. Per-user conversion rates on the sample are unbiased estimates of the full-population rates, because it is literally a simple random sample of users. Sessions survive too, since a session belongs to one user.
- **Consistency across chunks** comes from a deterministic hash: a user is kept iff `md5(user_id) mod 10000 < k`. The same user hashes the same way in every chunk and every run, so no giant in-memory user set is needed.

## Headline findings (demo dataset)

The repo ships with a small synthetic demo dataset (generated on first run) so the app renders immediately. On it — and directionally on the real data — the dashboard surfaces the classic patterns:

- Per-session conversion is far below per-user conversion, because a large share of buyers return in a later session (visible in the Timing tab's "later return" slice).
- Cheaper price quartiles convert meaningfully better than the most expensive quartile at the cart → purchase step, and the difference survives Bonferroni correction; several category gaps do not, and the app says so explicitly.
- Cart abandonment sits in the 40–70% range depending on category, and a handful of high-cart, low-purchase products account for a disproportionate share of abandoned carts.

Re-run the app on real data and replace this section with your actual numbers — the Segments tab's verdict panel writes the sentences for you.

## Limitations

- **Sampling variance:** all rates carry the CIs shown; small categories are excluded below minimum-size thresholds rather than reported noisily.
- **Observation window edges:** users who viewed near the end of the window haven't had time to purchase yet, deflating conversion slightly (right-censoring).
- **Tracking gaps:** the data-quality panel quantifies purchases with no recorded view; true funnel rates are at least what's shown.
- **NULL categories:** category-level results describe the *labelled* subset (~30% of events lack `category_code` in the real data).
- **Bonferroni is conservative:** real differences in small segments may be marked "borderline"; that's the intended trade-off.
- **No causality:** category/price differences are associations — product mix, seasonality and traffic source are all confounded.

## Project layout

```
├── app.py            # Streamlit UI + all statistics (inference only)
├── queries.sql       # ALL aggregation, one named + commented query per view
├── load_data.py      # chunked CSV → SQLite loader, samples by user
├── requirements.txt
└── data/
    ├── funnel.db     # produced by load_data.py (real sampled data)
    └── demo.db       # auto-generated synthetic demo (first app run)
```

## Run it locally

```bash
pip install -r requirements.txt
streamlit run app.py                 # runs on the built-in demo data

# with real data:
# 1. download 2019-Nov.csv (or any month) from the Kaggle dataset page
python load_data.py 2019-Nov.csv --target-events 1500000
streamlit run app.py                 # now uses data/funnel.db
```

## Deploy to Streamlit Community Cloud (beginner steps)

1. **Create a GitHub repo** and push this folder (`git init`, `git add .`, `git commit -m "funnel dashboard"`, create the repo on github.com, `git remote add origin …`, `git push -u origin main`).
2. **Ship the sampled database inside the repo** so the deployed app shows real data. GitHub rejects files over 100 MB, and Streamlit Cloud clones your repo, so: run `load_data.py` with `--target-events` low enough that `data/funnel.db` stays under ~90 MB (≈1–2M events fits comfortably; the loader prints the final size and warns if you're close). Commit `data/funnel.db`. If you skip this, the app deploys fine on the demo data.
3. Go to **share.streamlit.io**, sign in with GitHub, click **"Create app"**.
4. Pick your repo, branch `main`, main file path `app.py`, and click **Deploy**.
5. Streamlit Cloud installs `requirements.txt` automatically. First boot takes a couple of minutes; after that you get a public URL to put on your resume.
6. To update, just `git push` — the app redeploys itself.

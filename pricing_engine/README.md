# HSMI Dynamic Pricing Engine

Automatically calculates and pushes optimal nightly room rates for **Hepburn Springs Motor Inn** to Cloudbeds, based on occupancy, day-of-week, and Victorian public/school holiday calendars.

---

## What it does

Each run (scheduled daily at 6 am AEST):

1. Fetches room types and the active BAR/standard rate plan from Cloudbeds.
2. Pulls all reservations for the next 60 days and computes nightly occupancy per room type.
3. Applies a tiered pricing ruleset (see below) to produce a target rate for each room type × date.
4. Fetches current live rates from Cloudbeds.
5. Pushes updates only where the difference exceeds **$5** (to avoid noisy micro-changes).
6. Posts a summary of changes to Slack.

---

## Room type pricing tiers (AUD)

| Code | Floor | Midweek | Weekend | Peak  | Ceiling |
|------|------:|--------:|--------:|------:|--------:|
| TWI  |  $105 |    $160 |    $190 |  $225 |    $260 |
| QUE  |  $140 |    $185 |    $220 |  $260 |    $280 |
| SPA  |  $160 |    $195 |    $235 |  $275 |    $310 |
| FAM  |  $150 |    $220 |    $265 |  $310 |    $435 |
| BAL  |  $175 |    $195 |    $235 |  $275 |    $260 |
| ACC  |  $175 |    $205 |    $245 |  $285 |    $240 |

> **Note:** BAL and ACC have ceiling values below their peak tier. The ceiling is always enforced as the hard upper limit regardless of which rule fires.

---

## Pricing rules (priority order, highest first)

1. **Peak date** (public holiday or school holiday period): use `peak` rate. Min 2-night stay is logged as a recommendation.
2. **Occupancy > 90%**: raise 25% above the applicable base rate (midweek or weekend). Min 2-night stay logged.
3. **Weekend (Fri/Sat/Sun)**:
   - < 7 days out and occupancy > 80% → +20% above weekend rate
   - ≥ 7 days out and occupancy > 70% → +10% above weekend rate
   - Otherwise → weekend rate
4. **Midweek (Mon–Thu)**:
   - < 7 days out and occupancy < 25% → −15% below midweek rate (floor enforced)
   - 7–13 days out and occupancy < 30% → −10% below midweek rate (floor enforced)
   - ≥ 14 days out and occupancy < 40% → midweek rate (no discount)
   - Otherwise → midweek rate

All computed rates are **clamped to [floor, ceiling]** after applying modifiers.

---

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `CLOUDBEDS_API_KEY` | Yes | Cloudbeds x-api-key credential |
| `CLOUDBEDS_PROPERTY_ID` | Yes | Cloudbeds property ID |
| `SLACK_WEBHOOK_URL` | No | Slack incoming webhook URL for the daily summary |
| `ANTHROPIC_API_KEY` | No | Reserved for future AI-assisted pricing |

### config.py

Edit `/Users/jordan/hsmi-intelligence/pricing_engine/config.py` to adjust:
- `ROOM_TYPES` — floor/midweek/weekend/peak/ceiling per room type
- `LOOKAHEAD_DAYS` — how far ahead to price (default: 60)
- `RATE_CHANGE_THRESHOLD` — minimum $-change before pushing to Cloudbeds (default: $5)

---

## Running locally

```bash
# From repo root
pip install -r requirements.txt

export CLOUDBEDS_API_KEY=your_key
export CLOUDBEDS_PROPERTY_ID=your_property_id
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional

python pricing_engine/pricing_engine.py
```

Logs are written to stdout in the format `YYYY-MM-DDTHH:MM:SS LEVEL message`.

---

## GitHub Actions schedule

The workflow at `.github/workflows/pricing_engine.yml` runs daily at **8:00 pm UTC (6:00 am AEST)** via:

```yaml
on:
  schedule:
    - cron: '0 20 * * *'
  workflow_dispatch:  # allows manual trigger from GitHub UI
```

Secrets required in the GitHub repo settings:
- `CLOUDBEDS_API_KEY`
- `CLOUDBEDS_PROPERTY_ID`
- `SLACK_WEBHOOK_URL`
- `ANTHROPIC_API_KEY` (optional)

---

## Adding future holidays

### Public holidays

Open `pricing_engine/holidays.py` and add entries to `VICTORIAN_PUBLIC_HOLIDAYS`:

```python
date(2028, 1, 1),   # New Year's Day 2028
date(2028, 1, 26),  # Australia Day 2028
# ...
```

### School holidays

Add tuples to `SCHOOL_HOLIDAY_PERIODS` (both dates inclusive):

```python
(date(2027, 4, 3), date(2027, 4, 16)),  # Autumn 2027
```

Victoria's Department of Education publishes school term dates at:
https://www.education.vic.gov.au/about/department/Pages/termsdates.aspx

# 2-Bedroom Apartment Watcher

This script opens the MRI Prospect Connect availability page, selects **2** beds, searches, extracts visible unit cards/rows, ranks the units by:

1. latest available date first
2. if dates tie, lowest monthly rent first

It prints the top 5 and emails you only when the top 5 changes or on the first run.

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Configure email

```bash
cp .env.example .env
```

Edit `.env`. For Gmail, create an app password and use it as `SMTP_PASSWORD`.

## Test once with the browser visible

```bash
python watcher.py --once --show-browser --slow-mo 200
```

## Run continuously

```bash
python watcher.py
```

Default polling interval is 30 minutes. You can override it:

```bash
python watcher.py --interval-minutes 60
```

## Notes

The target site is dynamically rendered, so this uses Playwright rather than a static HTTP scraper. If MRI changes its page markup, the fallback extractor may need small selector adjustments in `extract_candidate_texts()` or `choose_beds()`.

#!/usr/bin/env python3
"""
Watcher for 2-bedroom units on MRI Prospect Connect.

Behavior:
  1. Opens the property availability page in Chromium via Playwright.
  2. Selects 2 beds and runs Search.
  3. Extracts unit cards/rows from the dynamic page.
  4. Ranks by latest available date, then lowest rent for ties.
  5. Prints the top 5.
  6. Persists the previous top 5 and emails you when the top 5 changes.

Install:
  python -m pip install -r requirements.txt
  python -m playwright install chromium

Run:
  cp .env.example .env
  # edit .env
  python cvcctr_watcher.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import smtplib
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any, Iterable

from dateutil import parser as date_parser
from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

URL = "https://cpi.mriprospectconnect.com/Search/Index/CVCCTR"
STATE_PATH_DEFAULT = Path("cvcctr_state.json")

PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)")
DATE_RE = re.compile(
    r"(?P<date>"
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"
    r"|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:,?\s+\d{4})?\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Unit:
    unit_id: str
    available_date: str  # ISO yyyy-mm-dd
    price: int
    summary: str
    raw_text: str

    @property
    def available_date_obj(self) -> date:
        return date.fromisoformat(self.available_date)


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc


def parse_price(text: str) -> int | None:
    """Return a plausible monthly rent in whole dollars."""
    # Prefer prices near rent-ish labels when available.
    rentish = re.search(
        r"(?:rent|price|monthly|market rent)\D{0,40}\$\s*([0-9][0-9,]*(?:\.\d{2})?)",
        text,
        flags=re.IGNORECASE,
    )
    if rentish:
        return int(float(rentish.group(1).replace(",", "")))

    prices = [int(float(m.group(1).replace(",", ""))) for m in PRICE_RE.finditer(text)]
    # Avoid accidentally using application/security fees if they appear in the same card.
    plausible_rents = [p for p in prices if p >= 1000]
    if plausible_rents:
        return plausible_rents[0]
    return prices[0] if prices else None


def parse_available_date(text: str, today: date | None = None) -> str | None:
    today = today or date.today()
    lower = text.lower()

    if re.search(r"\b(?:available|availability|avail|move[ -]?in)\b.{0,40}\b(?:now|today|immediate)\b", lower):
        return today.isoformat()
    if re.search(r"\b(?:now|immediate)\b", lower) and re.search(r"\b(?:available|availability|avail)\b", lower):
        return today.isoformat()

    # Prefer a date that appears near availability/move-in language.
    preferred = re.search(
        r"(?:available|availability|avail|move[ -]?in|date)\D{0,80}"
        + DATE_RE.pattern,
        text,
        flags=re.IGNORECASE,
    )
    candidates = []
    if preferred:
        candidates.append(preferred.group("date"))
    candidates.extend(m.group("date") for m in DATE_RE.finditer(text))

    for candidate in candidates:
        try:
            parsed = date_parser.parse(
                candidate,
                fuzzy=True,
                default=datetime(today.year, today.month, today.day),
            ).date()
        except (ValueError, OverflowError):
            continue

        # If the site gives month/day without a year and that date already passed,
        # interpret it as next year. Keep a 30-day grace window for stale pages/timezones.
        if parsed < today - timedelta(days=30):
            parsed = date(parsed.year + 1, parsed.month, parsed.day)
        return parsed.isoformat()

    return None


def parse_unit_id(text: str) -> str:
    patterns = [
        r"\bUnit\s*#?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-_.]*)",
        r"\bApartment\s*#?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-_.]*)",
        r"\bApt\s*#?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-_.]*)",
    ]
    for pat in patterns:
        match = re.search(pat, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    # Fallback: compact, stable ID from the card text.
    return "unknown-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def summarize(text: str, max_len: int = 180) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[: max_len - 1] + "…" if len(clean) > max_len else clean


def parse_units(candidate_texts: Iterable[str]) -> list[Unit]:
    units: list[Unit] = []
    seen: set[tuple[str, str, int]] = set()
    today = date.today()

    for raw in candidate_texts:
        text = re.sub(r"\s+", " ", raw).strip()
        if len(text) < 15:
            continue

        price = parse_price(text)
        available = parse_available_date(text, today=today)
        if price is None or available is None:
            continue

        unit_id = parse_unit_id(text)
        key = (unit_id, available, price)
        if key in seen:
            continue
        seen.add(key)

        units.append(
            Unit(
                unit_id=unit_id,
                available_date=available,
                price=price,
                summary=summarize(text),
                raw_text=raw,
            )
        )

    return units


def rank_units(units: list[Unit], limit: int = 5) -> list[Unit]:
    # Latest availability date first. If same date, lowest price first.
    return sorted(units, key=lambda u: (-u.available_date_obj.toordinal(), u.price, u.unit_id))[:limit]


def top_signature(units: list[Unit]) -> list[dict[str, Any]]:
    return [
        {"unit_id": u.unit_id, "available_date": u.available_date, "price": u.price}
        for u in units
    ]


def load_previous_state(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("top_signature")


def save_state(path: Path, top: list[Unit]) -> None:
    payload = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "top_signature": top_signature(top),
        "top_units": [asdict(u) for u in top],
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def render_text_report(units: list[Unit]) -> str:
    if not units:
        return "No matching units were parsed.\n"

    lines = ["Top 5 two-bedroom units", ""]
    for i, u in enumerate(units, start=1):
        lines.append(
            f"{i}. Unit {u.unit_id} | Available {u.available_date} | ${u.price:,}/mo"
        )
        lines.append(f"   {u.summary}")
    lines.append("")
    lines.append(f"Source: {URL}")
    return "\n".join(lines)


def render_html_report(units: list[Unit]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{i}</td>"
        f"<td>{escape(u.unit_id)}</td>"
        f"<td>{escape(u.available_date)}</td>"
        f"<td>${u.price:,}/mo</td>"
        f"<td>{escape(u.summary)}</td>"
        "</tr>"
        for i, u in enumerate(units, start=1)
    )
    return f"""
    <html><body>
      <p>The top 5 two-bedroom results changed.</p>
      <table border="1" cellpadding="6" cellspacing="0">
        <thead>
          <tr><th>#</th><th>Unit</th><th>Available</th><th>Rent</th><th>Summary</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p><a href="{escape(URL)}">Open listing page</a></p>
    </body></html>
    """


def send_email(top: list[Unit], subject_prefix: str = "CVCCTR watcher") -> None:
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Email is not configured. Missing environment variables: " + ", ".join(missing)
        )

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = getenv_int("SMTP_PORT", 587)
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_to = os.environ["EMAIL_TO"]
    email_from = os.getenv("EMAIL_FROM", smtp_user)

    msg = EmailMessage()
    msg["Subject"] = f"{subject_prefix}: top 5 two-bedroom units changed"
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(render_text_report(top))
    msg.add_alternative(render_html_report(top), subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


async def dismiss_cookie_banner(page) -> None:
    for label in ["I Understand", "Accept", "Accept All", "Agree"]:
        try:
            button = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            if await button.count():
                await button.first.click(timeout=2500)
                await page.wait_for_timeout(500)
                return
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logging.debug("Cookie banner dismissal failed for %s", label, exc_info=True)


async def choose_beds(page, beds: int = 2) -> None:
    beds_str = str(beds)

    # 1) Native/select-style controls.
    select_selectors = [
        'select[name*="bed"]',
        'select[name*="Bed"]',
        'select[id*="bed"]',
        'select[id*="Bed"]',
        'select[aria-label*="bed"]',
        'select[aria-label*="Bed"]',
    ]
    for selector in select_selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count():
                for option in [beds_str, {"label": beds_str}, {"value": beds_str}]:
                    try:
                        await loc.select_option(option, timeout=2500)
                        return
                    except Exception:
                        continue
        except Exception:
            logging.debug("Select-based bed chooser failed for %s", selector, exc_info=True)

    # 2) Radio/checkbox controls with value=2.
    input_selectors = [
        f'input[type="radio"][value="{beds_str}"]',
        f'input[type="checkbox"][value="{beds_str}"]',
        f'input[value="{beds_str}"][name*="bed"]',
        f'input[value="{beds_str}"][name*="Bed"]',
    ]
    for selector in input_selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count():
                await loc.click(timeout=2500, force=True)
                return
        except Exception:
            logging.debug("Input-based bed chooser failed for %s", selector, exc_info=True)

    # 3) Button/text fallback: click the exact visible "2" closest to a visible "Beds" label.
    clicked = await page.evaluate(
        """
        (beds) => {
          const target = String(beds).trim();
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' &&
                   rect.width > 0 && rect.height > 0;
          };
          const textOf = (el) => (el.innerText || el.textContent || el.value || '').trim();
          const bedLabels = Array.from(document.querySelectorAll('label,span,div,p,strong'))
            .filter(el => isVisible(el) && /^beds?$/i.test(textOf(el)));
          const label = bedLabels[0] || null;
          const distance = (a, b) => {
            if (!a) return 0;
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return Math.hypot(ar.left - br.left, ar.top - br.top);
          };
          const candidates = Array.from(document.querySelectorAll('button,a,label,input,span,div'))
            .filter(el => isVisible(el) && textOf(el) === target)
            .sort((a, b) => distance(label, a) - distance(label, b));
          for (const el of candidates) {
            el.click();
            return true;
          }
          return false;
        }
        """,
        beds,
    )
    if not clicked:
        raise RuntimeError("Could not find a Beds control to select 2 bedrooms.")


async def click_search(page) -> None:
    # Prefer an accessible Search button. Fall back to text if the role is not exposed.
    candidates = [
        page.get_by_role("button", name=re.compile(r"^Search$", re.I)),
        page.locator('button:has-text("Search")'),
        page.locator('input[type="submit"][value*="Search"]'),
        page.get_by_text("Search", exact=True),
    ]
    for loc in candidates:
        try:
            if await loc.count():
                await loc.first.click(timeout=5000)
                return
        except Exception:
            logging.debug("Search click candidate failed", exc_info=True)
    raise RuntimeError("Could not find the Search button.")


async def extract_candidate_texts(page) -> list[str]:
    """Extract unit rows from the MRI Prospect Connect results page.

    The CVCCTR result markup stores the important fields in stable attributes on
    each ``tr.pc-row-unit`` row. Using those attributes is more reliable than
    trying to infer unit/date/rent from all visible page text.
    """
    return await page.evaluate(
        r"""
        () => {
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const textFrom = (root, selector) => clean(root.querySelector(selector)?.innerText || root.querySelector(selector)?.textContent || '');

          const structuredRows = Array.from(document.querySelectorAll('tr.pc-row-unit'));
          const structured = structuredRows.map((row) => {
            const selectButton = row.querySelector('[data-unitid], button.select');
            const rentEl = row.querySelector('[data-rent-range], .pc-search-rent-range');
            const card = row.closest('.pc-card');

            const unit = clean(selectButton?.getAttribute('data-unitid')) || textFrom(row, '[data-th="Unit"]');
            const building = clean(selectButton?.getAttribute('data-bldgid')) || textFrom(row, '[data-th="Building"]');
            const availableText = textFrom(row, '[data-th="Available"]');
            const availableIso = clean(selectButton?.getAttribute('data-available-date'));
            const rent = clean(rentEl?.getAttribute('data-rent-range')) || clean(rentEl?.getAttribute('title')) || textFrom(row, '[data-th="Rent Range"]');
            const sqft = textFrom(row, '[data-th="Sqft"]');
            const amenities = textFrom(row, '[data-th="Amenities"]');
            const floorplan = textFrom(card || row, '.pc-card-title').replace(/\b\d+\s+available\b/i, '').trim();
            const address = clean(selectButton?.getAttribute('data-unit-address'));

            if (!unit || !rent || !(availableIso || availableText)) return '';

            return [
              `Unit ${unit}`,
              floorplan ? `Floorplan ${floorplan}` : '',
              building ? `Building ${building}` : '',
              address ? `Address ${address}` : '',
              sqft ? `Sqft ${sqft}` : '',
              `Available ${availableText || availableIso}`,
              `Rent $${rent}`,
              amenities ? `Amenities ${amenities}` : '',
            ].filter(Boolean).join(' | ');
          }).filter(Boolean);

          if (structured.length > 0) return [...new Set(structured)];

          // Generic fallback for a future site redesign. Avoid a JS regex literal
          // with an unescaped / inside a character class; that caused the
          // original "Invalid regular expression: missing /" error.
          const hasUsefulText = (text) => {
            if (!text || text.length < 20 || text.length > 5000) return false;
            const hasPrice = /[$]?\s*[0-9][0-9,]*\.\d{2}/.test(text);
            const hasAvailability = /(available|availability|avail|move[ -]?in|date|today|immediate|\d{1,2}[\/-]\d{1,2})/i.test(text);
            return hasPrice && hasAvailability;
          };
          const selectors = [
            'table tbody tr',
            '[role="row"]',
            'li',
            'article',
            '[class*="card"]', '[class*="Card"]',
            '[class*="unit"]', '[class*="Unit"]',
            '[class*="result"]', '[class*="Result"]',
            '[class*="apartment"]', '[class*="Apartment"]',
            '[class*="floor"]', '[class*="Floor"]'
          ];
          const out = [];
          for (const selector of selectors) {
            for (const el of Array.from(document.querySelectorAll(selector))) {
              const text = clean(el.innerText || el.textContent || '');
              if (hasUsefulText(text)) out.push(text);
            }
          }
          if (out.length === 0) {
            const body = (document.body.innerText || '').trim();
            for (const block of body.split(/\n\s*\n/)) {
              const text = clean(block);
              if (hasUsefulText(text)) out.push(text);
            }
          }
          return [...new Set(out)];
        }
        """
    )

async def fetch_top_units(headless: bool = True, slow_mo_ms: int = 0) -> list[Unit]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await dismiss_cookie_banner(page)
            await choose_beds(page, beds=2)
            await click_search(page)

            # Give the dynamic results grid/cards time to render.
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(5000)

            candidates = await extract_candidate_texts(page)
            units = parse_units(candidates)
            return rank_units(units, limit=5)
        finally:
            await browser.close()


async def poll_forever(args: argparse.Namespace) -> None:
    while True:
        try:
            top = await fetch_top_units(headless=not args.show_browser, slow_mo_ms=args.slow_mo)
            print("\n" + render_text_report(top), flush=True)

            previous = load_previous_state(args.state_path)
            current = top_signature(top)

            changed = previous != current
            first_run = previous is None
            if changed:
                send_email(top)
                logging.info("Email sent: top results %s.", "initialized" if first_run else "changed")
            else:
                logging.info("No top-result change detected.")

            save_state(args.state_path, top)
        except Exception as exc:
            logging.exception("Watcher cycle failed: %s", exc)
            if args.once:
                raise

        if args.once:
            return
        await asyncio.sleep(args.interval_minutes * 60)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch CVCCTR two-bedroom apartment availability.")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=getenv_int("POLL_MINUTES", 30),
        help="Polling interval. Default: POLL_MINUTES env var or 30.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(os.getenv("STATE_PATH", str(STATE_PATH_DEFAULT))),
        help="Path for the JSON state file.",
    )
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show Chromium. Useful for first-time debugging selector behavior.",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Milliseconds to slow each Playwright action; useful with --show-browser.",
    )
    return parser


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_arg_parser().parse_args()
    if args.interval_minutes < 5 and not args.once:
        raise ValueError("Use an interval of at least 5 minutes to avoid hammering the site.")
    asyncio.run(poll_forever(args))


if __name__ == "__main__":
    main()

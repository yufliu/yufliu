"""Airbnb price scraper using Playwright.

Modern Airbnb listing pages load prices via a client-side GraphQL call
(StaysPdpSections) after initial HTML render. The static HTML returns
`showPriceBreakdown: False` and `structuredDisplayPrice: None`, so we
need a real browser to execute the JS and capture the price response.

Strategy:
  1. Launch headless Chromium.
  2. Navigate to /rooms/{id}?check_in=...&check_out=...
  3. Listen on `response` events for any URL containing StaysPdpSections.
  4. Walk the captured GraphQL JSON for a price-breakdown section.
  5. Fall back to scraping the rendered DOM if no JSON match.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)


@dataclass
class PriceBreakdown:
    total: float                              # primary total — after taxes if we got the checkout, else before
    currency: str
    nights: int
    line_items: list[tuple[str, float]] = field(default_factory=list)
    final_url: str = ""
    total_qualifier: str = ""                 # e.g. "before taxes" when it excludes taxes
    total_before_taxes: float | None = None   # set when both listing + checkout were captured


class ScrapeError(RuntimeError):
    pass


def fetch_price_breakdown(
    listing_url: str,
    checkin: date,
    checkout: date,
    *,
    debug_dump_path: str | None = None,
    headless: bool = True,
    timeout_ms: int = 45_000,
) -> PriceBreakdown:
    nights = (checkout - checkin).days
    if nights <= 0:
        raise ValueError("checkout must be after checkin")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ScrapeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from e

    captured_listing: list[dict[str, Any]] = []
    captured_checkout: list[dict[str, Any]] = []
    phase = ["listing"]
    final_url = ""
    html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        def on_response(response):
            url = response.url
            if "/api/v3/" not in url and "/api/v2/" not in url:
                return
            try:
                body = response.json()
            except Exception:
                return
            bucket = captured_checkout if phase[0] == "checkout" else captured_listing
            bucket.append({"url": url, "body": body})

        page.on("response", on_response)

        target = _build_target_url(listing_url, checkin, checkout)
        try:
            # Phase 1: listing page — gives us before-tax pricing
            page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            final_url = page.url
            try:
                page.wait_for_selector(
                    '[data-section-id="BOOK_IT_SIDEBAR"], '
                    '[data-section-id="BOOK_IT_FLOATING_FOOTER"]',
                    timeout=15_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(6_000)

            # Phase 2: checkout flow — gives us after-tax pricing
            m = re.search(r"/rooms/(?:plus/)?(\d+)", final_url)
            if m:
                phase[0] = "checkout"
                listing_id = m.group(1)
                book_url = (
                    f"https://www.airbnb.com/book/stays/{listing_id}"
                    f"?numberOfAdults=1"
                    f"&checkin={checkin.isoformat()}"
                    f"&checkout={checkout.isoformat()}"
                )
                try:
                    page.goto(book_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(8_000)
                except Exception:
                    pass

            html = page.content()
        finally:
            browser.close()

    if debug_dump_path:
        Path(debug_dump_path).write_text(html)
        Path(debug_dump_path + ".graphql.json").write_text(
            json.dumps(
                {"listing": captured_listing, "checkout": captured_checkout},
                indent=2, default=str,
            )
        )

    listing_br = _first_match(captured_listing, nights)
    checkout_br = _first_match(captured_checkout, nights)

    if checkout_br is not None:
        if listing_br is not None and abs(listing_br.total - checkout_br.total) > 0.01:
            checkout_br.total_before_taxes = listing_br.total
        checkout_br.total_qualifier = ""
        checkout_br.final_url = final_url or target
        return checkout_br

    if listing_br is not None:
        listing_br.final_url = final_url or target
        return listing_br

    try:
        br = _scrape_dom(html, nights)
        br.final_url = final_url or target
        return br
    except ScrapeError as e:
        captured_total = len(captured_listing) + len(captured_checkout)
        msg = str(e)
        if captured_total:
            msg += (
                f" (captured {len(captured_listing)} listing + "
                f"{len(captured_checkout)} checkout GraphQL responses, "
                f"none had prices)"
            )
        else:
            msg += " (no GraphQL responses captured)"
        raise ScrapeError(msg) from None


def _first_match(entries: list[dict[str, Any]], nights: int) -> PriceBreakdown | None:
    for entry in entries:
        try:
            return _walk_for_price(entry["body"], nights)
        except ScrapeError:
            continue
    return None


def _build_target_url(listing_url: str, checkin: date, checkout: date) -> str:
    base = listing_url
    if not base.startswith("http"):
        base = "https://" + base
    sep = "&" if "?" in base else "?"
    return (
        f"{base}{sep}check_in={checkin.isoformat()}"
        f"&check_out={checkout.isoformat()}"
        f"&adults=1&numberOfGuests=1"
    )


def _walk_for_price(state: Any, nights: int) -> PriceBreakdown:
    """Find a price breakdown in an Airbnb GraphQL response.

    Modern shape (2025+):
      structuredDisplayPrice.explanationData.priceDetails[].items[]
      where each item has description / priceString / accessibilityLabel.
      Total is identified by `accessibilityLabel` containing "total"
      (e.g. "$1,370.72 total before taxes").
    Falls back to structuredDisplayPrice.primaryLine.discountedPrice
    when no explicit total line is present.
    """
    candidates: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            sdp = node.get("structuredDisplayPrice")
            if isinstance(sdp, dict):
                ed = sdp.get("explanationData")
                if isinstance(ed, dict) and ed.get("priceDetails"):
                    candidates.append(sdp)
                elif sdp.get("primaryLine"):
                    candidates.append(sdp)
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(state)

    for sdp in candidates:
        try:
            return _build_breakdown(sdp, nights)
        except ScrapeError:
            continue

    raise ScrapeError("No structuredDisplayPrice with a usable total found")


def _build_breakdown(sdp: dict[str, Any], nights: int) -> PriceBreakdown:
    line_items: list[tuple[str, float]] = []
    total: float | None = None
    total_qualifier = ""
    currency = "USD"

    pd = ((sdp.get("explanationData") or {}).get("priceDetails")) or []
    for group in pd:
        for item in group.get("items") or []:
            price_str = (item.get("priceString") or "").strip()
            if not price_str:
                continue
            amount = _parse_amount(price_str)
            if amount is None:
                continue
            if price_str.lstrip().startswith("-"):
                amount = -abs(amount)
            currency = _parse_currency(price_str) or currency
            desc = (item.get("description") or "").strip()
            acc = (item.get("accessibilityLabel") or "").strip()
            acc_lower = acc.lower()
            if total is None and "total" in acc_lower:
                total = amount
                # Pull qualifier text after "total" — e.g. "before taxes"
                m = re.search(r"total\s+(.+)$", acc_lower)
                total_qualifier = m.group(1).strip() if m else ""
                continue
            if not desc:
                desc = "Discount" if amount < 0 else "(unlabeled)"
            line_items.append((desc, amount))

    if total is None:
        primary = sdp.get("primaryLine") or {}
        for key in ("discountedPrice", "price", "originalPrice"):
            v = primary.get(key)
            parsed = _parse_amount(v) if v else None
            if parsed is not None:
                total = parsed
                currency = _parse_currency(v) or currency
                break

    if total is None:
        raise ScrapeError("structuredDisplayPrice present but no total resolved")

    return PriceBreakdown(
        total=total, currency=currency, nights=nights, line_items=line_items,
        total_qualifier=total_qualifier,
    )


_TOTAL_DOM_RE = re.compile(
    r"(?:Total(?:\s+before\s+taxes)?|Total)\s*[\s\S]{0,40}?\$([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LINE_DOM_RE = re.compile(
    r"<[^>]+>([^<]+?)</[^>]+>\s*<[^>]+>\$([\d,]+(?:\.\d+)?)</[^>]+>",
)


def _scrape_dom(html: str, nights: int) -> PriceBreakdown:
    """Last-resort: pull the total from the rendered DOM."""
    m = _TOTAL_DOM_RE.search(html)
    if not m:
        raise ScrapeError("Could not locate a Total in the rendered DOM either")
    total = _parse_amount(m.group(1)) or 0.0
    return PriceBreakdown(
        total=total, currency="USD", nights=nights,
        line_items=[("(DOM scrape — line items unavailable)", total)],
    )


_AMOUNT_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}


def _parse_amount(s: str) -> float | None:
    if not s:
        return None
    m = _AMOUNT_RE.search(s)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _parse_currency(s: str) -> str | None:
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in s:
            return code
    return None

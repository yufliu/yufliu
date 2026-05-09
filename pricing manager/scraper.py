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
    total: float
    currency: str
    nights: int
    line_items: list[tuple[str, float]] = field(default_factory=list)
    final_url: str = ""


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

    captured: list[dict[str, Any]] = []
    final_url = ""

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
            if "StaysPdpSections" not in url and "PdpStays" not in url:
                return
            try:
                body = response.json()
            except Exception:
                return
            captured.append({"url": url, "body": body})

        page.on("response", on_response)

        target = _build_target_url(listing_url, checkin, checkout)
        try:
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
            page.wait_for_timeout(3_000)

            html = page.content()
        finally:
            browser.close()

    if debug_dump_path:
        Path(debug_dump_path).write_text(html)
        Path(debug_dump_path + ".graphql.json").write_text(
            json.dumps(captured, indent=2, default=str)
        )

    for entry in captured:
        try:
            br = _walk_for_price(entry["body"], nights)
            br.final_url = final_url or target
            return br
        except ScrapeError:
            continue

    try:
        br = _scrape_dom(html, nights)
        br.final_url = final_url or target
        return br
    except ScrapeError as e:
        msg = str(e)
        if captured:
            msg += f" (captured {len(captured)} GraphQL responses, none had prices)"
        else:
            msg += " (no StaysPdpSections GraphQL response was captured)"
        raise ScrapeError(msg) from None


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
    """Search GraphQL response for a structured price breakdown.

    Modern shape: a section has `structuredDisplayPrice` with a `priceBreakdown`
    object containing `priceItems[]`. Older shape: `priceItems` directly on the
    section.
    """
    candidates: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            sdp = node.get("structuredDisplayPrice")
            if isinstance(sdp, dict):
                pb = sdp.get("priceBreakdown") or sdp.get("explanationData")
                if isinstance(pb, dict) and pb.get("priceItems"):
                    candidates.append(pb)
            if isinstance(node.get("priceItems"), list):
                candidates.append(node)
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(state)

    for node in candidates:
        items = node.get("priceItems") or []
        if not items:
            continue
        line_items: list[tuple[str, float]] = []
        total: float | None = None
        currency = "USD"
        for it in items:
            label = (it.get("description") or it.get("title") or "").strip()
            price_str = (
                it.get("priceString")
                or (it.get("total") or {}).get("amountFormatted")
                or (it.get("amount") or {}).get("amountFormatted")
                or ""
            ).strip()
            amount = _parse_amount(price_str)
            currency = _parse_currency(price_str) or currency
            it_type = (it.get("type") or "").upper()
            if it_type in ("TOTAL", "TOTAL_DEFAULT") or label.lower().startswith("total"):
                if amount is not None:
                    total = amount
            else:
                if amount is not None:
                    line_items.append((label or "(unlabeled)", amount))
        if total is None and line_items:
            total = sum(a for _, a in line_items)
        if total is not None:
            return PriceBreakdown(
                total=total, currency=currency, nights=nights, line_items=line_items,
            )

    raise ScrapeError("No structured price breakdown in GraphQL response")


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

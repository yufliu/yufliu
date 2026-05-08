"""Airbnb price scraper.

Resolves a listing URL (including short /h/ slugs), then loads the rooms page
with check_in/check_out query params and extracts the price breakdown from
the deferred-state JSON embedded in the HTML.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class PriceBreakdown:
    total: float
    currency: str
    nights: int
    line_items: list[tuple[str, float]] = field(default_factory=list)
    final_url: str = ""


class ScrapeError(RuntimeError):
    pass


def resolve_listing_id(url: str, session: requests.Session) -> tuple[str, str]:
    """Follow redirects on a short URL and pull the numeric listing ID."""
    if not url.startswith("http"):
        url = "https://" + url
    r = session.get(url, allow_redirects=True, headers=DEFAULT_HEADERS, timeout=20)
    final_url = r.url
    m = re.search(r"/rooms/(?:plus/)?(\d+)", final_url)
    if not m:
        raise ScrapeError(
            f"Could not extract a numeric listing ID from final URL: {final_url}"
        )
    return m.group(1), final_url


def fetch_price_breakdown(
    listing_url: str,
    checkin: date,
    checkout: date,
    *,
    debug_dump_path: str | None = None,
) -> PriceBreakdown:
    nights = (checkout - checkin).days
    if nights <= 0:
        raise ValueError("checkout must be after checkin")

    session = requests.Session()
    listing_id, _ = resolve_listing_id(listing_url, session)

    target = (
        f"https://www.airbnb.com/rooms/{listing_id}"
        f"?check_in={checkin.isoformat()}"
        f"&check_out={checkout.isoformat()}"
        f"&adults=1&numberOfGuests=1"
    )
    r = session.get(target, headers=DEFAULT_HEADERS, timeout=30)
    r.raise_for_status()

    if debug_dump_path:
        with open(debug_dump_path, "w") as f:
            f.write(r.text)

    state = _extract_deferred_state(r.text)
    breakdown = _walk_for_price(state, nights)
    breakdown.final_url = target
    return breakdown


def _extract_deferred_state(html: str) -> dict[str, Any]:
    # Airbnb embeds JSON in <script id="data-deferred-state-0" ...>...</script>.
    # The id has historically been `data-deferred-state` or `data-deferred-state-0`.
    for pattern in (
        r'<script[^>]+id="data-deferred-state-0"[^>]*>(.*?)</script>',
        r'<script[^>]+id="data-deferred-state"[^>]*>(.*?)</script>',
    ):
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError as e:
                raise ScrapeError(f"deferred-state JSON parse failed: {e}") from e
    raise ScrapeError(
        "Could not find data-deferred-state in HTML. Airbnb may have changed "
        "the page structure or blocked the request. Re-run with --debug-dump "
        "to save the response and inspect."
    )


def _walk_for_price(state: Any, nights: int) -> PriceBreakdown:
    """Find a price-breakdown section anywhere inside the deferred state.

    Airbnb's response shape changes; we search defensively for any object that
    looks like a price summary.
    """
    candidates: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            # Heuristic 1: explicit price breakdown sections
            if any(k in node for k in ("priceItems", "explanationData")) and isinstance(
                node.get("priceItems") or node.get("explanationData", {}).get("priceItems"),
                list,
            ):
                candidates.append(node)
            # Heuristic 2: Stays product page price section
            if node.get("__typename", "").lower().startswith("priceitem") or \
               node.get("type") in ("TOTAL", "TOTAL_DEFAULT"):
                candidates.append(node)
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(state)

    # Prefer a node that has both line items and a TOTAL row
    for node in candidates:
        items = node.get("priceItems") or node.get("explanationData", {}).get("priceItems")
        if not items:
            continue
        line_items: list[tuple[str, float]] = []
        total: float | None = None
        currency = "USD"
        for it in items:
            label = (it.get("description") or it.get("title") or "").strip()
            price_str = (it.get("priceString") or it.get("total", {}).get("amountFormatted") or "").strip()
            amount = _parse_amount(price_str)
            cur = _parse_currency(price_str) or currency
            currency = cur
            if it.get("type") in ("TOTAL", "TOTAL_DEFAULT") or label.lower().startswith("total"):
                if amount is not None:
                    total = amount
            else:
                if amount is not None:
                    line_items.append((label or "(unlabeled)", amount))
        if total is not None:
            return PriceBreakdown(
                total=total,
                currency=currency,
                nights=nights,
                line_items=line_items,
            )

    raise ScrapeError(
        "Found page state but no price breakdown. The dates may be unavailable "
        "for booking, or Airbnb's response shape changed."
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

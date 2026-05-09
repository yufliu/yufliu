"""Pricing manager CLI.

Usage:
    python pricing_manager.py <listing-or-address> [checkin-date]

Examples:
    python pricing_manager.py "217 cactus"               # check-in = today
    python pricing_manager.py 217cactusmtr 1/12/2026
    python pricing_manager.py "cactus tallahassee" 2026-01-12

If no check-in date is given, today's date is used. For each input date,
fetches the price the guest pays for a 7-night, 30-night, and 90-night
stay starting on that date, prints the line-item breakdown, and warns if
the total is more than 10% outside the configured target range for that
listing+span.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from scraper import PriceBreakdown, ScrapeError, fetch_price_breakdown

SPANS: list[tuple[str, int]] = [
    ("weekly", 7),
    ("monthly", 30),
    ("3-month", 90),
]
OFF_TARGET_TOLERANCE = 0.10  # 10%

CONFIG_PATH = Path(__file__).parent / "listings.json"


@dataclass
class TargetCheck:
    status: str  # "ok", "low", "high", "no_target"
    message: str


def parse_date(s: str) -> date:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"Unrecognized date format: {s!r}. Try M/D/YYYY or YYYY-MM-DD.")


def load_listings() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing listings config: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def _tokenize(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _searchable_text(key: str, listing: dict) -> str:
    parts = [key, listing.get("name", ""), listing.get("address", "")]
    parts.extend(listing.get("aliases", []) or [])
    return " ".join(p for p in parts if p)


def resolve_listing(query: str, listings: dict) -> str:
    """Resolve an address-ish query to a listing key.

    First tries an exact key match, then a fuzzy token-subset match across
    each listing's key, name, address, and aliases. Raises LookupError if
    zero or >1 listings match.
    """
    if query in listings:
        return query
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        raise LookupError(f"Empty query: {query!r}")
    matches: list[tuple[str, int]] = []
    for key, listing in listings.items():
        cand_tokens = set(_tokenize(_searchable_text(key, listing)))
        if q_tokens.issubset(cand_tokens):
            matches.append((key, len(q_tokens)))
    if len(matches) == 1:
        return matches[0][0]
    if not matches:
        configured = ", ".join(f"{k} ({v.get('address', 'no address')})"
                               for k, v in listings.items())
        raise LookupError(
            f"No listing matches {query!r}. Configured: {configured}"
        )
    keys = [m[0] for m in matches]
    raise LookupError(
        f"{query!r} is ambiguous; matches: {keys}. "
        "Add more tokens or use the listing key."
    )


def evaluate_target(span: str, breakdown: PriceBreakdown, listing_cfg: dict) -> TargetCheck:
    target = (listing_cfg.get("targets") or {}).get(span)
    if not target:
        return TargetCheck("no_target", "no target configured")
    lo = target["min"] * (1 - OFF_TARGET_TOLERANCE)
    hi = target["max"] * (1 + OFF_TARGET_TOLERANCE)
    note = f" ({target['note']})" if target.get("note") else ""
    base_target = f"target ${target['min']:,}-${target['max']:,}{note}"
    if breakdown.total < lo:
        delta_pct = (breakdown.total - target["min"]) / target["min"] * 100
        return TargetCheck("low", f"BELOW {base_target} by {delta_pct:.1f}%")
    if breakdown.total > hi:
        delta_pct = (breakdown.total - target["max"]) / target["max"] * 100
        return TargetCheck("high", f"ABOVE {base_target} by +{delta_pct:.1f}%")
    return TargetCheck("ok", f"within {base_target} (±10%)")


def print_breakdown(span: str, nights: int, checkin: date, checkout: date,
                    breakdown: PriceBreakdown, target: TargetCheck) -> None:
    header = f"== {span.upper()} ({nights} nights: {checkin} -> {checkout}) =="
    print(header)
    nightly_avg = breakdown.total / nights if nights else 0
    print(f"  Total: ${breakdown.total:,.2f} {breakdown.currency}")
    print(f"  Nightly average: ${nightly_avg:,.2f}")
    if breakdown.line_items:
        print("  Breakdown:")
        for label, amount in breakdown.line_items:
            print(f"    - {label}: ${amount:,.2f}")
    flag = {"ok": "OK", "low": "WARN", "high": "WARN", "no_target": "--"}[target.status]
    print(f"  [{flag}] {target.message}")
    print()


def run(query: str, checkin: date, *, debug_dump: bool, headless: bool) -> int:
    listings = load_listings()
    try:
        listing_key = resolve_listing(query, listings)
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 2
    listing = listings[listing_key]
    print(f"Listing: {listing['name']} [{listing_key}]")
    if listing.get("address"):
        print(f"Address: {listing['address']}")
    print(f"URL: {listing['url']}")
    print(f"Check-in: {checkin}\n")

    any_warn = False
    for span_name, nights in SPANS:
        checkout = checkin + timedelta(days=nights)
        dump = f"debug_{span_name}.html" if debug_dump else None
        try:
            br = fetch_price_breakdown(
                listing["url"], checkin, checkout,
                debug_dump_path=dump, headless=headless,
            )
        except ScrapeError as e:
            print(f"== {span_name.upper()} ({nights} nights) ==")
            print(f"  ERROR: {e}\n")
            any_warn = True
            continue
        target = evaluate_target(span_name, br, listing)
        if target.status in ("low", "high"):
            any_warn = True
        print_breakdown(span_name, nights, checkin, checkout, br, target)
    return 1 if any_warn else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="STR/MTR pricing manager")
    p.add_argument(
        "listing",
        help="Listing key (e.g. 217cactusmtr) OR an address-ish query "
             "(e.g. \"217 cactus\", \"cactus tallahassee\").",
    )
    p.add_argument(
        "checkin",
        nargs="?",
        default=None,
        help="Check-in date, e.g. 1/12/2026 or 2026-01-12. Defaults to today.",
    )
    p.add_argument(
        "--debug-dump", action="store_true",
        help="Save rendered HTML and intercepted GraphQL JSON to "
             "debug_<span>.html and debug_<span>.html.graphql.json.",
    )
    p.add_argument(
        "--show-browser", action="store_true",
        help="Run Playwright with the browser visible (helpful when Cloudflare "
             "blocks headless).",
    )
    args = p.parse_args(argv)
    checkin = parse_date(args.checkin) if args.checkin else date.today()
    return run(
        args.listing, checkin,
        debug_dump=args.debug_dump,
        headless=not args.show_browser,
    )


if __name__ == "__main__":
    sys.exit(main())

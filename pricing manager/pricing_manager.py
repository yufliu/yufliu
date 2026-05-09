"""Pricing manager CLI.

Usage:
    python pricing_manager.py <listing-or-address> [checkin-date]
    python pricing_manager.py --all [checkin-date]

Examples:
    python pricing_manager.py "217 cactus"            # check-in = today
    python pricing_manager.py 217cactusmtr 1/12/2026
    python pricing_manager.py --all                   # every listing in listings.json

If no check-in date is given, today's date is used. For each input date,
fetches the price the guest pays for a 7-night, 30-night, and 90-night
stay starting on that date, prints the line-item breakdown, appends each
result to history.csv, and warns if the total is more than 10% outside
the configured target range for that listing+span.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from notifier import (
    ListingReport, SpanResult, build_message, build_portfolio_message,
    send as send_slack,
)
from scraper import PriceBreakdown, ScrapeError, fetch_price_breakdown

SPANS: list[tuple[str, int, str]] = [
    ("weekly", 7, "Weekly total"),
    ("monthly", 30, "Monthly total"),
    ("3-month", 90, "3-month total"),
]
OFF_TARGET_TOLERANCE = 0.10  # 10%

CONFIG_PATH = Path(__file__).parent / "listings.json"
HISTORY_PATH = Path(__file__).parent / "history.csv"
HISTORY_HEADER = [
    "ts_utc", "listing", "checkin", "span", "nights",
    "total", "currency", "total_before_taxes", "status",
]


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


def print_breakdown(span: str, total_label: str, nights: int,
                    checkin: date, checkout: date,
                    breakdown: PriceBreakdown, target: TargetCheck) -> None:
    print(f"== {span.upper()} ({nights} nights: {checkin} -> {checkout}) ==")
    nightly_avg = breakdown.total / nights if nights else 0
    qualifier = f" ({breakdown.total_qualifier})" if breakdown.total_qualifier else ""
    print(f"  {total_label}{qualifier}: ${breakdown.total:,.2f} {breakdown.currency}")
    if breakdown.total_before_taxes is not None:
        print(f"    before taxes: ${breakdown.total_before_taxes:,.2f}")
    if nights >= 30:
        per_month = breakdown.total / (nights / 30)
        print(f"  Per-month average: ${per_month:,.2f}")
    print(f"  Nightly average: ${nightly_avg:,.2f}")
    if breakdown.line_items:
        print("  Breakdown:")
        for label, amount in breakdown.line_items:
            print(f"    - {label}: ${amount:,.2f}")
    flag = {"ok": "OK", "low": "WARN", "high": "WARN", "no_target": "--"}[target.status]
    print(f"  [{flag}] {target.message}")
    print()


def append_history(listing_key: str, checkin: date, results: list[SpanResult]) -> None:
    new_file = not HISTORY_PATH.exists()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with HISTORY_PATH.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HISTORY_HEADER)
        for r in results:
            w.writerow([
                ts, listing_key, checkin.isoformat(), r.span, r.nights,
                f"{r.total:.2f}" if r.total is not None else "",
                r.currency,
                f"{r.total_before_taxes:.2f}" if r.total_before_taxes is not None else "",
                r.status,
            ])


def run_one(listing_key: str, listing: dict, checkin: date, *,
            debug_dump: bool, headless: bool) -> tuple[list[SpanResult], bool]:
    """Run all spans for one listing. Prints, appends history, returns results."""
    print(f"Listing: {listing['name']} [{listing_key}]")
    if listing.get("address"):
        print(f"Address: {listing['address']}")
    print(f"URL: {listing['url']}")
    print(f"Check-in: {checkin}\n")

    any_warn = False
    results: list[SpanResult] = []
    for span_name, nights, total_label in SPANS:
        checkout = checkin + timedelta(days=nights)
        dump = f"debug_{listing_key}_{span_name}.html" if debug_dump else None
        try:
            br = fetch_price_breakdown(
                listing["url"], checkin, checkout,
                debug_dump_path=dump, headless=headless,
            )
        except ScrapeError as e:
            print(f"== {span_name.upper()} ({nights} nights) ==")
            print(f"  ERROR: {e}\n")
            any_warn = True
            results.append(SpanResult(
                span=span_name, nights=nights, total=None, currency="USD",
                status="error", target_message="", error=str(e),
            ))
            continue
        target = evaluate_target(span_name, br, listing)
        if target.status in ("low", "high"):
            any_warn = True
        print_breakdown(span_name, total_label, nights, checkin, checkout, br, target)
        results.append(SpanResult(
            span=span_name, nights=nights, total=br.total, currency=br.currency,
            status=target.status, target_message=target.message,
            total_qualifier=br.total_qualifier,
            total_before_taxes=br.total_before_taxes,
        ))

    append_history(listing_key, checkin, results)
    return results, any_warn


def run(query: str | None, checkin: date, *, all_listings: bool,
        debug_dump: bool, headless: bool,
        slack_webhook: str | None, notify_mode: str) -> int:
    listings = load_listings()
    keys: list[str]
    if all_listings:
        keys = list(listings.keys())
        if not keys:
            print("No listings configured in listings.json", file=sys.stderr)
            return 2
    else:
        try:
            keys = [resolve_listing(query or "", listings)]
        except LookupError as e:
            print(str(e), file=sys.stderr)
            return 2

    reports: list[ListingReport] = []
    any_warn_overall = False
    for k in keys:
        listing = listings[k]
        results, any_warn = run_one(
            k, listing, checkin,
            debug_dump=debug_dump, headless=headless,
        )
        reports.append(ListingReport(
            name=listing["name"],
            address=listing.get("address", ""),
            url=listing["url"],
            checkin=checkin.isoformat(),
            results=results,
        ))
        if any_warn:
            any_warn_overall = True

    _maybe_notify(reports, any_warn_overall, webhook_url=slack_webhook, mode=notify_mode)
    return 1 if any_warn_overall else 0


def _maybe_notify(reports: list[ListingReport], any_warn: bool, *,
                  webhook_url: str | None, mode: str) -> None:
    if mode == "off":
        return
    if mode == "warn-only" and not any_warn:
        return
    if not webhook_url:
        print("(slack webhook not configured; skipping notification)",
              file=sys.stderr)
        return
    if len(reports) == 1:
        rep = reports[0]
        payload = build_message(
            listing_name=rep.name, listing_address=rep.address,
            listing_url=rep.url, checkin=rep.checkin, results=rep.results,
        )
    else:
        payload = build_portfolio_message(reports)
    try:
        send_slack(webhook_url, payload)
        print(f"(slack notified — {'WARN' if any_warn else 'OK'})", file=sys.stderr)
    except Exception as e:
        print(f"(slack notify failed: {e})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="STR/MTR pricing manager")
    p.add_argument(
        "listing",
        nargs="?",
        default=None,
        help="Listing key (e.g. 217cactusmtr) OR an address-ish query "
             "(e.g. \"217 cactus\", \"cactus tallahassee\"). "
             "Omit when using --all.",
    )
    p.add_argument(
        "checkin",
        nargs="?",
        default=None,
        help="Check-in date, e.g. 1/12/2026 or 2026-01-12. Defaults to today.",
    )
    p.add_argument(
        "--all", dest="all_listings", action="store_true",
        help="Run every listing in listings.json (one combined Slack message).",
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
    p.add_argument(
        "--slack-webhook", default=None,
        help="Slack incoming webhook URL. Falls back to $SLACK_WEBHOOK_URL.",
    )
    notify = p.add_mutually_exclusive_group()
    notify.add_argument(
        "--always-notify", dest="notify_mode", action="store_const",
        const="always",
        help="Send a Slack message every run (default sends only on warnings).",
    )
    notify.add_argument(
        "--no-notify", dest="notify_mode", action="store_const", const="off",
        help="Skip the Slack notification even if a webhook is configured.",
    )
    p.set_defaults(notify_mode="warn-only")
    args = p.parse_args(argv)
    if not args.all_listings and not args.listing:
        p.error("provide a listing/address query or pass --all")
    if args.all_listings and args.listing:
        # Treat the positional as the date when paired with --all
        # so `pricing_manager.py --all 1/12/2026` reads naturally.
        if args.checkin is None:
            args.checkin = args.listing
            args.listing = None
        else:
            p.error("--all takes no listing argument")
    checkin = parse_date(args.checkin) if args.checkin else date.today()
    webhook = args.slack_webhook or os.environ.get("SLACK_WEBHOOK_URL")
    return run(
        args.listing, checkin,
        all_listings=args.all_listings,
        debug_dump=args.debug_dump,
        headless=not args.show_browser,
        slack_webhook=webhook,
        notify_mode=args.notify_mode,
    )


if __name__ == "__main__":
    sys.exit(main())

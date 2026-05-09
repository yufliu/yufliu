"""Slack notification for pricing runs.

Posts one summary message per run via an Incoming Webhook. To get a
webhook URL: create a Slack app at https://api.slack.com/apps, enable
"Incoming Webhooks", install to your workspace, and copy the URL.
Then either:
    export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
or pass --slack-webhook on the command line.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class SpanResult:
    span: str           # "weekly" / "monthly" / "3-month"
    nights: int
    total: float | None
    currency: str
    status: str         # "ok" / "low" / "high" / "no_target" / "error"
    target_message: str
    error: str = ""
    total_qualifier: str = ""
    total_before_taxes: float | None = None


@dataclass
class ListingReport:
    name: str
    address: str
    url: str
    checkin: str
    results: list[SpanResult]


def _format_results(r_list: list[SpanResult]) -> list[str]:
    lines: list[str] = []
    for r in r_list:
        if r.status == "error":
            lines.append(f"• *{r.span}* ({r.nights}n): ERROR — {r.error}")
            continue
        amt = f"${r.total:,.2f} {r.currency}" if r.total is not None else "?"
        qual = f" ({r.total_qualifier})" if r.total_qualifier else ""
        flag = {"ok": "OK", "low": "WARN", "high": "WARN", "no_target": "--"}[r.status]
        line = f"• *{r.span}* ({r.nights}n): {amt}{qual} — [{flag}] {r.target_message}"
        if r.total_before_taxes is not None:
            line += f"  _(before taxes: ${r.total_before_taxes:,.2f})_"
        lines.append(line)
    return lines


def build_message(listing_name: str, listing_address: str, listing_url: str,
                  checkin: str, results: list[SpanResult]) -> dict:
    """Single-listing summary message. Kept for back-compat / single-run callers."""
    has_warn = any(r.status in ("low", "high") for r in results)
    has_error = any(r.status == "error" for r in results)
    prefix = "[ALERT] " if has_warn else ("[ERROR] " if has_error else "")
    header = f"{prefix}{listing_name} — check-in {checkin}"

    lines: list[str] = []
    if listing_address:
        lines.append(listing_address)
    lines.append(f"<{listing_url}|listing>")
    lines.append("")
    lines.extend(_format_results(results))

    return {
        "text": header,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
    }


def build_portfolio_message(reports: list[ListingReport]) -> dict:
    """One Slack message covering multiple listings — used by --all runs."""
    has_warn = any(r.status in ("low", "high") for rep in reports for r in rep.results)
    has_error = any(r.status == "error" for rep in reports for r in rep.results)
    prefix = "[ALERT] " if has_warn else ("[ERROR] " if has_error else "")
    when = reports[0].checkin if reports else ""
    header = f"{prefix}Pricing check — {len(reports)} listing{'s' if len(reports) != 1 else ''}, check-in {when}"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
    ]
    for rep in reports:
        body_lines = [f"<{rep.url}|{rep.name}>"]
        if rep.address:
            body_lines.append(rep.address)
        body_lines.append("")
        body_lines.extend(_format_results(rep.results))
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(body_lines)}}
        )
        blocks.append({"type": "divider"})
    if blocks and blocks[-1].get("type") == "divider":
        blocks.pop()

    return {"text": header, "blocks": blocks}


def send(webhook_url: str, payload: dict, *, timeout: float = 10.0) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise RuntimeError(
                    f"Slack returned HTTP {resp.status}: {resp.read().decode()[:200]}"
                )
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Slack rejected the webhook (HTTP {e.code}): {e.read().decode()[:200]}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Slack: {e.reason}") from e

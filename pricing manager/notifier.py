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


def build_message(listing_name: str, listing_address: str, listing_url: str,
                  checkin: str, results: list[SpanResult]) -> dict:
    has_warn = any(r.status in ("low", "high") for r in results)
    has_error = any(r.status == "error" for r in results)
    prefix = "[ALERT] " if has_warn else ("[ERROR] " if has_error else "")
    header = f"{prefix}{listing_name} — check-in {checkin}"

    lines = []
    if listing_address:
        lines.append(listing_address)
    lines.append(f"<{listing_url}|listing>")
    lines.append("")
    for r in results:
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

    return {
        "text": header,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
    }


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

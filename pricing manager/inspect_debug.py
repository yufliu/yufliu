"""Inspect a debug_*.html file Airbnb returned to figure out where the price
breakdown actually lives, so we can fix the parser in scraper.py.

Usage:
    python inspect_debug.py debug_monthly.html
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def extract_state(html: str):
    for pattern in (
        r'<script[^>]+id="data-deferred-state-0"[^>]*>(.*?)</script>',
        r'<script[^>]+id="data-deferred-state"[^>]*>(.*?)</script>',
    ):
        m = re.search(pattern, html, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    raise SystemExit("No data-deferred-state script tag found.")


def short(value, limit=120):
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "..."


def walk(node, path="$", out=None, max_hits=80):
    if out is None:
        out = []
    if len(out) >= max_hits:
        return out
    if isinstance(node, dict):
        # Surface anything that looks price/availability related
        for k, v in node.items():
            kl = k.lower()
            if any(t in kl for t in (
                "price", "total", "amount", "fee", "tax",
                "checkout", "booking", "stay", "availability",
                "error", "unavailable",
            )):
                out.append((path + "." + k, short(v)))
            walk(v, path + "." + k, out, max_hits)
            if len(out) >= max_hits:
                return out
    elif isinstance(node, list):
        for i, v in enumerate(node[:5]):
            walk(v, f"{path}[{i}]", out, max_hits)
            if len(out) >= max_hits:
                return out
    elif isinstance(node, str):
        # Capture stray dollar-amount strings in case they sit in odd places
        if re.search(r"\$\s*\d", node):
            out.append((path, short(node)))
    return out


def find_section_types(node, found=None):
    if found is None:
        found = set()
    if isinstance(node, dict):
        st = node.get("sectionId") or node.get("__typename") or node.get("loggingId")
        if st and isinstance(st, str):
            found.add(st)
        for v in node.values():
            find_section_types(v, found)
    elif isinstance(node, list):
        for v in node:
            find_section_types(v, found)
    return found


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python inspect_debug.py <debug_*.html>")
    html = Path(sys.argv[1]).read_text()
    print(f"# inspecting {sys.argv[1]} ({len(html):,} chars)")
    state = extract_state(html)

    print("\n## top-level keys")
    if isinstance(state, dict):
        for k in list(state.keys())[:30]:
            print(f"  - {k}")

    print("\n## section / typename markers found")
    for s in sorted(find_section_types(state)):
        print(f"  - {s}")

    print("\n## price/availability hits (first 80)")
    hits = walk(state)
    for path, v in hits:
        print(f"  {path}\n    {v}")

    if not hits:
        print("  (none — likely an availability/error page, not a price page)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Webpage change detector for https://metr.org/time-horizons/

Detection strategy (layered, most-specific to broadest):
  1. Primary:  Extract the "LAST UPDATED" date from <div class="header-date">
  2. Fallback: Extract all visible text and compare its SHA-256 hash
  3. Fallback: Compare the SHA-256 hash of the entire raw HTML body

Any layer reporting a difference counts as CHANGED.
"""

import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TARGET_URL = "https://metr.org/time-horizons/"
SNAPSHOT_FILE = Path("data/snapshot.json")
RSS_FILE = Path("docs/feed.xml")  # served via GitHub Pages from /docs
MAX_RSS_ITEMS = 90  # ~3 months of daily checks

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; webpage-monitor-bot/1.0; "
        "+https://github.com)"
    ),
    "Accept": "text/html",
}
REQUEST_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> str:
    """Fetch the HTML of the target page. Retries once on failure."""
    for attempt in range(2):
        try:
            resp = requests.get(
                url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == 0:
                print(f"  Fetch attempt 1 failed ({exc}), retrying…")
            else:
                raise SystemExit(f"ERROR: Could not fetch {url}: {exc}")


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

def extract_last_updated_date(soup: BeautifulSoup) -> str | None:
    """
    Primary signal – pull the human-readable date string from:
      <div class="header-date">
        <h5>LAST UPDATED</h5>
        <span class="post-date">March 3, 2026</span>
      </div>
    """
    # Strategy A: look for the specific div structure
    header_date_div = soup.find("div", class_="header-date")
    if header_date_div:
        span = header_date_div.find("span", class_="post-date")
        if span and span.get_text(strip=True):
            return span.get_text(strip=True)

    # Strategy B: look for any element with class "post-date" near
    # text containing "LAST UPDATED" (in case the class name changes
    # on the wrapper div but the inner elements stay)
    for h5 in soup.find_all("h5"):
        if "last updated" in h5.get_text(strip=True).lower():
            parent = h5.parent
            if parent:
                span = parent.find("span")
                if span and span.get_text(strip=True):
                    return span.get_text(strip=True)

    # Strategy C: regex-free scan for any span whose text looks like a date
    # near "last updated" – covers major restructuring
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        # Quick heuristic: contains a 4-digit year
        if any(c.isdigit() for c in text) and len(text) < 40:
            prev = span.find_previous(string=lambda s: s and "updated" in s.lower())
            if prev:
                return text

    return None


def visible_text_hash(soup: BeautifulSoup) -> str:
    """Second signal – SHA-256 of all visible text on the page."""
    text = soup.get_text(separator=" ", strip=True)
    return hashlib.sha256(text.encode()).hexdigest()


def raw_html_hash(html: str) -> str:
    """Third signal – SHA-256 of the entire raw HTML."""
    return hashlib.sha256(html.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------

def load_snapshot() -> dict | None:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text())
    return None


def save_snapshot(data: dict) -> None:
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(old: dict, new: dict) -> dict:
    """
    Compare old and new snapshots.  Returns a dict describing what changed.
    """
    changes = {}

    if old["last_updated_date"] != new["last_updated_date"]:
        changes["last_updated_date"] = {
            "old": old["last_updated_date"],
            "new": new["last_updated_date"],
        }

    if old["text_hash"] != new["text_hash"]:
        changes["text_hash"] = True

    if old["html_hash"] != new["html_hash"]:
        changes["html_hash"] = True

    return changes


# ---------------------------------------------------------------------------
# RSS feed management
# ---------------------------------------------------------------------------

def _rss_datetime(dt: datetime) -> str:
    """RFC-822 date string for RSS."""
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def build_or_update_rss(
    changed: bool,
    changes: dict,
    new_snapshot: dict,
    is_initial: bool,
) -> None:
    """Append an item to the RSS feed (or create the feed from scratch)."""

    RSS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    # --- Determine title and description --------------------------------
    if is_initial:
        title = "INITIAL — Monitoring started"
        description = (
            f"Started monitoring {TARGET_URL}. "
            f"Current 'Last Updated' date: {new_snapshot['last_updated_date'] or 'not found'}."
        )
    elif changed:
        parts = []
        if "last_updated_date" in changes:
            parts.append(
                f"'Last Updated' date changed from "
                f"\"{changes['last_updated_date']['old']}\" to "
                f"\"{changes['last_updated_date']['new']}\"."
            )
        if "text_hash" in changes:
            parts.append("Visible page text changed.")
        if "html_hash" in changes:
            parts.append("Raw HTML changed.")
        title = "CHANGED — " + TARGET_URL
        description = " ".join(parts) if parts else "Page content changed."
    else:
        title = "UNCHANGED — " + TARGET_URL
        description = (
            f"No changes detected. "
            f"'Last Updated' date is still: {new_snapshot['last_updated_date'] or 'not found'}."
        )

    # --- Load or create the feed ----------------------------------------
    if RSS_FILE.exists():
        tree = ET.parse(RSS_FILE)
        rss = tree.getroot()
        channel = rss.find("channel")
    else:
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "METR Time Horizons Page Monitor"
        ET.SubElement(channel, "link").text = TARGET_URL
        ET.SubElement(channel, "description").text = (
            "Daily change-detection feed for the METR Task-Completion "
            "Time Horizons page."
        )
        ET.SubElement(channel, "language").text = "en-us"
        tree = ET.ElementTree(rss)

    # Update lastBuildDate
    lbd = channel.find("lastBuildDate")
    if lbd is None:
        lbd = ET.SubElement(channel, "lastBuildDate")
    lbd.text = _rss_datetime(now)

    # --- Add new item ---------------------------------------------------
    item = ET.Element("item")
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "link").text = TARGET_URL
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text = _rss_datetime(now)
    ET.SubElement(item, "guid", isPermaLink="false").text = (
        f"metr-monitor-{now.strftime('%Y%m%dT%H%M%SZ')}"
    )

    # Insert new item at the top (right after the channel metadata)
    # Find index after last non-item child
    insert_idx = 0
    for idx, child in enumerate(channel):
        if child.tag != "item":
            insert_idx = idx + 1
    channel.insert(insert_idx, item)

    # --- Trim old items -------------------------------------------------
    items = channel.findall("item")
    while len(items) > MAX_RSS_ITEMS:
        channel.remove(items.pop())

    # --- Write ----------------------------------------------------------
    ET.indent(tree, space="  ")
    tree.write(RSS_FILE, encoding="unicode", xml_declaration=True)
    # Append trailing newline
    with open(RSS_FILE, "a") as f:
        f.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Fetching {TARGET_URL} …")
    html = fetch_page(TARGET_URL)
    soup = BeautifulSoup(html, "lxml")

    last_updated = extract_last_updated_date(soup)
    txt_hash = visible_text_hash(soup)
    htm_hash = raw_html_hash(html)

    new_snapshot = {
        "last_updated_date": last_updated,
        "text_hash": txt_hash,
        "html_hash": htm_hash,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    print(f"  Last Updated date : {last_updated or '(not found)'}")
    print(f"  Text hash         : {txt_hash[:16]}…")
    print(f"  HTML hash         : {htm_hash[:16]}…")

    old_snapshot = load_snapshot()

    if old_snapshot is None:
        # ---------- First run: seed the snapshot and RSS ----------
        print("\nFirst run — saving initial snapshot.")
        save_snapshot(new_snapshot)
        build_or_update_rss(
            changed=False, changes={}, new_snapshot=new_snapshot, is_initial=True
        )
        print("Done. Initial RSS feed created.")
        return

    # ---------- Subsequent runs: compare ----------
    changes = detect_changes(old_snapshot, new_snapshot)
    changed = bool(changes)

    if changed:
        print(f"\n*** CHANGE DETECTED ***")
        for key, val in changes.items():
            print(f"  {key}: {val}")
    else:
        print("\nNo changes detected.")

    # Always save the new snapshot so hashes stay current
    save_snapshot(new_snapshot)
    build_or_update_rss(
        changed=changed,
        changes=changes,
        new_snapshot=new_snapshot,
        is_initial=False,
    )
    print("RSS feed updated.")


if __name__ == "__main__":
    main()

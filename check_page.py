#!/usr/bin/env python3
"""
YAML change detector for https://metr.org/assets/benchmark_results_1_1.yaml

This monitor fetches the METR benchmark-results YAML, parses it, compares the
parsed structure against the previous saved snapshot, and appends an RSS entry
that explains what changed since the last successful check.

Two hashes are tracked:
  1. data_hash: canonical JSON hash of the parsed YAML structure. This is the
     primary signal and ignores YAML formatting, comments, and key ordering.
  2. raw_hash: SHA-256 hash of the fetched YAML text. This catches raw text-only
     changes such as formatting or comments.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import yaml
except ImportError:  # pragma: no cover - handled at runtime in GitHub Actions
    yaml = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TARGET_URL = "https://metr.org/assets/benchmark_results_1_1.yaml"
SNAPSHOT_FILE = Path("data/snapshot.json")
RSS_FILE = Path("docs/feed.xml")  # served via GitHub Pages from /docs
MAX_RSS_ITEMS = 90  # ~3 months of daily checks
MAX_DIFF_LINES_IN_RSS = 75
MAX_VALUE_CHARS = 180

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; metr-yaml-monitor/1.0; "
        "+https://github.com)"
    ),
    "Accept": "application/x-yaml, text/yaml, text/plain, */*",
}
REQUEST_TIMEOUT = 30  # seconds

LIST_ID_KEYS = (
    "id",
    "key",
    "name",
    "slug",
    "model",
    "system",
    "agent",
    "benchmark",
    "task",
    "dataset",
    "label",
    "date",
)


# ---------------------------------------------------------------------------
# YAML fetching and parsing
# ---------------------------------------------------------------------------

def fetch_yaml(url: str) -> str:
    """Fetch the YAML document. Retries once on failure."""
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

    raise AssertionError("unreachable")


def parse_yaml(raw_yaml: str) -> Any:
    """Parse YAML using PyYAML and return the raw Python structure."""
    if yaml is None:
        raise SystemExit(
            "ERROR: PyYAML is required. Install it with: pip install PyYAML"
        )

    try:
        return yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise SystemExit(f"ERROR: Could not parse YAML from {TARGET_URL}: {exc}")


# ---------------------------------------------------------------------------
# Normalization and hashing
# ---------------------------------------------------------------------------

def normalize_for_json(value: Any) -> Any:
    """
    Convert PyYAML output into a deterministic JSON-serializable structure.

    PyYAML can return dates, datetimes, non-string dict keys, NaN/Infinity, and
    other scalar-ish values that JSON does not represent consistently. This
    function normalizes those so hashing and snapshots are stable.
    """
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, val in value.items():
            normalized[str(normalize_for_json(key))] = normalize_for_json(val)
        return normalized

    if isinstance(value, (list, tuple)):
        return [normalize_for_json(item) for item in value]

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)

    if value is None or isinstance(value, (str, int, bool)):
        return value

    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_shape(value: Any) -> str:
    if isinstance(value, dict):
        keys = sorted(value.keys())
        preview = ", ".join(keys[:10])
        suffix = "…" if len(keys) > 10 else ""
        return f"mapping with {len(keys)} top-level key(s): {preview}{suffix}"

    if isinstance(value, list):
        return f"list with {len(value)} item(s)"

    return type(value).__name__


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------

def load_snapshot() -> dict[str, Any] | None:
    if not SNAPSHOT_FILE.exists():
        return None

    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or not data:
        return None

    return data


def is_compatible_snapshot(snapshot: dict[str, Any] | None) -> bool:
    return bool(
        snapshot
        and snapshot.get("target_url") == TARGET_URL
        and "data" in snapshot
        and "data_hash" in snapshot
        and "raw_hash" in snapshot
    )


def save_snapshot(data: dict[str, Any]) -> None:
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Structural diffing
# ---------------------------------------------------------------------------

def path_join(base: str, key: Any) -> str:
    key_text = str(key)
    if key_text.replace("_", "").replace("-", "").isalnum() and key_text[:1].isalpha():
        return f"{base}.{key_text}"
    return f"{base}[{json.dumps(key_text, ensure_ascii=False)}]"


def index_path(base: str, index: int) -> str:
    return f"{base}[{index}]"


def keyed_path(base: str, key_name: str, key_value: Any) -> str:
    return f"{base}[{key_name}={format_value(key_value, max_chars=60)}]"


def is_scalar_key(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def select_list_key(old: list[Any], new: list[Any]) -> str | None:
    """
    Prefer matching lists of dicts by a stable id-like field instead of index.

    This produces RSS lines such as $.results[name="foo"].score instead of
    $.results[37].score when the YAML uses identifiable records.
    """
    combined = old + new
    if not combined or not all(isinstance(item, dict) for item in combined):
        return None

    for candidate in LIST_ID_KEYS:
        old_values = []
        new_values = []
        ok = True

        for item in old:
            if candidate not in item or not is_scalar_key(item[candidate]):
                ok = False
                break
            old_values.append(item[candidate])

        if not ok:
            continue

        for item in new:
            if candidate not in item or not is_scalar_key(item[candidate]):
                ok = False
                break
            new_values.append(item[candidate])

        if (
            ok
            and len(old_values) == len(set(map(repr, old_values)))
            and len(new_values) == len(set(map(repr, new_values)))
        ):
            return candidate

    return None


def format_value(value: Any, max_chars: int = MAX_VALUE_CHARS) -> str:
    if isinstance(value, dict):
        text = canonical_json(value)
        if len(text) > max_chars:
            keys = sorted(value.keys())
            preview = ", ".join(keys[:6])
            suffix = "…" if len(keys) > 6 else ""
            return f"{{mapping with {len(keys)} key(s): {preview}{suffix}}}"
        return text

    if isinstance(value, list):
        text = canonical_json(value)
        if len(text) > max_chars:
            return f"[list with {len(value)} item(s)]"
        return text

    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def add_change(
    changes: list[str],
    counts: Counter[str],
    kind: str,
    line: str,
    max_lines: int,
) -> None:
    counts[kind] += 1
    if len(changes) < max_lines:
        changes.append(line)


def diff_values(
    old: Any,
    new: Any,
    path: str,
    changes: list[str],
    counts: Counter[str],
    max_lines: int = MAX_DIFF_LINES_IN_RSS,
) -> None:
    if old == new:
        return

    if isinstance(old, dict) and isinstance(new, dict):
        old_keys = set(old.keys())
        new_keys = set(new.keys())

        for key in sorted(new_keys - old_keys):
            child_path = path_join(path, key)
            add_change(
                changes,
                counts,
                "added",
                f"+ {child_path} = {format_value(new[key])}",
                max_lines,
            )

        for key in sorted(old_keys - new_keys):
            child_path = path_join(path, key)
            add_change(
                changes,
                counts,
                "removed",
                f"- {child_path} was {format_value(old[key])}",
                max_lines,
            )

        for key in sorted(old_keys & new_keys):
            diff_values(old[key], new[key], path_join(path, key), changes, counts, max_lines)

        return

    if isinstance(old, list) and isinstance(new, list):
        key_name = select_list_key(old, new)
        if key_name:
            old_by_key = {item[key_name]: item for item in old}
            new_by_key = {item[key_name]: item for item in new}
            old_keys = set(old_by_key.keys())
            new_keys = set(new_by_key.keys())

            for key in sorted(new_keys - old_keys, key=repr):
                child_path = keyed_path(path, key_name, key)
                add_change(
                    changes,
                    counts,
                    "added",
                    f"+ {child_path} = {format_value(new_by_key[key])}",
                    max_lines,
                )

            for key in sorted(old_keys - new_keys, key=repr):
                child_path = keyed_path(path, key_name, key)
                add_change(
                    changes,
                    counts,
                    "removed",
                    f"- {child_path} was {format_value(old_by_key[key])}",
                    max_lines,
                )

            for key in sorted(old_keys & new_keys, key=repr):
                diff_values(
                    old_by_key[key],
                    new_by_key[key],
                    keyed_path(path, key_name, key),
                    changes,
                    counts,
                    max_lines,
                )

            return

        shared_length = min(len(old), len(new))
        for idx in range(shared_length):
            diff_values(old[idx], new[idx], index_path(path, idx), changes, counts, max_lines)

        for idx in range(shared_length, len(new)):
            add_change(
                changes,
                counts,
                "added",
                f"+ {index_path(path, idx)} = {format_value(new[idx])}",
                max_lines,
            )

        for idx in range(shared_length, len(old)):
            add_change(
                changes,
                counts,
                "removed",
                f"- {index_path(path, idx)} was {format_value(old[idx])}",
                max_lines,
            )

        return

    if type(old) is not type(new):
        add_change(
            changes,
            counts,
            "type_changed",
            (
                f"~ {path}: type changed from {type(old).__name__} "
                f"to {type(new).__name__}; {format_value(old)} → {format_value(new)}"
            ),
            max_lines,
        )
        return

    add_change(
        changes,
        counts,
        "changed",
        f"~ {path}: {format_value(old)} → {format_value(new)}",
        max_lines,
    )


def detect_changes(old_snapshot: dict[str, Any], new_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compare snapshots and return a report for RSS/logging."""
    diff_lines: list[str] = []
    counts: Counter[str] = Counter()

    diff_values(
        old_snapshot["data"],
        new_snapshot["data"],
        "$",
        diff_lines,
        counts,
        MAX_DIFF_LINES_IN_RSS,
    )

    structural_change_count = sum(counts.values())
    raw_changed = old_snapshot.get("raw_hash") != new_snapshot.get("raw_hash")
    data_changed = old_snapshot.get("data_hash") != new_snapshot.get("data_hash")

    return {
        "data_changed": data_changed,
        "raw_changed": raw_changed,
        "structural_change_count": structural_change_count,
        "counts": dict(counts),
        "diff_lines": diff_lines,
        "diff_lines_truncated": structural_change_count > len(diff_lines),
    }


# ---------------------------------------------------------------------------
# RSS feed management
# ---------------------------------------------------------------------------

def _rss_datetime(dt: datetime) -> str:
    """RFC-822 date string for RSS."""
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def create_empty_feed() -> ET.ElementTree:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title")
    ET.SubElement(channel, "link")
    ET.SubElement(channel, "description")
    ET.SubElement(channel, "language").text = "en-us"
    return ET.ElementTree(rss)


def load_or_create_feed() -> ET.ElementTree:
    if not RSS_FILE.exists():
        return create_empty_feed()

    try:
        tree = ET.parse(RSS_FILE)
        if tree.getroot().find("channel") is None:
            return create_empty_feed()
        return tree
    except ET.ParseError:
        return create_empty_feed()


def set_channel_text(channel: ET.Element, tag: str, text: str) -> None:
    element = channel.find(tag)
    if element is None:
        element = ET.SubElement(channel, tag)
    element.text = text


def change_summary_sentence(report: dict[str, Any]) -> str:
    counts = Counter(report.get("counts", {}))
    parts = []
    if counts.get("added"):
        parts.append(f"{counts['added']} added")
    if counts.get("removed"):
        parts.append(f"{counts['removed']} removed")
    if counts.get("changed"):
        parts.append(f"{counts['changed']} changed")
    if counts.get("type_changed"):
        parts.append(f"{counts['type_changed']} type-changed")

    if not parts:
        return "0 structural changes"

    return ", ".join(parts)


def build_description(
    report: dict[str, Any] | None,
    new_snapshot: dict[str, Any],
    is_initial: bool,
) -> str:
    if is_initial:
        return (
            f"Started monitoring {TARGET_URL}.\n"
            f"Current parsed YAML hash: {new_snapshot['data_hash']}.\n"
            f"Current YAML shape: {new_snapshot['shape']}."
        )

    assert report is not None

    if report["data_changed"]:
        total = report["structural_change_count"]
        description_lines = [
            f"Detected {total} structural YAML change(s) since the last check: "
            f"{change_summary_sentence(report)}.",
            "",
            "Details:",
        ]
        description_lines.extend(report["diff_lines"])

        if report["diff_lines_truncated"]:
            omitted = total - len(report["diff_lines"])
            description_lines.append(f"… and {omitted} more change(s).")

        if report["raw_changed"]:
            description_lines.extend(["", "Raw YAML text also changed."])

        description_lines.extend(
            [
                "",
                f"New parsed YAML hash: {new_snapshot['data_hash']}",
            ]
        )
        return "\n".join(description_lines)

    if report["raw_changed"]:
        return (
            "Raw YAML text changed since the last check, but the parsed YAML "
            "data is unchanged. This usually means formatting, comments, or "
            "key ordering changed.\n"
            f"Parsed YAML hash remains: {new_snapshot['data_hash']}"
        )

    return (
        "No changes detected in the parsed YAML or raw YAML text.\n"
        f"Parsed YAML hash is still: {new_snapshot['data_hash']}"
    )


def build_or_update_rss(
    report: dict[str, Any] | None,
    new_snapshot: dict[str, Any],
    is_initial: bool,
) -> None:
    """Append an item to the RSS feed, creating the feed if needed."""
    RSS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    if is_initial:
        title = "INITIAL — Monitoring started"
    elif report and report["data_changed"]:
        title = "CHANGED — parsed YAML data changed"
    elif report and report["raw_changed"]:
        title = "CHANGED — raw YAML text changed"
    else:
        title = "UNCHANGED — YAML unchanged"

    description = build_description(report, new_snapshot, is_initial)

    tree = load_or_create_feed()
    rss = tree.getroot()
    channel = rss.find("channel")
    if channel is None:
        tree = create_empty_feed()
        rss = tree.getroot()
        channel = rss.find("channel")

    assert channel is not None

    set_channel_text(channel, "title", "METR Benchmark Results YAML Monitor")
    set_channel_text(channel, "link", TARGET_URL)
    set_channel_text(
        channel,
        "description",
        "Change-detection feed for METR benchmark_results_1_1.yaml.",
    )
    set_channel_text(channel, "language", "en-us")
    set_channel_text(channel, "lastBuildDate", _rss_datetime(now))

    item = ET.Element("item")
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "link").text = TARGET_URL
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text = _rss_datetime(now)
    ET.SubElement(item, "guid", isPermaLink="false").text = (
        f"metr-yaml-monitor-{now.strftime('%Y%m%dT%H%M%SZ')}"
    )

    # Insert new item after channel metadata and before older items.
    insert_idx = 0
    for idx, child in enumerate(channel):
        if child.tag != "item":
            insert_idx = idx + 1
    channel.insert(insert_idx, item)

    items = channel.findall("item")
    while len(items) > MAX_RSS_ITEMS:
        channel.remove(items.pop())

    ET.indent(tree, space="  ")
    tree.write(RSS_FILE, encoding="unicode", xml_declaration=True)
    RSS_FILE.write_text(RSS_FILE.read_text(encoding="utf-8") + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Fetching {TARGET_URL} …")
    raw_yaml = fetch_yaml(TARGET_URL)
    parsed = parse_yaml(raw_yaml)
    normalized_data = normalize_for_json(parsed)
    canonical = canonical_json(normalized_data)

    new_snapshot = {
        "target_url": TARGET_URL,
        "data_hash": sha256_text(canonical),
        "raw_hash": sha256_text(raw_yaml),
        "shape": summarize_shape(normalized_data),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "data": normalized_data,
    }

    print(f"  Parsed YAML hash : {new_snapshot['data_hash'][:16]}…")
    print(f"  Raw YAML hash    : {new_snapshot['raw_hash'][:16]}…")
    print(f"  YAML shape       : {new_snapshot['shape']}")

    old_snapshot = load_snapshot()

    if not is_compatible_snapshot(old_snapshot):
        print("\nFirst run for this YAML — saving initial snapshot.")
        save_snapshot(new_snapshot)
        build_or_update_rss(report=None, new_snapshot=new_snapshot, is_initial=True)
        print("Done. Initial RSS feed entry created.")
        return

    assert old_snapshot is not None
    report = detect_changes(old_snapshot, new_snapshot)
    changed = report["data_changed"] or report["raw_changed"]

    if changed:
        print("\n*** CHANGE DETECTED ***")
        if report["data_changed"]:
            print(
                f"  Parsed YAML data changed: "
                f"{report['structural_change_count']} structural change(s)"
            )
            for line in report["diff_lines"][:20]:
                print(f"  {line}")
            if report["diff_lines_truncated"]:
                omitted = report["structural_change_count"] - len(report["diff_lines"])
                print(f"  … and {omitted} more change(s)")
        elif report["raw_changed"]:
            print("  Raw YAML text changed, but parsed data is unchanged.")
    else:
        print("\nNo changes detected.")

    save_snapshot(new_snapshot)
    build_or_update_rss(report=report, new_snapshot=new_snapshot, is_initial=False)
    print("RSS feed updated.")


if __name__ == "__main__":
    main()

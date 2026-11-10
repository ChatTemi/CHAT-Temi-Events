#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHAT Event Feed publishing gate (V1).

Run AFTER building a fresh public/ folder and BEFORE uploading Pages artifact.
Fetches the current published feed as a baseline to catch accidental mass data
loss. Does not grant public approval and does not modify the Temi Android app.

Python standard library only. A failed baseline fetch fails closed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

HKT = timezone(timedelta(hours=8))
ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_BASELINE = "https://chattemi.github.io/CHAT-Temi-Events/"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_json_from_url(url: str):
    req = Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "CHAT-Feed-Validation-Gate/1.0"})
    with urlopen(req, timeout=20) as response:
        ct = response.headers.get("Content-Type", "").lower()
        if "json" not in ct and "text/plain" not in ct:
            raise ValueError(f"Unexpected baseline Content-Type at {url}: {ct}")
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("Baseline response exceeded size limit")
        return json.loads(body.decode("utf-8-sig"))


class Gate:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.passes = []

    def block(self, msg):
        self.errors.append(str(msg))

    def warn(self, msg):
        self.warnings.append(str(msg))

    def ok(self, msg):
        self.passes.append(str(msg))


def parse_hkt(value: str, label: str, gate: Gate):
    try:
        dt = datetime.fromisoformat(value)
        if dt.utcoffset() != timedelta(hours=8):
            raise ValueError("not +08:00")
        return dt
    except (ValueError, TypeError) as ex:
        gate.block(f"{label}: timestamp must include HKT +08:00 ({ex})")
        return None


def validate(folder: Path, baseline_loader, gate: Gate, now=None):
    now = now or datetime.now(HKT)
    try:
        manifest = read_json(folder / "manifest.json")
    except (OSError, ValueError) as ex:
        gate.block(f"Cannot open fresh manifest.json: {ex}")
        return

    if manifest.get("schema_version") != 1:
        gate.block("Manifest schema_version must be 1")
    if manifest.get("public_release_approved") is not False:
        gate.block("Only internal test feed is supported; public_release_approved must remain false")
    candidates = manifest.get("index_candidates")
    details = manifest.get("detail_pages_available")
    if not isinstance(candidates, int) or not (1 <= candidates <= 40):
        gate.block("index_candidates must be 1..40")
    if candidates != details:
        gate.block("Not all indexed activity detail pages are available")

    updated = parse_hkt(manifest.get("last_updated_hkt"), "manifest.last_updated_hkt", gate)
    snapshot = parse_hkt(manifest.get("snapshot_at_hkt"), "manifest.snapshot_at_hkt", gate)
    for label, dt in (("generation", updated), ("source HTML snapshot", snapshot)):
        if dt:
            delta = (now - dt).total_seconds()
            if delta < -300 or delta > 4 * 3600:
                gate.block(f"New {label} is not fresh (age={delta / 3600:.1f}h)")
    if updated and snapshot and updated < snapshot:
        gate.block("Generated timestamp precedes HTML source snapshot")

    dates = manifest.get("available_dates")
    expected = [(now.date() + timedelta(days=i)).isoformat() for i in range(14)]
    if dates != expected:
        gate.block(f"Expected 14 consecutive HKT dates starting today, got {dates!r}")
    elif dates:
        gate.ok("14 consecutive HKT dates")

    event_by_day = {}
    for day in expected:
        path = folder / "events" / f"{day}.json"
        try:
            payload = read_json(path)
        except (OSError, ValueError) as ex:
            gate.block(f"{day}: missing or malformed daily JSON: {ex}")
            continue
        if payload.get("date_hk") != day:
            gate.block(f"{day}: date_hk mismatch")
        if payload.get("schema_version") != 1:
            gate.block(f"{day}: unsupported schema_version")
        if payload.get("public_release_approved") is not False:
            gate.block(f"{day}: public release flag must remain false")
        coverage = payload.get("coverage") or {}
        if coverage.get("detail_pages_missing") != 0 or coverage.get("detail_pages_available") != candidates:
            gate.block(f"{day}: incomplete detail-page coverage")
        if coverage.get("complete_daily_schedule") is not False:
            gate.block(f"{day}: do not claim verified complete daily coverage")
        if payload.get("route_generation") != "disabled_by_user_request":
            gate.block(f"{day}: event navigation unexpectedly enabled")
        if not payload.get("disclaimer_zh") or not payload.get("disclaimer_en"):
            gate.block(f"{day}: bilingual disclaimer missing")
        ev = payload.get("today_events")
        ex = payload.get("current_exhibitions")
        if not isinstance(ev, list) or not isinstance(ex, list):
            gate.block(f"{day}: events/exhibitions must be arrays")
            continue
        if len(ev) + len(ex) > 200:
            gate.block(f"{day}: suspiciously high daily record count")
        if datetime.fromisoformat(day).weekday() == 1 and payload.get("venue_open_by_regular_rule") is False:
            if ev or ex:
                gate.block(f"{day}: ordinary closed Tuesday contains event/exhibition records")
        if payload.get("venue_open_by_regular_rule") is not True and (ev or ex):
            gate.block(f"{day}: records shown on an unverified/closed venue day")
        if payload.get("venue_open_by_regular_rule") is None:
            gate.block(f"{day}: venue opening rule could not be verified")
        seen = set()
        for kind, records in (("today_events", ev), ("current_exhibitions", ex)):
            for item in records:
                if not isinstance(item, dict):
                    gate.block(f"{day}: non-object item")
                    continue
                rec_id = item.get("occurrence_id")
                if not rec_id or rec_id in seen:
                    gate.block(f"{day}: empty or duplicate occurrence_id {rec_id!r}")
                seen.add(rec_id)
                if item.get("date") != day:
                    gate.block(f"{day}: item date mismatch {rec_id!r}")
                if item.get("route_enabled") is not False:
                    gate.block(f"{day}: route_enabled must be false {rec_id!r}")
                for key in ("event_id", "title_en", "venue_text"):
                    if not isinstance(item.get(key), str) or not item[key].strip():
                        gate.block(f"{day}: missing {key} for {rec_id!r}")
                url = item.get("source_url")
                if url:
                    parsed = urlparse(url)
                    if parsed.scheme != "https" or parsed.hostname not in {"www.mill6chat.org", "mill6chat.org"}:
                        gate.block(f"{day}: non-CHAT or non-HTTPS source_url for {rec_id!r}")
                if kind == "today_events":
                    start = item.get("start_time")
                    end = item.get("end_time")
                    if not isinstance(start, str) or not TIME_PATTERN.fullmatch(start):
                        gate.block(f"{day}: invalid start_time {rec_id!r}")
                    if end and (not isinstance(end, str) or not TIME_PATTERN.fullmatch(end) or end <= start):
                        gate.block(f"{day}: invalid end_time {rec_id!r}")
        event_by_day[day] = payload
    if len(event_by_day) == 14:
        gate.ok("14 daily JSON files readable, with structural checks")

    # Baseline is the PREVIOUS successful Pages deployment. It may cover a
    # different rolling window; compare only the overlapping dates.
    try:
        old_manifest = baseline_loader("manifest.json")
        old_dates = old_manifest.get("available_dates")
        if old_manifest.get("schema_version") != 1 or not isinstance(old_dates, list):
            raise ValueError("baseline manifest is not valid schema v1")
        old_count = old_manifest.get("index_candidates")
        if isinstance(candidates, int) and isinstance(old_count, int) and old_count > 0:
            lost = old_count - candidates
            if lost >= 5 and candidates < old_count * 0.65:
                gate.block(f"Activity index shrank abruptly: {old_count} → {candidates}; review the official website/parser")
            elif lost >= 3:
                gate.warn(f"Activity index changed: {old_count} → {candidates}; some items may have naturally ended")
            if candidates > old_count * 2 and candidates - old_count >= 8:
                gate.warn(f"Activity index grew substantially: {old_count} → {candidates}; check categories")

        overlap = sorted(set(expected) & set(old_dates))
        if len(overlap) < 7:
            gate.block("Less than 7 days overlap with last published feed; cannot perform meaningful comparison")
            return
        old_records = set()
        new_records = set()
        old_info = {}
        new_info = {}
        tracked_fields = ("title_en", "start_time", "end_time", "venue_text", "opening_hours_text")
        for day in overlap:
            old = baseline_loader(f"events/{day}.json")
            if old.get("date_hk") != day:
                raise ValueError(f"Previous daily feed date mismatch: {day}")
            for part in ("today_events", "current_exhibitions"):
                if not isinstance(old.get(part), list):
                    raise ValueError(f"Previous daily feed {day} has invalid {part}")
                for item in old[part]:
                    if item.get("occurrence_id"):
                        key = (day, part, item["occurrence_id"])
                        old_records.add(key)
                        old_info[key] = tuple(item.get(field, "") for field in tracked_fields)
                for item in event_by_day.get(day, {}).get(part, []):
                    if item.get("occurrence_id"):
                        key = (day, part, item["occurrence_id"])
                        new_records.add(key)
                        new_info[key] = tuple(item.get(field, "") for field in tracked_fields)
        removed = old_records - new_records
        if len(old_records) >= 10 and len(removed) >= 5 and len(removed) >= len(old_records) * 0.35:
            gate.block(f"Large unexpected disappearance: {len(removed)}/{len(old_records)} previously listed records missing on overlapping dates")
        elif removed:
            gate.warn(f"{len(removed)} previously listed occurrence(s) disappeared from overlapping dates; may be cancellations/endings")
        modified = [key for key in (old_records & new_records) if old_info.get(key) != new_info.get(key)]
        if len(modified) >= 5 and len(modified) >= max(1, len(old_records & new_records) * 0.30):
            gate.block(f"Large unexplained change in timings/titles/venues: {len(modified)} recurring entries modified")
        elif modified:
            gate.warn(f"{len(modified)} record(s) changed time/title/location; check official site before relying on this feed")
        added = new_records - old_records
        if added:
            gate.ok(f"{len(added)} added occurrence(s) compared with previous publication")
        gate.ok(f"Compared with previous published feed across {len(overlap)} overlapping dates")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as ex:
        gate.block(f"Could not validate against the previous successful Pages feed: {ex}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="public", help="Fresh build directory")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline-url", help="Existing GitHub Pages base URL")
    group.add_argument("--baseline-dir", help="Local previous publication folder, for offline tests")
    args = parser.parse_args(argv)
    gate = Gate()
    if args.baseline_dir:
        base = Path(args.baseline_dir).resolve()
        loader = lambda name: read_json(base / name)
    else:
        base = args.baseline_url.rstrip("/") + "/"
        if not base.startswith("https://"):
            parser.error("Baseline URL must use HTTPS")
        loader = lambda name: load_json_from_url(base + name)
    validate(Path(args.dir).resolve(), loader, gate)
    for msg in gate.passes:
        print("PASS:", msg)
    for msg in gate.warnings:
        print("WARNING:", msg)
        print("::warning::" + msg.replace("\n", " "))
    for msg in gate.errors:
        print("BLOCK:", msg, file=sys.stderr)
        print("::error::" + msg.replace("\n", " "))
    outcome = "BLOCK" if gate.errors else ("WARNING" if gate.warnings else "PASS")
    summary = f"## CHAT Event Data Validation Gate: {outcome}\n\nPASS: {len(gate.passes)} · WARNING: {len(gate.warnings)} · BLOCK: {len(gate.errors)}\n"
    if gate.warnings:
        summary += "\n### Warnings\n" + "\n".join("- " + x for x in gate.warnings) + "\n"
    if gate.errors:
        summary += "\n### Blocking issues\n" + "\n".join("- " + x for x in gate.errors) + "\n"
    summary += "\nNote: Passing technical validation does NOT mean CHAT has approved a public event timetable.\n"
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)
    else:
        print(summary)
    return 1 if gate.errors else 0


if __name__ == "__main__":
    sys.exit(main())

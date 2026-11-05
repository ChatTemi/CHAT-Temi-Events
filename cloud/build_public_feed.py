#!/usr/bin/env python3
"""Read-only CHAT website -> vetted rolling 14-day static JSON feed.
Run only on controlled CI or from command line. No AI, no staff overrides, no tokens.
A failed/partial refresh fails closed: do not deploy a replacement feed.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import CHAT_Event_Parser_Phase02 as parser

ROOT = Path(__file__).resolve().parent
INDEX_URL = 'https://www.mill6chat.org/events/list/'


def build_from_snapshot(snapshot: Path, destination: Path, days: int = 14):
    files = parser.load_snapshot(snapshot)
    report = json.loads(files.get('fetch_report.json', '{}'))
    if report.get('details_failed'):
        raise RuntimeError('Some official event detail pages failed; refusing to publish an incomplete feed')
    candidates = parser.parse_index(files['01_events_list.html'])
    if not candidates or len(candidates) > 40:
        raise RuntimeError(f'Unexpected event card count: {len(candidates)}')
    rows = parser.attach_details(candidates, files)
    missing = [r['event_id'] for r in rows if not r['detail_available']]
    issues = [(r['event_id'], r['review_reasons']) for r in rows if r['review_reasons']]
    if missing or issues:
        raise RuntimeError(f'Incomplete source data, publication refused: missing={missing}; issues={issues}')
    snapshot_time = report.get('retrieved_at_hkt')
    if not snapshot_time:
        # Legacy snapshots cannot be passed off as a current automated refresh.
        raise RuntimeError('Snapshot lacks a verified fetch timestamp')
    retrieved = datetime.fromisoformat(snapshot_time)
    if retrieved.tzinfo is None:
        raise RuntimeError('Refresh timestamp must include HKT offset')
    now = datetime.now(parser.HK_TZ)
    if abs((now - retrieved).total_seconds()) > 3 * 3600:
        raise RuntimeError('Snapshot is older than 3 hours; refusing publication')
    requested = []
    for i in range(days):
        target = (now.date() + timedelta(days=i))
        if target.weekday() == 1 and parser.hk_general_holiday(target) is None:
            raise RuntimeError(f'Holiday calendar does not cover {target.year}; update HK_General_Holidays JSON')
        requested.append(target.isoformat())
    destination.mkdir(parents=True, exist_ok=True)
    (destination/'events').mkdir(exist_ok=True)
    for day in requested:
        payload = parser.generate_day(rows, day, [], snapshot_time)
        payload['source'] = 'CHAT official website public HTML — automated read-only snapshot'
        payload['last_updated_hkt'] = now.isoformat(timespec='seconds')
        payload['public_release_approved'] = False  # disclaimer is mandatory
        payload['coverage']['complete_daily_schedule'] = False  # cannot guarantee cancellations/special events
        if len(payload['today_events']) + len(payload['current_exhibitions']) > 200:
            raise RuntimeError('Suspicious number of events: abort')
        (destination/'events'/f'{day}.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {
        'schema_version': 1,
        'source': INDEX_URL,
        'last_updated_hkt': now.isoformat(timespec='seconds'),
        'snapshot_at_hkt': snapshot_time,
        'available_dates': requested,
        'index_candidates': len(rows),
        'detail_pages_available': len(rows),
        'public_release_approved': False,
        'notes': 'Automatically derived from public website; subject to venue changes; no staff override',
    }
    (destination/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (destination/'index.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><title>CHAT Map Event Feed</title>
    <body><h1>CHAT Map Event Feed</h1><p>Automated public event data for an internal gallery map prototype. Events may change without notice.</p>
    <p>Read <a href="manifest.json">manifest.json</a> for sync status. This service does not guarantee confirmed venue schedules.</p></body></html>''', encoding='utf-8')
    print(f'VALIDATED: {len(rows)} detail pages; {len(requested)} calendar day files; generated {now.isoformat()}')
    return manifest


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--snapshot', help='Use a freshly obtained V2 snapshot (test only, must have valid timestamp)')
    ap.add_argument('--output', default=str(ROOT.parent/'public'))
    ap.add_argument('--days',type=int,default=14)
    args=ap.parse_args()
    if not 1 <= args.days <= 31:
        ap.error('--days must be 1–31')
    if args.snapshot:
        snapshot = Path(args.snapshot).resolve()
    else:
        import CHAT_Refresh_Public_HTML as refresh
        snapshot = refresh.main()
    with tempfile.TemporaryDirectory(prefix='chat-feed-stage-') as td:
        stage = Path(td)
        build_from_snapshot(snapshot,stage,args.days)
        dest = Path(args.output).resolve()
        if dest.exists():
            # remove only previously generated public files, not other application assets
            import shutil
            shutil.rmtree(dest)
        import shutil
        shutil.copytree(stage, dest)
    print('Ready for static publishing:', Path(args.output).resolve())

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('FAILED CLOSED: '+str(e), file=sys.stderr)
        sys.exit(1)

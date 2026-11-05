#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional Windows-only source refresh for Phase 02.
Reads only public CHAT event pages, with a modest request rate. No AI, no writes to source.
Outputs a timestamped snapshot; never overwrites the last good snapshot.
"""
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile, ZIP_DEFLATED
import json
import sys
import time
from CHAT_Event_Parser_Phase02 import HK_TZ, parse_index

INDEX_URL = 'https://www.mill6chat.org/events/list/'
MAX_BYTES = 2_000_000
MAX_CANDIDATES = 40  # stop on unexpectedly large site changes


def get_html(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'www.mill6chat.org', 'mill6chat.org'}:
        raise ValueError('Refusing non-CHAT public HTTPS URL: '+url)
    if url != INDEX_URL and not parsed.path.startswith('/event/'):
        raise ValueError('Refusing non-event URL: '+url)
    req = Request(url, headers={'User-Agent': 'CHAT-Map-Activity-Prototype/1.0 (public site read-only)',
                               'Accept':'text/html,application/xhtml+xml'})
    with urlopen(req, timeout=25) as response:
        final_url = response.geturl()
        if urlparse(final_url).hostname not in {'www.mill6chat.org', 'mill6chat.org'}:
            raise ValueError('Redirected off CHAT website: '+final_url)
        ct = response.headers.get('Content-Type','').lower()
        if 'text/html' not in ct:
            raise ValueError('Expected HTML; got '+ct)
        raw = response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:
            raise ValueError('HTML exceeds safety size limit: '+url)
        charset=response.headers.get_content_charset() or 'utf-8'
        return raw.decode(charset, errors='replace')


def main():
    target_dir=Path(__file__).resolve().parent
    print('Reading official CHAT public event index...')
    index=get_html(INDEX_URL)
    rows=parse_index(index)
    if not rows:
        raise RuntimeError('No current event cards found: refusing to overwrite existing source')
    if len(rows)>MAX_CANDIDATES:
        raise RuntimeError('Unexpected event index size (>40); review site structure before bulk fetching')
    print('Event cards:',len(rows),'| requesting details at ~1 page/second')
    files={'01_events_list.html':index}
    report={'source':'CHAT public website HTML', 'extractor_version':'Phase02_V2', 'retrieved_at_hkt':datetime.now(HK_TZ).isoformat(timespec='seconds'),
            'index_url':INDEX_URL,'candidate_count':len(rows),'details_ok':[], 'details_failed':[]}
    for i,row in enumerate(rows,1):
        print(f"[{i}/{len(rows)}] {row['event_id']}")
        try:
            page=get_html(row['source_url'])
            files['detail__'+row['event_id']+'.html']=page
            report['details_ok'].append({'event_id':row['event_id'],'url':row['source_url']})
        except Exception as err:
            report['details_failed'].append({'event_id':row['event_id'],'url':row['source_url'],
                                             'error':str(err)[:200]})
        if i<len(rows):
            time.sleep(1.0)
    stamp=datetime.now(HK_TZ).strftime('%Y%m%d_%H%M%S')
    output=target_dir/f'CHAT_Public_HTML_Snapshot_{stamp}.zip'
    with ZipFile(output,'w',compression=ZIP_DEFLATED) as z:
        for name,html in files.items():
            z.writestr(name, html.encode('utf-8'))
        z.writestr('fetch_report.json',json.dumps(report,indent=2,ensure_ascii=False))
    (target_dir/'latest_snapshot_path.txt').write_text(str(output.resolve()), encoding='utf-8')
    print('\nSnapshot saved:',output.name)
    print('Detail pages:',len(report['details_ok']),'successful /',len(report['details_failed']),'failed')
    print('This is a source snapshot, not a validated visitor schedule.')
    if report['details_failed']:
        print('WARNING: some detail pages failed; keep the error report and do not publish a complete schedule.')
    return output


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR:',str(exc),file=sys.stderr)
        sys.exit(1)

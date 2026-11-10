#!/usr/bin/env python3
"""Safely change CHAT event release state in a *separate* public GitHub file.
This does NOT deploy Pages, scrape CHAT website, or affect the Validation Gate.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HK = timezone(timedelta(hours=8))
DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'config' / 'release_control.json'
ACTIONS = ('EMERGENCY_DISABLE', 'RESTORE_INTERNAL_TEST', 'APPROVE_PUBLIC_7_DAYS')


def transition(config, action, review_reference='', now=None):
    if config.get('schema_version') != 1:
        raise ValueError('Unexpected control schema')
    if action not in ACTIONS:
        raise ValueError('Unknown release action')
    now = now or datetime.now(HK)
    if now.utcoffset() != timedelta(hours=8):
        raise ValueError('Expected Hong Kong timezone')
    new = dict(config)
    if action == 'EMERGENCY_DISABLE':
        new.update(mode='internal_test', emergency_disable=True,
                   approved_at_hkt=None, approval_expires_hkt=None,
                   review_reference='')
    elif action == 'RESTORE_INTERNAL_TEST':
        new.update(mode='internal_test', emergency_disable=False,
                   approved_at_hkt=None, approval_expires_hkt=None,
                   review_reference='')
    else:
        if config.get('emergency_disable') is not False:
            raise ValueError('Emergency disable is active; first restore internal testing after investigating')
        ref = review_reference.strip()
        if len(ref) < 8 or len(ref) > 120 or any(ord(c) < 32 for c in ref):
            raise ValueError('A non-sensitive 8–120-character Step 11 sign-off reference is required')
        new.update(mode='public', emergency_disable=False,
                   approved_at_hkt=now.isoformat(timespec='seconds'),
                   approval_expires_hkt=(now + timedelta(days=7)).isoformat(timespec='seconds'),
                   review_reference=ref)
    return new


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--action', choices=ACTIONS, required=True)
    p.add_argument('--review-reference', default='')
    p.add_argument('--file', default=str(DEFAULT_PATH))
    args = p.parse_args(argv)
    path = Path(args.file)
    previous = json.loads(path.read_text(encoding='utf-8'))
    changed = transition(previous, args.action, args.review_reference)
    path.write_text(json.dumps(changed, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('Release control updated:', args.action,
          '| mode:', changed['mode'], '| emergency_disable:', changed['emergency_disable'],
          '| expires:', changed['approval_expires_hkt'])
    if 'GITHUB_STEP_SUMMARY' in __import__('os').environ:
        with open(__import__('os').environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(f"### CHAT release control: {args.action}\n"
                    f"Mode: `{changed['mode']}` · Emergency disable: `{str(changed['emergency_disable']).lower()}`\n\n"
                    f"Approval expires: `{changed['approval_expires_hkt']}`\n\n")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print('BLOCK: ' + str(e), file=sys.stderr)
        sys.exit(1)

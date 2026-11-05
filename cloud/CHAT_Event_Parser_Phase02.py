#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHAT Phase 02 V2.4 activity parser: conservative public HTML snapshot processing.

Uses Python standard library. No AI tokens, GPS, routes, Android integration,
or staff override central service. A snapshot is NOT a verified daily programme.
"""
import argparse
import csv
import json
import re
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

HK_TZ = timezone(timedelta(hours=8))
DISCLAIMER_ZH = '活動安排或會臨時更改，恕不另行通知。如有疑問，請向接待處職員查詢。'
DISCLAIMER_EN = 'Event arrangements are subject to change without prior notice. For enquiries, please contact our reception staff.'
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
DATE_RE = re.compile(r'(?<!\d)(\d{1,2})[./](\d{1,2})[./](20\d{2})(?!\d)')
TIME_RE = re.compile(r'(\d{1,2}(?::\d{2})?\s*(?:am|pm|nn)?)\s*[-–—]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|nn)?)', re.I)
# Two-day short range: 07-08.11.2026; cross-month range: 20.03-13.06.2027.
RANGE_SAME_MONTH_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[-–—]\s*(\d{1,2})[./](\d{1,2})[./](20\d{2})(?!\d)')
RANGE_CROSS_MONTH_RE = re.compile(r'(?<!\d)(\d{1,2})[./](\d{1,2})\s*[-–—]\s*(\d{1,2})[./](\d{1,2})[./](20\d{2})(?!\d)')
# Public / general holidays, NOT statutory employment holidays.
# This manually verified file is shipped with the offline parser.
# A weekday outside its coverage is never silently assumed not to be a holiday.
HOLIDAY_CALENDAR_FILE = Path(__file__).with_name('HK_General_Holidays_2026_2027.json')
with HOLIDAY_CALENDAR_FILE.open(encoding='utf-8') as _holiday_file:
    _holiday_data = json.load(_holiday_file)
HK_GENERAL_HOLIDAYS = {str(y): set(days) for y, days in _holiday_data['dates_by_year'].items()}

def hk_general_holiday(day):
    """True, False, or None when government-calendar coverage is unavailable."""
    entries = HK_GENERAL_HOLIDAYS.get(str(day.year))
    return None if entries is None else day.isoformat() in entries


def chat_open_by_regular_schedule(day):
    """CHAT is normally closed on Tuesdays, except HK general holidays.

    Returns True (regular open day), False (regular closure), or None
    (Tuesday with no holiday-calendar coverage). Actual ad-hoc closures
    and programme cancellations remain unverified.
    """
    if day.weekday() != 1:
        return True
    return hk_general_holiday(day)

DETAIL_FILES = {
    'lets-make-a-knitted-doll': '02_known_workshop.html',
    'pixel-to-stitch-knit-computer-sleeve-workshop': '03_multidate_workshop.html',
    'oh-haji-becoming-flowers': '04_exhibition.html',
    '30-minute-making': '05_recurring.html',
}


class Node:
    def __init__(self, tag='', attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.parent = parent
        self.children = []

    def classes(self):
        return set(self.attrs.get('class', '').split())

    def find_all(self, tag=None, cls=None):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in child.classes()):
                    found.append(child)
                found.extend(child.find_all(tag, cls))
        return found

    def first(self, tag=None, cls=None):
        hits = self.find_all(tag, cls)
        return hits[0] if hits else None

    def text(self):
        parts = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, Node) else child)
        return ' '.join(''.join(parts).split())

    def next_sibling(self, tag):
        if not self.parent:
            return None
        siblings = self.parent.children
        try:
            idx = siblings.index(self)
        except ValueError:
            return None
        for item in siblings[idx+1:]:
            if isinstance(item, Node) and item.tag == tag:
                return item
        return None


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        item = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(item)
        if tag not in VOID_TAGS:
            self.stack.append(item)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(content):
    parser = DocumentParser()
    parser.feed(content)
    return parser.root


def slug_of(url):
    return urlparse(url).path.rstrip('/').split('/')[-1]


def iso_dates(text):
    dates = []
    for match in DATE_RE.finditer(text or ''):
        day, month, year = map(int, match.groups())
        try:
            value = date(year, month, day).isoformat()
            if value not in dates:
                dates.append(value)
        except ValueError:
            continue
    return dates



def date_structure(text, classification):
    """Parse visible Date field, never JSON-LD. No guessed missing years.

    A short continuous event range can expand to a few calendar days.
    An exhibition/recurring range remains an interval, never daily slots.
    """
    text = text or ''
    found = iso_dates(text)
    ranges = []
    cross_matches=list(RANGE_CROSS_MONTH_RE.finditer(text))
    cross_spans=[m.span() for m in cross_matches]
    for m in RANGE_SAME_MONTH_RE.finditer(text):
        if any(m.start()<en and m.end()>st for st,en in cross_spans):
            continue  # avoid interpreting '.03-13.06.2027' as a same-month range
        d1, d2, mo, y = map(int, m.groups())
        try:
            a, b = date(y, mo, d1), date(y, mo, d2)
            if a <= b:
                ranges.append((a, b))
        except ValueError:
            continue
    for m in cross_matches:
        d1, m1, d2, m2, year = map(int, m.groups())
        try:
            a, b = date(year, m1, d1), date(year, m2, d2)
            if a <= b:
                ranges.append((a, b))
        except ValueError:
            continue
    # Full-date ranges already appear as two dates in visible text.
    if len(found) == 2 and re.search(r'20\d{2}\s*[-–—]\s*\d{1,2}[./]\d{1,2}[./]20\d{2}', text):
        a, b = date.fromisoformat(found[0]), date.fromisoformat(found[1])
        if a <= b:
            ranges.append((a, b))
    unique_ranges = sorted(set(ranges))
    date_list = set(found)
    for a, b in unique_ranges:
        date_list.update((a.isoformat(), b.isoformat()))
    continuous = len(unique_ranges) == 1 and len(date_list) == 2
    if classification in ('scheduled_event', 'special_event') and continuous:
        a, b = unique_ranges[0]
        if (b-a).days <= 7:
            date_list.update((a+timedelta(days=i)).isoformat() for i in range((b-a).days+1))
        else:
            continuous = False  # do not invent every day for an unexpectedly long event range
    return {
        'dates': sorted(date_list),
        'range_dates': [unique_ranges[0][0].isoformat(), unique_ranges[0][1].isoformat()] if continuous else [],
        'date_parse_confidence': 'visible_date_field' if date_list else 'missing_or_unparsed',
        'short_continuous_range': bool(continuous and classification in ('scheduled_event','special_event')),
    }


def index_and_detail_compatible(index_text, detail_text, classification):
    """A confirmed mismatch blocks auto-publication; text-only recurring rules are different."""
    if classification in {'recurring', 'permanent_display','excluded_private','excluded_digital_or_online','excluded_booking_only','excluded_non_daily','needs_review'}:
        return True
    list_dates = date_structure(index_text, classification)['dates']
    detail_dates = date_structure(detail_text, classification)['dates']
    if not list_dates or not detail_dates:
        return True  # missing is handled by separate review rules
    return list_dates == detail_dates


# CHAT operations exclusions: these entries must never generate Today's Events
# occurrences, even if the website later lists a date. They remain in the
# candidates output for auditing and possible use in another content section.
OPERATIONS_EXCLUSIONS = {
    'learning tour on sustainability': 'excluded_booking_only',
    'chat contemporary textile art prize 2027 finalists': 'excluded_non_daily',
}


def classify(title, display_date, categories):
    # Prefer explicit venue-operational decisions to the site's broad categories.
    special = OPERATIONS_EXCLUSIONS.get(' '.join(title.casefold().split()))
    if special:
        return special
    t = (title + ' ' + display_date).lower()
    cats = set(categories)
    if 'private' in t or 'by booking' in t:
        return 'excluded_private'
    if 'digital guide' in t or 'online programme' in t or 'online' in cats:
        return 'excluded_digital_or_online'
    if 'permanent' in t or 'special display' in t:
        return 'permanent_display'
    if 'exhibition' in cats:
        return 'exhibition'
    if 'weekends and public holidays' in t or 'every sunday' in t or 'regular' in t:
        return 'recurring'
    if 'performance' in cats:
        return 'special_event'
    if cats & {'workshops', 'tours', 'demonstration'}:
        return 'scheduled_event'
    return 'needs_review'


def timeslots(text):
    """Infer meridiem only if single non-overnight plausible <= 8 h solution exists."""
    slots = []
    for m in TIME_RE.finditer(text or ''):
        left, right = m.group(1).lower().replace(' ', ''), m.group(2).lower().replace(' ', '')
        def parts(value):
            x = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(am|pm|nn)?', value)
            if not x:
                return None
            h, minute = int(x.group(1)), int(x.group(2) or 0)
            return h, minute, x.group(3)
        lp, rp = parts(left), parts(right)
        if not lp or not rp:
            continue
        def possibilities(piece):
            h, mi, suffix = piece
            if h > 23 or mi > 59:
                return []
            if not suffix and (h == 0 or h > 12):
                return [h * 60 + mi]  # explicit 24-hour form, e.g. 15:00–16:00
            if (suffix and (h < 1 or h > 12)):
                return []
            meridiems = ['pm'] if suffix == 'nn' else ([suffix] if suffix else ['am', 'pm'])
            return [(h % 12 + (12 if s == 'pm' else 0)) * 60 + mi for s in meridiems]
        combos = sorted((end-start, start, end) for start in possibilities(lp) for end in possibilities(rp)
                        if 0 < (end-start) <= 8*60)
        if not combos:
            continue
        # If multiple interpretations give different plausible time slots, mark ambiguous, do not publish.
        if len(set((s, e) for _, s, e in combos)) > 1:
            slots.append({'start_time': None, 'end_time': None, 'raw': m.group(), 'ambiguous': True})
            continue
        _, start, end = combos[0]
        context = (text or '')[max(0, m.start()-85):m.start()].lower()
        label = ''
        if 'pre-performance workshop' in context or 'pre-performance workshop' in (text or '').lower():
            # Multiple pairs on the same page: label from the nearest preceding subsection.
            label = 'Pre-performance workshop' if ('pre-performance workshop' in context and
                     context.rfind('pre-performance workshop') >= context.rfind('performance:')) else ''
        if not label and re.search(r'performance\s*:\s*$', context):
            label = 'Performance'
        slots.append({'start_time': f'{start//60:02d}:{start%60:02d}',
                      'end_time': f'{end//60:02d}:{end%60:02d}', 'raw': m.group(),
                      'session_label': label, 'ambiguous': False})
    return slots



def explicit_start_times(text):
    """Handle a visible Time field containing *only* comma-delimited start times.

    E.g. "3:30pm, 4:30pm" is two starts, NOT a guessed one-hour tour.
    Reject ambiguous / prose-rich values and never supply a fabricated end time.
    """
    text = (text or '').strip()
    if not text or re.search(r'[-–—]', text):
        return []
    parts = re.split(r'\s*[,;]\s*', text)
    if not (1 <= len(parts) <= 8):
        return []
    result = []
    for entry in parts:
        m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|nn)', entry.strip(), re.I)
        if not m:
            return []
        hour, minute, marker = int(m[1]), int(m[2] or '0'), m[3].lower()
        if not (1 <= hour <= 12 and minute < 60):
            return []
        hour = hour % 12 + (12 if marker in ('pm', 'nn') else 0)
        result.append({'start_time':f'{hour:02d}:{minute:02d}',
                       'end_time':'', 'raw':entry.strip(),
                       'session_label':'Start time only (end not supplied by source)',
                       'ambiguous':False})
    return result

def parse_index(html):
    doc = parse_html(html)
    lists = doc.find_all('ul', 'ecs-event-list')
    if not lists:
        raise ValueError('找不到 ul.ecs-event-list；官方網站版面可能已改動。')
    rows = []
    for i, li in enumerate(lists[0].find_all('li', 'ecs-event'), 1):
        h = li.first('h4', 'entry-title')
        a = h.first('a') if h else None
        duration = li.first(cls='duration')
        if not a or not a.attrs.get('href'):
            continue
        cats = sorted(c[:-len('_ecs_category')] for c in li.classes() if c.endswith('_ecs_category'))
        url = a.attrs['href']
        title = a.text()
        raw_date = duration.text() if duration else ''
        rows.append({'index': i, 'event_id': slug_of(url), 'title_en': title,
                     'source_url': url, 'list_date_text': raw_date, 'categories': cats,
                     'classification': classify(title, raw_date, cats), 'detail_available': False,
                     'raw_date_text': '', 'raw_time_text': '', 'venue_text': '',
                     'dates': [], 'time_slots': [], 'range_dates': [],
                     'review_reasons': ['detail_page_not_in_snapshot']})
    return rows


def parse_detail(html):
    doc = parse_html(html)
    details = {}
    for dt in doc.find_all('dt'):
        label = dt.text().strip().lower().rstrip(':')
        nxt = dt.next_sibling('dd')
        if label in ('date', 'time') and nxt:
            details[label] = nxt.text()
    venue = doc.first('dd', 'tribe-venue')
    details['venue'] = venue.text() if venue else ''
    # Some event pages, notably Guided Tour, put the *public walk-in* schedule
    # in a paragraph, while the usual Date and Time <dd> fields are empty.
    # Restrict extraction to a labelled sentence, avoiding group-booking text
    # and other clock times (e.g. the docent's 4:00pm-5:10pm gallery presence).
    for paragraph in doc.find_all('p'):
        txt = paragraph.text().strip()
        m = re.match(r'Every\s+Friday,\s*Saturday,\s*Sunday\s+and\s+Public\s+Holidays?\s*:\s*(.+)', txt, re.I)
        if not m:
            continue
        starts = m.group(1).split('(')[0].strip().rstrip('.')
        slots = explicit_start_times(starts)
        if len(slots) == 2 and all(slot['start_time'] for slot in slots):
            details['walkin_schedule_dates'] = 'Every Friday, Saturday, Sunday and Public Holiday'
            details['walkin_schedule_times'] = starts
            break
    return details


def attach_details(rows, files):
    for row in rows:
        file = ('detail__' + row['event_id'] + '.html') if ('detail__' + row['event_id'] + '.html') in files else DETAIL_FILES.get(row['event_id'])
        if file not in files:
            continue
        data = parse_detail(files[file])
        if row['classification'] == 'recurring' and data.get('walkin_schedule_times'):
            data['date'] = data['walkin_schedule_dates']
            data['time'] = data['walkin_schedule_times']
        row.update(detail_available=True,
                   raw_date_text=data.get('date', ''), raw_time_text=data.get('time', ''),
                   venue_text=data.get('venue', ''),
                   dates=date_structure(data.get('date', ''), row['classification'])['dates'],
                   time_slots=(timeslots(data.get('time', '')) or explicit_start_times(data.get('time', ''))),
                   review_reasons=[])
        date_info=date_structure(row['raw_date_text'],row['classification'])
        row['range_dates']=date_info['range_dates']
        row['date_parse_confidence']=date_info['date_parse_confidence']
        if not row['raw_date_text']:
            row['review_reasons'].append('missing_detail_date')
        if row['classification'] in ('scheduled_event','special_event','exhibition') and not row['dates']:
            row['review_reasons'].append('unparsed_detail_date')
        if not index_and_detail_compatible(row['list_date_text'],row['raw_date_text'],row['classification']):
            row['review_reasons'].append('index_detail_date_mismatch')
        if not row['venue_text']:
            row['review_reasons'].append('missing_venue')
        if row['classification'] in ('scheduled_event', 'special_event', 'recurring') and not row['time_slots']:
            row['review_reasons'].append('missing_or_ambiguous_time')
        if any(s['ambiguous'] for s in row['time_slots']):
            row['review_reasons'].append('ambiguous_time')
        if row['classification'] == 'exhibition' and not row['range_dates']:
            row['review_reasons'].append('exhibition_range_not_verified')
        if row['classification'] == 'recurring':
            dt_text = row['raw_date_text'].lower()
            if ('every friday, saturday, sunday and public holiday' in dt_text
                  and len(row['time_slots']) == 2):
                row['recurrence'] = 'fri_sat_sun_and_hk_public_holidays'
            elif ('weekdays' in row['raw_time_text'].lower()
                  and 'weekends and public holidays' in row['raw_time_text'].lower()
                  and len(row['time_slots']) == 2):
                # The labelled weekday hours precede weekend / PH hours.
                row['recurrence'] = 'weekday_weekend_split'
                row['closed_tuesday'] = 'closed on tuesdays' in row['raw_time_text'].lower()
            elif 'weekends and public holidays' in dt_text:
                row['recurrence'] = 'weekends_and_hk_public_holidays'
            elif 'every sunday' in dt_text and len(row.get('range_dates', [])) == 2:
                row['recurrence'] = 'weekly_sunday'
            else:
                row['review_reasons'].append('unparsed_recurrence')
        # These are not scheduled public programmes; missing event dates are normal.
        if row['classification'] in {'excluded_private', 'excluded_digital_or_online', 'excluded_booking_only', 'excluded_non_daily', 'permanent_display'}:
            row['review_reasons'] = []
    return rows


def load_snapshot(zip_file):
    with zipfile.ZipFile(zip_file) as zipped:
        files = {}
        for path in zipped.namelist():
            name = path.split('/')[-1]
            if (name in set(DETAIL_FILES.values()) | {'01_events_list.html', 'fetch_report.json'}
                    or (name.startswith('detail__') and name.endswith('.html'))):
                files[name] = zipped.read(path).decode('utf-8-sig')
        if '01_events_list.html' not in files:
            raise ValueError('ZIP 內沒有 01_events_list.html')
        return files


def load_overrides(file):
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding='utf-8-sig'))
    result = data.get('overrides', [])
    if not isinstance(result, list):
        raise ValueError('staff_overrides.json: overrides 必須是列表')
    return result


def apply_overrides(item, overrides, on_date):
    for edit in overrides:
        if edit.get('event_id') != item['event_id']:
            continue
        if edit.get('occurrence_id') and edit['occurrence_id'] != item.get('occurrence_id'):
            continue
        if edit.get('date') not in (None, '', on_date):
            continue
        if edit.get('expires_on') and on_date > edit['expires_on']:
            continue
        if edit.get('status') in ('cancelled', 'hidden'):
            item['status'] = edit['status']
        elif edit.get('status') == 'confirmed':
            item['status'] = 'confirmed'
        for key in ('venue_text', 'title_en', 'start_time', 'end_time'):
            if key in edit:
                item[key] = str(edit[key])
        item['staff_override_applied'] = True
    return item


def occurrences_for_day(rows, day, overrides):
    day_obj = date.fromisoformat(day)
    events, exhibitions = [], []
    # CHAT operations rule: ordinary Tuesdays are closed; a Tuesday that is
    # a Hong Kong general holiday is open. Suppress ALL public on-site listings
    # (including exhibitions) on a regular closure day. Unknown Tuesday
    # holidays fail closed until the calendar for that year is installed.
    if chat_open_by_regular_schedule(day_obj) is not True:
        return events, exhibitions
    for row in rows:
        if not row['detail_available']:
            continue
        typ = row['classification']
        if typ.startswith('excluded_') or typ == 'permanent_display':
            continue
        if typ == 'exhibition':
            if row['review_reasons']:
                continue
            rng = row.get('range_dates', [])
            if len(rng) != 2 or not rng[0] <= day <= rng[1]:
                continue
            item = {'event_id': row['event_id'], 'occurrence_id': row['event_id']+'@'+day,
                    'title_en': row['title_en'], 'date': day, 'venue_text': row['venue_text'],
                    'opening_hours_text': row['raw_time_text'], 'source_url': row['source_url'],
                    'status': 'listed_within_exhibition_period', 'category': 'current_exhibitions',
                    'open_today_verified': False,  # exhibit period != daily opening guarantee
                    'route_enabled': False}
            item = apply_overrides(item, overrides, day)
            if item['status'] != 'hidden':
                exhibitions.append(item)
            continue
        is_holiday = hk_general_holiday(day_obj)
        is_weekend = day_obj.weekday() in (5, 6)
        scheduled = typ in ('scheduled_event', 'special_event') and day in row['dates']
        weekend = (typ == 'recurring' and row.get('recurrence') == 'weekends_and_hk_public_holidays'
                   and (is_weekend or is_holiday is True))
        guided = (typ == 'recurring' and row.get('recurrence') == 'fri_sat_sun_and_hk_public_holidays'
                  and (day_obj.weekday() in (4, 5, 6) or is_holiday is True))
        weekly = (typ == 'recurring' and row.get('recurrence') == 'weekly_sunday'
                  and len(row.get('range_dates', [])) == 2
                  and row['range_dates'][0] <= day <= row['range_dates'][1]
                  and day_obj.weekday() == 6)
        # The site's Tuesday closure has a general-holiday exception,
        # confirmed by CHAT operations. Cotton Insider uses its stated
        # weekend / public-holiday session when Tuesday is a general holiday.
        closed_tuesday = day_obj.weekday() == 1 and is_holiday is not True and row.get('closed_tuesday')
        split = (typ == 'recurring' and row.get('recurrence') == 'weekday_weekend_split'
                 and not closed_tuesday and (is_weekend or is_holiday is not None))
        if not (scheduled or weekend or guided or weekly or split):
            continue
        # An unclear source schedule is never auto-published, even if a date matches.
        if row['review_reasons']:
            continue
        if not row['venue_text']:
            continue
        if split:
            # Holiday schedule overrides the weekday schedule exactly once.
            which = 1 if (is_weekend or is_holiday is True) else 0
            chosen_slots = [(which+1,row['time_slots'][which])]
        else:
            chosen_slots = list(enumerate(row['time_slots'], 1))
        for j, slot in chosen_slots:
            if slot['ambiguous'] or not slot['start_time']:
                continue
            item = {'event_id': row['event_id'], 'occurrence_id': row['event_id']+'@'+day+'#'+str(j),
                    'title_en': row['title_en'], 'date': day,
                    'start_time': slot['start_time'], 'end_time': slot['end_time'],
                    'venue_text': row['venue_text'], 'source_url': row['source_url'],
                    'status': 'provisional_from_snapshot', 'category': 'today_events',
                    'session_label': slot.get('session_label',''),
                    'route_enabled': False, 'staff_override_applied': False}
            if weekend or guided or weekly or split:
                item['notes'] = 'Recurring timetable from official page; actual availability, special closures and cancellations not verified'
            if is_holiday is True:
                item['calendar_day_type'] = 'hk_general_holiday'
            elif is_weekend:
                item['calendar_day_type'] = 'weekend'
            else:
                item['calendar_day_type'] = 'weekday'
            if not slot.get('end_time'):
                item['notes'] = (item.get('notes', '')+'; ' if item.get('notes') else '')+'Only the start time is stated on the source page'
            item = apply_overrides(item, overrides, day)
            if item['status'] != 'hidden':
                events.append(item)
    events.sort(key=lambda x: (x.get('start_time',''), x['title_en']))
    exhibitions.sort(key=lambda x: x['title_en'])
    return events, exhibitions


def generate_day(rows, day, overrides, snapshot_timestamp):
    events, exhibitions = occurrences_for_day(rows, day, overrides)
    regular_open = chat_open_by_regular_schedule(date.fromisoformat(day))
    return {
        'schema_version': 1,
        'source': 'CHAT official website public HTML snapshot; not live',
        'snapshot_tested_at_hkt': snapshot_timestamp,
        'generated_at_hkt': datetime.now(HK_TZ).isoformat(timespec='seconds'),
        'date_hk': day,
        'coverage': {'index_candidates': len(rows),
                     'detail_pages_available': sum(e['detail_available'] for e in rows),
                     'detail_pages_missing': sum(not e['detail_available'] for e in rows),
                     'complete_daily_schedule': False},
        'public_release_approved': False,
        'venue_open_by_regular_rule': regular_open,
        'venue_regular_rule_source': 'CHAT operations: normally closed Tuesdays except HK general holidays; exceptional closures not verified',
        'holiday_calendar': {'source': 'HK Government General Holidays (not Labour Department statutory holidays)',
                             'coverage_years': sorted(HK_GENERAL_HOLIDAYS),
                             'verified_for_target_year': str(date.fromisoformat(day).year) in HK_GENERAL_HOLIDAYS},
        'message_if_empty_zh': '目前樣本資料未找到已核實的當日活動；資料未齊全，不能代表當日沒有活動。',
        'message_if_empty_en': 'No matching events were found in this incomplete sample; this does not mean there are no events today.',
        'disclaimer_zh': DISCLAIMER_ZH,
        'disclaimer_en': DISCLAIMER_EN,
        'today_events': events,
        'current_exhibitions': exhibitions,
        'route_generation': 'disabled_by_user_request'
    }


def save_preview(out_path, days):
    payload = json.dumps(days, ensure_ascii=False).replace('</', '<\\/')
    template = r'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CHAT 活動解析原型（僅供內部測試）</title><style>
    :root{font-family:Arial,"Noto Sans TC",sans-serif;color:#293a36;background:#f2f5f2}*{box-sizing:border-box}body{margin:0;padding:26px}main{max-width:1000px;margin:auto}h1{font-size:24px;margin:0 0 6px}.sub{color:#61766d;margin:0 0 18px}.banner{padding:12px 16px;background:#fff4e3;border:1px solid #c3aa76;border-radius:8px;margin-bottom:18px}.layout{display:grid;grid-template-columns:1fr 1fr;gap:20px}.panel{background:#fff;border:1px solid #d7dfd9;padding:20px;border-radius:12px;min-height:300px}.label{font-size:13px;color:#587369;letter-spacing:.05em;text-transform:uppercase}h2{font-size:19px;margin:8px 0 12px}.event{border-top:1px solid #e0e6e1;padding:12px 0}.event b{display:block;margin-bottom:6px}.meta{font-size:14px;line-height:1.55;color:#52645e}.event a{font-size:13px;color:#1c6556}.notice{font-size:12px;color:#60716a;border-top:1px solid #d9e0da;margin-top:16px;padding-top:12px;line-height:1.6}.pill{display:inline-block;border-radius:50px;border:1px solid #ccd9cf;padding:4px 9px;font-size:12px;margin:3px 0}input,select{padding:8px 10px;border:1px solid #a3b3a9;border-radius:7px;font:inherit}select#date{min-width:158px}button{border:1px solid #a9bab0;background:white;border-radius:7px;padding:8px 12px;cursor:pointer}@media(max-width:700px){.layout{grid-template-columns:1fr}body{padding:14px}}
    </style></head><body><main><h1>CHAT 今日活動 · Today's Events</h1><p class="sub">Phase 02 — 離線解析器原型（不是 Temi 正式版）</p><div class="banner">⚠ 本頁只使用已下載的官方活動 HTML 快照；部分詳情或日期可能未核實。資料不可直接對外發布，不能據此斷定「今日沒有活動」。</div>
    <div style="margin-bottom:16px"><label>測試日期（香港時間） <select id="date">__DATE_OPTIONS__</select></label> <button type="button" onclick="go(Object.keys(DAYS)[0])">第一日</button> <button type="button" onclick="go(Object.keys(DAYS)[1]||Object.keys(DAYS)[0])">第二日</button></div>
    <div class="layout"><section class="panel"><div class="label">今日活動</div><h2>Today's Events</h2><div id="events"></div></section><section class="panel"><div class="label">現正展出</div><h2>Current Exhibitions</h2><div id="exhibitions"></div></section></div>
    <p class="notice">活動安排或會臨時更改，恕不另行通知。如有疑問，請向接待處職員查詢。<br>Event arrangements are subject to change without prior notice. For enquiries, please contact our reception staff.<br><strong>來源：</strong>官網 HTML 快照 · 非即時同步 · 僅內部測試。</p></main>
    <script>const DAYS = __PAYLOAD__; const node=(t,v)=>{const e=document.createElement(t);e.textContent=v;return e};
    function draw(id,arr,when){const target=document.getElementById(id);target.replaceChildren();if(!arr||!arr.length){target.append(node('p',id==='events'?'此不完整樣本未找到當日活動；不代表沒有活動。':'此樣本未找到對應展覽。'));return;}
    arr.forEach(e=>{const card=node('div','');card.className='event';card.append(node('b',e.title_en));const meta=node('div',(e.start_time?e.start_time+(e.end_time?'–'+e.end_time:'')+' · ':'')+(e.venue_text||'Location not verified'));meta.className='meta';card.append(meta);if(e.status==='cancelled'){card.append(node('span','Cancelled — 已取消'))}if(e.source_url){const a=document.createElement('a');a.href=e.source_url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='Official details / 官方詳情';card.append(a)}target.append(card)})}
    function render(){const d=document.getElementById('date').value;const obj=DAYS[d];if(!obj){throw new Error('Preview date not generated: '+d)}if(obj.venue_open_by_regular_rule!==true){const message=obj.venue_open_by_regular_rule===false?'CHAT 星期二休館（公眾假期除外）。\nCHAT is normally closed on Tuesdays, except public holidays.':'未能核實該年星期二是否公眾假期，暫不展示活動。\nHoliday calendar unavailable for this Tuesday; event information withheld.';for(const id of ['events','exhibitions']){document.getElementById(id).replaceChildren(node('p',message))}return;}draw('events',obj.today_events,d);draw('exhibitions',obj.current_exhibitions,d)}function go(d){document.getElementById('date').value=d;render()}document.getElementById('date').addEventListener('change',render);render();</script></body></html>'''
    date_options = ''.join('<option value="'+d+'"'+(' selected' if d==next(iter(days)) else '')+'>'+d+'</option>' for d in days)
    out_path.write_text(template.replace('__PAYLOAD__', payload).replace('__DATE_OPTIONS__', date_options), encoding='utf-8')


def save_staff_editor(out_path, rows):
    template_path = Path(__file__).with_name('Staff_Override_Editor_Template.html')
    template = template_path.read_text(encoding='utf-8')
    data = json.dumps([{'event_id': e['event_id'], 'title_en':e['title_en'],
                        'list_date_text':e['list_date_text'], 'detail_available':e['detail_available'],
                        'time_slots':e['time_slots']} for e in rows], ensure_ascii=False).replace('</', '<\\/')
    out_path.write_text(template.replace('__EVENTS_DATA__', data), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='CHAT Phase 02 offline HTML parsing prototype')
    parser.add_argument('--zip', default='results_v3.zip', help='Path to Phase 01 V3 snapshot zip')
    parser.add_argument('--overrides', default='staff_overrides.json', help='Optional staff override JSON')
    parser.add_argument('--output', default='output_phase02', help='Output directory')
    parser.add_argument('--dates', default='', help='Comma-separated HK calendar dates, blank = 14 days from today (HK time)')
    args = parser.parse_args()
    in_file, override_file, out = Path(args.zip), Path(args.overrides), Path(args.output)
    if not in_file.is_file():
        parser.error('找不到輸入 ZIP: '+str(in_file))
    source_html = load_snapshot(in_file)
    rows = attach_details(parse_index(source_html['01_events_list.html']), source_html)
    if not rows:
        raise RuntimeError('No event cards found; aborting')
    overrides = load_overrides(override_file)
    out.mkdir(parents=True, exist_ok=True)
    if 'fetch_report.json' in source_html:
        report = json.loads(source_html['fetch_report.json'])
        stamp = report.get('retrieved_at_hkt','unknown')
    else:
        stamp = '2026-11-05T15:29:00+08:00'  # V3 user-supplied snapshot, not live timestamp.
    (out/'events_index_and_details.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2),encoding='utf-8')
    with (out/'all_23_candidates.csv').open('w', newline='', encoding='utf-8-sig') as fp:
        fieldnames=['index','title_en','classification','list_date_text','raw_date_text','raw_time_text','venue_text','detail_available','source_url','review_reasons']
        w=csv.DictWriter(fp,fieldnames=fieldnames);w.writeheader()
        for r in rows:
            w.writerow({k:(' | '.join(r[k]) if isinstance(r[k], list) else r.get(k, '')) for k in fieldnames})
    days={}
    chosen_days=([d.strip() for d in args.dates.split(',') if d.strip()]
                 if args.dates else [(datetime.now(HK_TZ).date()+timedelta(days=i)).isoformat() for i in range(14)])
    for target in chosen_days:
        date.fromisoformat(target)  # fail early for invalid dates
        day=generate_day(rows,target,overrides,stamp)
        days[target]=day
        (out/f'events_{target}.json').write_text(json.dumps(day,ensure_ascii=False,indent=2),encoding='utf-8')
    save_preview(out/'CHAT_Todays_Events_Demo.html',days)
    # Staff Override online editor/service is explicitly deferred; no editor output here.
    issues=[{'title_en':r['title_en'],'event_id':r['event_id'],'review_reasons':r['review_reasons']} for r in rows if r['review_reasons']]
    (out/'needs_review.json').write_text(json.dumps(issues,ensure_ascii=False,indent=2),encoding='utf-8')
    info={'candidate_count':len(rows), 'detail_count':sum(r['detail_available'] for r in rows),
          'needs_review_count':len(issues),
          'details_missing':sum(not r['detail_available'] for r in rows),
          'details_with_blocking_issues':sum(bool(r['review_reasons']) for r in rows if r['detail_available']),
          'json_ld_startdate_used':False,
          'walking_routes_generated':False, 'public_release_approved':False,
          'holiday_calendar_coverage_years':sorted(HK_GENERAL_HOLIDAYS),
          'daily_preview_counts':{k:{'events':len(v['today_events']),'exhibitions':len(v['current_exhibitions'])} for k,v in days.items()}}
    (out/'summary.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    print('\nCHAT Phase 02 Parsing Completed — offline snapshot only')
    print('Index candidates:', info['candidate_count'],'| Detail pages:',info['detail_count'],'| Needs review:',len(issues))
    print('All pages retrieved does NOT guarantee valid scheduling data or that the website is exhaustive.')
    print('No JSON-LD startDate used. No walking routes. NOT approved for visitor use.')
    if info['detail_count'] != info['candidate_count']:
        print('INCOMPLETE: detail pages missing; cannot conclude there are no events on a date.')
    for day,vals in info['daily_preview_counts'].items():
        print(day,':',vals['events'],'sample event slots /',vals['exhibitions'],'sample exhibitions')
    print('Output folder:',out.resolve())

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print('ERROR:',str(exc),file=sys.stderr)
        sys.exit(1)

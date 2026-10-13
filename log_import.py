"""Optional, offline WoW Forever log reader. Standard library only; no Lua execution."""
from __future__ import annotations

import argparse
from bisect import bisect_left
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

SCHEMA = 1
VERSION = (Path(__file__).with_name('VERSION')).read_text(encoding='utf-8').strip()
METRICS = ("abilities", "mobs", "toughest", "nearDeaths")
MAX_POINTS = 1_000_000
DEFAULT_INTERVAL = 10
# Reviewed direct-impact families. Multi-Shot projectiles in the local 70338
# logs land separately, up to 1.225 seconds after SPELL_CAST_SUCCESS. Do not
# group arbitrary damage by time: channels, DoTs and unmatched proc IDs are not
# a volley. Other listed bursts have a short immediate-impact window.
VOLLEY_WINDOWS = {2643: 3.0}
VOLLEY_WINDOWS.update(dict.fromkeys((1449, 8437, 8438, 8439, 10201, 10202,
    845, 7369, 11608, 11609, 20569, 1680,
    411128, 779, 780, 769, 9754, 9908, 407778, 407784), .5))
MAX_VOLLEY_TARGETS = 200
GAME_SETUP = "In WoW, press Enter to open chat. Type '/ding logs on' and press Enter.\nThen type '/reload' in chat and press Enter to save your character first."
SPACE = re.compile(r"(?:\s+|--[^\r\n]*)+")
NAMED = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*=")
SCALAR = re.compile(r"(?:true|false|nil)(?![A-Za-z_0-9])|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class LiteralReader:
    """Accept only the literal syntax emitted by WoW's SavedVariables writer."""

    def __init__(self, text):
        self.text, self.pos, self.items = text, 0, 0

    def space(self):
        while True:
            match = SPACE.match(self.text, self.pos)
            if not match:
                return
            self.pos = match.end()

    def take(self, token):
        self.space()
        if not self.text.startswith(token, self.pos):
            raise ValueError("Unsupported SavedVariables syntax")
        self.pos += len(token)

    def value(self, keep=True, depth=0):
        self.items += 1
        if depth > 96 or self.items > 2_000_000:
            raise ValueError("SavedVariables exceeds reader limits")
        self.space()
        if self.pos >= len(self.text):
            raise ValueError("Truncated SavedVariables")
        char = self.text[self.pos]
        if char == '{':
            self.pos += 1
            result, index = {}, 1
            while True:
                self.space()
                if self.text.startswith('}', self.pos):
                    self.pos += 1
                    return result if keep else None
                if self.text.startswith('[', self.pos):
                    self.pos += 1
                    key = self.value(True, depth + 1)
                    if not isinstance(key, (str, int, float)) or isinstance(key, bool):
                        raise ValueError("Invalid table key")
                    self.take(']'); self.take('=')
                else:
                    named = NAMED.match(self.text, self.pos)
                    if named:
                        key = named[1]; self.pos = named.end()
                    else:
                        key = index; index += 1
                value = self.value(keep, depth + 1)
                if keep:
                    if key in result:
                        raise ValueError("Duplicate SavedVariables key")
                    result[key] = value
                self.space()
                if self.text[self.pos:self.pos + 1] in (',', ';'):
                    self.pos += 1
                elif not self.text.startswith('}', self.pos):
                    raise ValueError("Invalid table separator")
        if char in ('"', "'"):
            self.pos += 1
            out = []
            while self.pos < len(self.text):
                c = self.text[self.pos]; self.pos += 1
                if c == char:
                    return ''.join(out) if keep else None
                if c == '\\':
                    if self.pos == len(self.text):
                        break
                    c = self.text[self.pos]; self.pos += 1
                    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v', '\\': '\\', '"': '"', "'": "'", '\n': '\n'}
                    if c.isdigit():
                        digits = c
                        while len(digits) < 3 and self.text[self.pos:self.pos + 1].isdigit():
                            digits += self.text[self.pos]; self.pos += 1
                        if int(digits) > 255:
                            raise ValueError("Invalid string escape")
                        c = chr(int(digits))
                    elif c in escapes:
                        c = escapes[c]
                    else:
                        raise ValueError("Unsupported string escape")
                if keep:
                    out.append(c)
            raise ValueError("Unterminated SavedVariables string")
        match = SCALAR.match(self.text, self.pos)
        if not match:
            raise ValueError("Executable or unsupported SavedVariables expression")
        self.pos = match.end()
        token = match[0]
        if token in ('true', 'false', 'nil'):
            return {'true': True, 'false': False, 'nil': None}[token]
        value = float(token) if any(c in token for c in '.eE') else int(token)
        if not math.isfinite(value):
            raise ValueError("Non-finite SavedVariables number")
        return value

    def context(self):
        result = None
        while True:
            self.space()
            if self.pos == len(self.text):
                return result
            match = NAMED.match(self.text, self.pos)
            if not match:
                raise ValueError("Unsupported SavedVariables assignment")
            self.pos = match.end()
            keep = match[1] == 'DingTrackerLogContext'
            value = self.value(keep)
            if keep:
                if result is not None:
                    raise ValueError("Duplicate log context")
                result = value


def array(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    return [value[k] for k in sorted(value) if isinstance(k, int)]


def timestamp(text):
    match = re.fullmatch(r"(\d{1,2}/\d{1,2}/\d{4} \d{2}:\d{2}:\d{2}\.\d+)([+-]\d{1,2})(?::?(\d{2}))?", text)
    if not match:
        raise ValueError("Unsupported log timestamp (full year and UTC offset required)")
    hours = int(match[2]); minutes = int(match[3] or 0)
    offset = dt.timedelta(hours=hours, minutes=minutes if hours >= 0 and not match[2].startswith('-') else -minutes)
    return dt.datetime.strptime(match[1], '%m/%d/%Y %H:%M:%S.%f').replace(tzinfo=dt.timezone(offset)).timestamp()


def integer(value, maximum=10**15):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if str(number) == str(value) and 0 <= number <= maximum else None
    except (ValueError, TypeError):
        return None


def read_logs(paths, characters):
    points, seen, stats = [], set(), {'files': 0, 'rejectedLines': 0, 'unsupportedFiles': 0}
    for path in paths:
        supported = False
        stats['files'] += 1
        with path.open('r', encoding='utf-8-sig', errors='strict') as stream:
            for line in stream:
                if not line.endswith('\n'):  # WoW may still be writing the last line.
                    break
                try:
                    when, payload = line.rstrip('\r\n').split('  ', 1)
                    fields = next(csv.reader([payload], strict=True))
                    event = fields[0]
                    if event == 'COMBAT_LOG_VERSION':
                        header = dict(zip(fields[2::2], fields[3::2]))
                        supported = fields[1] == '22' and header.get('BUILD_VERSION') == '1.60.1' and header.get('PROJECT_ID') == '18'
                        if not supported:
                            stats['unsupportedFiles'] += 1
                        continue
                    if not supported or len(fields) < 9:
                        continue
                    at = timestamp(when)
                    if len(fields) < 10:
                        continue
                    src, dst = fields[1], fields[5]
                    spell = event.startswith(('SPELL_', 'RANGE_', 'DAMAGE_SHIELD'))
                    start = 12 if spell else 9
                    if event == 'SPELL_CAST_SUCCESS' and src in characters and int(fields[3], 16) & 0x401 == 0x401:
                        spell_id = integer(fields[9], 10**8)
                        if spell_id in VOLLEY_WINDOWS and len(fields) >= 12:
                            points.append((at, 'cast', src, {'id': spell_id, 'name': fields[10]}))
                    if event in ('SPELL_DAMAGE', 'SPELL_PERIODIC_DAMAGE', 'RANGE_DAMAGE', 'DAMAGE_SHIELD', 'SWING_DAMAGE') and src in characters:
                        suffix = fields[:-1] if fields[-1] in ('ST', 'AOE') else fields
                        amount = integer(suffix[-10])
                        spell_id = integer(fields[9], 10**8) if spell else 6603
                        # Require the advanced health block as well as the ten-field damage suffix.
                        if len(suffix) >= start + 27 and amount and spell_id and int(fields[3], 16) & 0x401 == 0x401:
                            points.append((at, 'hit', src, {'id': spell_id, 'name': fields[10] if spell else 'Attack', 'amount': amount,
                                'critical': suffix[-3] == 'true', 'event': event, 'target': dst}))
                    if event in ('SPELL_DAMAGE', 'SPELL_PERIODIC_DAMAGE', 'RANGE_DAMAGE', 'DAMAGE_SHIELD', 'SPELL_HEAL', 'SPELL_PERIODIC_HEAL', 'SPELL_CAST_SUCCESS', 'SWING_DAMAGE', 'SWING_DAMAGE_LANDED') and len(fields) >= start + 17:
                        guid = fields[start]
                        hp, maximum = integer(fields[start + 2]), integer(fields[start + 3])
                        if guid in (src, dst) and maximum and hp is not None and hp <= maximum:
                            if guid in characters or guid.startswith('Creature-'):
                                points.append((at, 'health', guid, {'health': hp, 'maxHealth': maximum, 'name': fields[2] if guid == src else fields[6]}))
                    if event == 'UNIT_DIED' and dst in characters:
                        points.append((at, 'death', dst, {}))
                    if event == 'PARTY_KILL' and src in characters and dst.startswith('Creature-'):
                        points.append((at, 'kill', src, {'guid': dst, 'name': fields[6]}))
                except (ValueError, IndexError, csv.Error, OverflowError):
                    stats['rejectedLines'] += 1
                if len(points) > MAX_POINTS:
                    raise ValueError("Log selection exceeds one million observations; archive older logs first")
    unique = []
    for point in sorted(points, key=lambda p: (p[0], {'death': 0, 'cast': 1, 'health': 2, 'hit': 3, 'kill': 4}[p[1]])):
        key = (point[0], point[1], point[2], json.dumps(point[3], sort_keys=True))
        if key not in seen:
            seen.add(key); unique.append(point)
    stats['observations'] = len(unique)
    return unique, stats


def record_at(character, at, metric):
    for level, record in character.get('records', {}).items():
        if not isinstance(record, dict):
            continue
        start, end = record.get('startedAt'), record.get('endedAt')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not start + 1 <= at < end:
            continue
        for span in array(record.get('coverage', {}).get(metric)):
            if span.get('enabled') is True and span.get('startedAt', math.inf) + 1 <= at < span.get('endedAt', -math.inf):
                return int(level)
    return None


def highest_volleys(character, guid, points):
    """One observed direct impact per target, attached to a matching cast.

    Windows are deliberately bounded and spell-specific; they are not evidence
    of a shared cast GUID. Wait for the whole window in the log and saved context,
    and reject overlapping casts, repeat impacts or split tracking/level spans.
    """
    pending, best = {}, {}
    observed_until = max((p[0] for p in points), default=0)

    def finish(group):
        level = group['level']
        deadline = group['castAt'] + VOLLEY_WINDOWS[group['id']]
        if group['invalid'] or len(group['targets']) < 2 or deadline > observed_until or deadline > character['exportedAt']:
            return
        record = character['records'][level]
        # One uninterrupted enabled interval must cover the whole association
        # window, so a reload, ding or tracking pause cannot splice two volleys.
        if not any(span.get('enabled') is True and span['startedAt'] + 1 <= group['castAt']
            and deadline < span['endedAt'] for span in array(record['coverage'].get('abilities'))):
            return
        if not record['startedAt'] + 1 <= group['castAt'] < deadline < record['endedAt']:
            return
        amount = sum(hit['amount'] for hit in group['targets'].values())
        if not 0 < amount <= 10**15:
            return
        volley = {'id': group['id'], 'name': group['name'], 'amount': amount,
            'castAt': group['castAt'], 'at': group['lastAt'], 'window': VOLLEY_WINDOWS[group['id']],
            'targets': len(group['targets']), 'criticalHits': sum(hit.get('critical') is True for hit in group['targets'].values())}
        previous = best.get(level)
        if previous is None or (volley['amount'], -volley['id']) > (previous['amount'], -previous['id']):
            best[level] = volley

    for at, kind, entity, value in points:
        spell_id = value.get('id')
        if entity != guid or spell_id not in VOLLEY_WINDOWS:
            continue
        previous = pending.get(spell_id)
        if kind == 'cast':
            overlap = previous and at < previous['castAt'] + VOLLEY_WINDOWS[spell_id]
            if previous:
                previous['invalid'] |= bool(overlap)
                finish(previous)
            level = record_at(character, at, 'abilities')
            if level:
                pending[spell_id] = {'id': spell_id, 'name': value.get('name'), 'castAt': at,
                    'lastAt': at, 'level': level, 'targets': {}, 'invalid': bool(overlap)}
            else:
                pending.pop(spell_id, None)
        elif kind == 'hit' and previous and 0 <= at - previous['castAt'] < VOLLEY_WINDOWS[spell_id]:
            if value.get('event') not in ('SPELL_DAMAGE', 'RANGE_DAMAGE'):
                continue  # Never include periodic ticks, shields or melee swings.
            target = value.get('target')
            if (not isinstance(target, str) or not target.startswith(('Creature-', 'Player-', 'Pet-', 'Vehicle-'))
                or target in previous['targets'] or len(previous['targets']) >= MAX_VOLLEY_TARGETS
                or record_at(character, at, 'abilities') != previous['level']):
                previous['invalid'] = True
                continue
            previous['targets'][target] = value
            previous['lastAt'] = at
    for group in pending.values():
        finish(group)
    return best


def build_payload(context, points, now=None):
    if not isinstance(context, dict) or context.get('schema') != SCHEMA:
        raise ValueError("No supported log context. Enable imports in Ding Tracker, then /reload")
    validate_context(context)
    result = {'schema': SCHEMA, 'generatedAt': int(now or time.time()), 'characters': {}}
    report = {}
    for guid, character in context.get('characters', {}).items():
        if not isinstance(guid, str) or not guid.startswith('Player-') or character.get('enabled') is not True:
            continue
        levels, health, low_count, confirmed = {}, {}, 0, 0

        def bucket(level):
            return levels.setdefault(level, {'hits': [], 'volleys': [], 'health': [], 'closeCalls': []})

        markers = array(character.get('markers'))
        markers.sort(key=lambda m: m['at'])
        combined = [(m['at'], 'marker', guid, m) for m in markers] + points
        combined.sort(key=lambda p: (p[0], 0 if p[1] == 'marker' else 1))
        state, pending, combat_start, combat_end, previous_health = None, None, None, None, None
        cancellations = []
        for observation_index, (at, kind, entity, value) in enumerate(combined):
            if kind == 'marker':
                old = state
                state = value
                session_change = not old or old.get('session') != state.get('session')
                changed_level = old and old.get('level') != state.get('level')
                if session_change or changed_level or state.get('proof') is not True or state.get('duel') or not state.get('alive') or not state.get('enabled', {}).get('nearDeaths') or state.get('closed'):
                    pending, combat_start, combat_end, previous_health = None, None, None, None
                    cancellations.append(at)
                elif old and old.get('combat') is False and state.get('combat') is True:
                    pending, combat_start, combat_end = None, at, None
                elif old and old.get('combat') is True and state.get('combat') is False:
                    combat_end = at
                    if pending and pending.get('recoveredAt'):
                        # The exit marker may sort before damage from its own
                        # second. Such a fall could have happened before exit,
                        # so require a later unambiguous recovery instead.
                        last_hp = previous_health['health'] if previous_health else pending['recoveredHealth']
                        next_index = observation_index + 1
                        while next_index < len(combined) and combined[next_index][0] < at + 1:
                            _, next_kind, next_entity, next_value = combined[next_index]
                            if next_kind == 'health' and next_entity == guid:
                                next_hp, next_maximum = next_value['health'], next_value['maxHealth']
                                if next_hp < last_hp and 0 < next_hp / next_maximum <= .1:
                                    for key in ('recoveredAt', 'recoveredHealth', 'recoveredMaxHealth'):
                                        pending.pop(key, None)
                                    break
                                last_hp = next_hp
                            next_index += 1
                    if pending and pending.get('recoveredAt'):
                        finish_close(guid, pending, combat_start, combat_end, bucket)
                        confirmed += 1; pending = None
                continue
            if kind == 'health' and entity.startswith('Creature-'):
                health[entity] = (at, value)
                continue
            if entity != guid:
                continue
            if kind == 'hit':
                level = record_at(character, at, 'abilities')
                if level:
                    hits = bucket(level)['hits']
                    hit = dict(value, at=at)
                    if not hits or hit['amount'] > hits[0]['amount']:
                        hits[:] = [hit]
            elif kind == 'kill':
                sample = health.pop(value['guid'], None)
                level = record_at(character, at, 'mobs')
                if sample and level and record_at(character, at, 'toughest') == level and 0 <= at - sample[0] <= 120 and record_at(character, sample[0], 'mobs') == level:
                    npc = integer(value['guid'].split('-')[-2], 10**8)
                    if npc:
                        hp = dict(sample[1], guid=value['guid'], id=npc, at=at)
                        entries = bucket(level)['health']
                        previous = next((h for h in entries if h['id'] == npc), None)
                        if previous is None and len(entries) < 256:
                            entries.append(hp)
                        elif previous and hp['maxHealth'] > previous['maxHealth']:
                            entries[entries.index(previous)] = hp
            elif kind == 'death':
                pending, previous_health, combat_start, combat_end = None, None, None, None
                cancellations.append(at)
            elif kind == 'health':
                hp, maximum = value['health'], value['maxHealth']
                if 0 < hp / maximum <= .1:
                    low_count += 1
                if hp == 0:
                    pending, previous_health, combat_start, combat_end = None, None, None, None
                    cancellations.append(at)
                    continue
                valid = state and state.get('proof') is True and not state.get('closed') and not state.get('duel') and state.get('alive') and at < character.get('exportedAt', 0)
                level = record_at(character, at, 'nearDeaths')
                if not valid or not level or state.get('level') != level or not state.get('enabled', {}).get('nearDeaths'):
                    pending, previous_health = None, None
                    continue
                # Markers have one-second precision, so these samples cannot
                # prove which side of a transition they belong to. Ignore them
                # as crossings/minima/recoveries, but retain their actual HP as
                # the baseline for a later unambiguous health change.
                if int(at) == state['at']:
                    previous_health = value
                    continue
                # A first low sample can be the only advanced-log snapshot of a
                # genuine fall. With an earlier valid sample, require both an
                # actual HP decrease and a crossing; max-HP changes are not damage.
                falling = previous_health is None or hp < previous_health['health']
                crossing = previous_health is None or previous_health['health'] / previous_health['maxHealth'] > .1
                if state.get('combat') and combat_start is not None and not pending and hp / maximum <= .1 and falling and crossing:
                    pending = dict(value, timestamp=at, level=level)
                elif pending:
                    if state.get('combat') and falling and hp / maximum <= .1:
                        if hp / maximum < pending['health'] / pending['maxHealth']:
                            pending.update(value, timestamp=at)
                        for key in ('recoveredAt', 'recoveredHealth', 'recoveredMaxHealth'):
                            pending.pop(key, None)
                    if not pending.get('recoveredAt') and previous_health is not None and hp > previous_health['health'] and hp > pending['health'] and hp / maximum >= .35:
                        pending.update(recoveredAt=at, recoveredHealth=hp, recoveredMaxHealth=maximum)
                        if combat_end is not None:
                            finish_close(guid, pending, combat_start, combat_end, bucket)
                            confirmed += 1; pending = None
                previous_health = value
        # Combat-ending markers have integer timestamps. A death or exclusion
        # in that same second may have ended combat, rather than proving survival.
        def uninterrupted_close(close):
            index = bisect_left(cancellations, close['timestamp'])
            return index == len(cancellations) or (cancellations[index] >= close['combatEndedAt'] + 1 and cancellations[index] > close['recoveredAt'])

        for level, volley in highest_volleys(character, guid, points).items():
            bucket(level)['volleys'] = [volley]
        for items in levels.values():
            items['closeCalls'] = [c for c in items['closeCalls'] if uninterrupted_close(c)]
        confirmed = sum(len(v['closeCalls']) for v in levels.values())
        result['characters'][guid] = {'exportedAt': character.get('exportedAt'), 'levels': levels}
        report[guid] = {'levels': len(levels), 'highestHit': max((v['hits'][0]['amount'] for v in levels.values() if v['hits']), default=0),
            'highestVolley': max((v['volleys'][0]['amount'] for v in levels.values() if v['volleys']), default=0),
            'mobHealthRecords': sum(len(v['health']) for v in levels.values()), 'lowHealthSamples': low_count, 'confirmedCloseCalls': confirmed}
    return result, report


def finish_close(guid, pending, start, end, bucket):
    identity = f"{guid}:{pending['level']}:{pending['timestamp']:.3f}:{pending['health']}:{pending['maxHealth']}"
    entry = {k: pending[k] for k in ('health', 'maxHealth', 'timestamp', 'recoveredAt', 'recoveredHealth', 'recoveredMaxHealth')}
    entry.update(percent=100 * entry['health'] / entry['maxHealth'], episodeID='log:' + hashlib.sha256(identity.encode()).hexdigest()[:32], duelExcluded=True, combatStartedAt=start, combatEndedAt=end)
    rows = bucket(pending['level'])['closeCalls']
    if len(rows) < 256:
        rows.append(entry)


def lua_literal(value):
    if value is True: return 'true'
    if value is False: return 'false'
    if value is None: return 'nil'
    if isinstance(value, (int, float)) and math.isfinite(value): return repr(value)
    if isinstance(value, str):
        return '"' + ''.join('\\%03d' % ord(c) if ord(c) < 32 or ord(c) == 127 else '\\' + c if c in '\\"' else c for c in value) + '"'
    if isinstance(value, list): return '{' + ','.join(lua_literal(v) for v in value) + '}'
    if isinstance(value, dict): return '{' + ','.join('[' + lua_literal(k) + ']=' + lua_literal(v) for k, v in value.items()) + '}'
    raise ValueError("Unsupported output value")


def validate_context(context):
    characters = context.get('characters')
    if not isinstance(characters, dict) or len(characters) > 1000:
        raise ValueError("Invalid character context")
    for guid, character in characters.items():
        if not isinstance(guid, str) or not re.fullmatch(r'Player-[A-Za-z0-9-]{1,100}', guid) or not isinstance(character, dict):
            raise ValueError("Invalid character identity")
        if not isinstance(character.get('enabled'), bool) or integer(character.get('exportedAt'), 10**11) is None:
            raise ValueError("Invalid context export time or opt-in")
        if not character['enabled']:
            continue
        records = character.get('records')
        if not isinstance(records, dict) or len(records) > 1000:
            raise ValueError("Invalid level context")
        for level, record in records.items():
            if integer(level, 1000) is None or level == 0 or not isinstance(record, dict):
                raise ValueError("Invalid level")
            for key in ('startedAt', 'endedAt'):
                if integer(record.get(key), 10**11) is None:
                    raise ValueError("Invalid level timestamps")
            if not isinstance(record.get('coverage'), dict):
                raise ValueError("Invalid tracking context")
            for spans in record['coverage'].values():
                if not isinstance(spans, (dict, list)) or len(spans) > 8192:
                    raise ValueError("Invalid tracking intervals")
                for span in array(spans):
                    if not isinstance(span, dict) or not isinstance(span.get('enabled'), bool) or any(integer(span.get(k), 10**11) is None for k in ('startedAt', 'endedAt')):
                        raise ValueError("Invalid tracking interval")
        markers = character.get('markers')
        if not isinstance(markers, (list, dict)) or len(markers) > 2048:
            raise ValueError("Invalid combat markers")
        for marker in array(markers):
            if not isinstance(marker, dict) or integer(marker.get('at'), 10**11) is None or integer(marker.get('level'), 1000) is None or not isinstance(marker.get('session'), str) or not isinstance(marker.get('enabled'), dict):
                raise ValueError("Invalid combat marker")
            if any(not isinstance(marker.get(k), bool) for k in ('combat', 'alive', 'duel', 'proof', 'closed')):
                raise ValueError("Incomplete combat marker")
            if any(not isinstance(marker['enabled'].get(k), bool) for k in METRICS):
                raise ValueError("Incomplete tracking marker")


def run_once(wow, dry_run=False):
    wow = wow.resolve(strict=True)
    target = wow / 'Interface' / 'AddOns' / 'DingTracker_LogData' / 'Data.lua'
    if not target.is_file() or target.resolve().parent != wow / 'Interface' / 'AddOns' / 'DingTracker_LogData' or target.is_symlink():
        raise ValueError("Install the DingTracker_LogData companion in this client first")
    contexts, errors = {'schema': SCHEMA, 'characters': {}}, []
    for saved in sorted((wow / 'WTF' / 'Account').glob('*/SavedVariables/DingTracker.lua')):
        try:
            if saved.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("SavedVariables exceeds 64 MB reader limit")
            context = LiteralReader(saved.read_text(encoding='utf-8-sig')).context()
            if isinstance(context, dict) and context.get('schema') == SCHEMA:
                validate_context(context)
                for guid, character in context.get('characters', {}).items():
                    old = contexts['characters'].get(guid)
                    if not old or character.get('exportedAt', 0) > old.get('exportedAt', 0):
                        contexts['characters'][guid] = character
        except (ValueError, UnicodeError) as error:
            errors.append(f'{saved.parent.parent.name}: {error}')
    characters = {guid: c for guid, c in contexts['characters'].items() if c.get('enabled') is True}
    if not characters:
        raise ValueError("No enabled characters saved. " + GAME_SETUP + ' ' + '; '.join(errors))
    points, stats = read_logs(sorted((wow / 'Logs').glob('WoWCombatLog*.txt')), characters)
    payload, report = build_payload(contexts, points)
    output = '-- Generated locally by Ding Tracker Log Helper. No SavedVariables are modified.\nDingTrackerLogPayload = ' + lua_literal(payload) + '\n'
    if len(output.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError("Import exceeds 2 MB; archive older logs first")
    if not dry_run:
        # The only write is the companion payload; replacement is atomic.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent, suffix='.tmp', delete=False, newline='\n') as stream:
                temporary = Path(stream.name); stream.write(output)
            os.replace(temporary, target)
        finally:
            if temporary and temporary.exists(): temporary.unlink()
    return {'dryRun': dry_run, 'statistics': stats, 'characters': report, 'warnings': errors, 'output': str(target)}


def discover_clients(environment=None, nearby=None):
    """Look only in common install locations; folder selection stays explicit."""
    environment = os.environ if environment is None else environment
    bases = {Path(environment[key]) / 'World of Warcraft'
             for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432')
             if environment.get(key)}
    nearby = [Path.cwd(), Path(sys.executable).parent] if nearby is None else nearby
    candidates = set()
    for base in bases:
        try:
            candidates.update(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
    candidates.update(Path(p) for p in nearby)
    found = set()
    for client in candidates:
        try:
            if (client / 'Interface/AddOns/DingTracker_LogData/Data.lua').is_file():
                found.add(client.resolve())
        except OSError:
            continue
    return sorted(found, key=lambda p: str(p).casefold())


def choose_client():
    print(f'Ding Tracker Log Helper {VERSION}\n')
    print(GAME_SETUP)
    print('Here in the helper, choose the client folder containing Interface, Logs and WTF.\n')
    clients = discover_clients()
    for number, client in enumerate(clients, 1):
        print(f'  {number}. {client}')
    while True:
        if len(clients) == 1:
            prompt = '\nPress Enter to use folder 1, or paste another client folder (Q to exit): '
        elif clients:
            prompt = '\nEnter a number or paste the client folder (Q to exit): '
        else:
            prompt = '\nPaste the client folder (Enter to exit): '
        try:
            answer = input(prompt).strip().strip('"').strip("'")
        except EOFError:
            return None
        if answer.casefold() == 'q' or (not answer and not clients):
            return None
        if not answer:
            if len(clients) != 1:
                print('Several clients were found. Choose the folder you play on.')
                continue
            answer = '1'
        if answer.isdecimal() and clients:
            number = int(answer)
            if not 1 <= number <= len(clients):
                print('Choose one of the listed numbers.')
                continue
            client = clients[number - 1]
        else:
            client = Path(os.path.expandvars(answer)).expanduser()
        if not (client / 'Interface/AddOns/DingTracker_LogData/Data.lua').is_file():
            print('DingTracker_LogData was not found there. Install the addon first and choose its client folder.')
            continue
        return client.resolve()


def show_report(report, interactive=False):
    if not interactive:
        print(json.dumps(report, indent=2), flush=True)
    else:
        print('\nScan complete.', flush=True)
        for guid, result in report['characters'].items():
            print(f"  {guid}: highest single-target hit {result['highestHit']}, highest AoE volley {result['highestVolley']}, {result['mobHealthRecords']} mob HP records, {result['confirmedCloseCalls']} close calls.", flush=True)
        for warning in report['warnings']:
            print(f'  Warning: {warning}', flush=True)
    if not report['dryRun']:
        print("Import written. In WoW chat, enter '/reload' to load it.", flush=True)


def interval_seconds(value):
    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('Choose an interval from 5 to 3600 seconds') from None
    if not 5 <= interval <= 3600:
        raise argparse.ArgumentTypeError('Choose an interval from 5 to 3600 seconds')
    return interval


class WatchControl:
    def __init__(self, interval):
        self.interval = interval
        self.stopped, self.wake = threading.Event(), threading.Event()
        self._force = False
        self._lock = threading.Lock()

    def stop(self):
        self.stopped.set()
        self.wake.set()

    def set_interval(self, interval):
        self.interval = interval_seconds(interval)
        self.wake.set()

    def scan_now(self):
        with self._lock:
            self._force = True
        self.wake.set()

    def consume_scan(self):
        with self._lock:
            force, self._force = self._force, False
        return force

    def wait(self):
        self.wake.wait(self.interval)
        self.wake.clear()


def watch_files(wow, dry_run=False, watch=False, interactive=False, control=None, status=None):
    control = control or WatchControl(DEFAULT_INTERVAL)
    previous = None
    while not control.stopped.is_set():
        force = control.consume_scan()
        try:
            files = list((wow / 'Logs').glob('WoWCombatLog*.txt')) + list((wow / 'WTF' / 'Account').glob('*/SavedVariables/DingTracker.lua'))
            target = wow / 'Interface/AddOns/DingTracker_LogData/Data.lua'
            if target.is_file():
                files.append(target)
            fingerprint = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(files))
            if force or fingerprint != previous:
                # Remember failed attempts too: unchanged files need no repeated parsing.
                previous = fingerprint
                report = run_once(wow, dry_run)
                show_report(report, interactive)
                if status:
                    status('Scan complete - enter /reload in WoW chat')
                # Our own atomic output replacement should not trigger another scan.
                if not dry_run:
                    changed = target.stat()
                    previous = tuple((entry[0], changed.st_size, changed.st_mtime_ns)
                                     if entry[0] == str(target) else entry for entry in fingerprint)
        except (OSError, ValueError, UnicodeError) as error:
            print(f'Ding Tracker Log Helper: {error}', file=sys.stderr, flush=True)
            if status:
                status(str(error).splitlines()[0])
            if not watch:
                return 1
        if not watch:
            return 0
        control.wait()
    return 0


def launch_background(args):
    import subprocess
    if not (args.wow / 'Interface/AddOns/DingTracker_LogData/Data.lua').is_file():
        raise OSError('Install the DingTracker_LogData addon first and select its client folder.')
    from windows_tray import find_worker
    if find_worker(args.wow):
        print('The helper is already running for this client. Right-click its tray icon for controls.', flush=True)
        return 0
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command.append(str(Path(__file__).resolve()))
    command += ['--wow', str(args.wow.resolve()), '--interval', str(args.interval), '--tray-worker']
    if args.dry_run:
        command.append('--dry-run')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    environment = {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}
    worker = subprocess.Popen(command, env=environment, startupinfo=startup,
                              creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    for _ in range(100):
        if find_worker(args.wow):
            print(f'Running in the Windows tray. Checks every {args.interval} seconds.\nRight-click the Ding Tracker icon (possibly under the ^ arrow) for Show console, Scan now, interval choices or Exit helper.', flush=True)
            return 0
        if worker.poll() is not None:
            raise OSError('Background startup failed. Run with --watch in a console to inspect the error.')
        time.sleep(0.1)
    worker.terminate()
    worker.wait(timeout=5)
    raise OSError('Background startup timed out. Run with --watch in a console instead.')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if '--journal-transfer' in argv or '--journal' in argv:
        from journal_transfer import main as journal_main
        return journal_main([value for value in argv if value not in ('--journal-transfer', '--journal')])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=f'Ding Tracker Log Helper {VERSION}')
    parser.add_argument('--journal-transfer', '--journal', action='store_true', help='Open the journal file/clipboard transfer window; add --export/--import/--copy/--paste for command-line operations')
    parser.add_argument('--wow', type=Path, help='Client folder containing Interface, Logs and WTF')
    parser.add_argument('--dry-run', action='store_true', help='Report results without changing any files')
    parser.add_argument('--interval', type=interval_seconds, default=DEFAULT_INTERVAL, help=f'Seconds between checks (5-3600; default {DEFAULT_INTERVAL})')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--watch', '--foreground', action='store_true', help='Keep checking in the foreground console')
    mode.add_argument('--once', action='store_true', help='Scan once and exit instead of starting background checks')
    mode.add_argument('--background', action='store_true', help='Watch in the Windows tray (default on Windows)')
    mode.add_argument('--stop', action='store_true', help='Stop the background helper for this --wow folder')
    mode.add_argument('--status', action='store_true', help='Check whether a background helper is running for this --wow folder')
    mode.add_argument('--tray-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    interactive = not argv and sys.stdin.isatty()
    if not args.wow:
        if not interactive:
            parser.error('--wow is required when not launched interactively')
        args.wow = choose_client()
        if args.wow is None:
            return 0
    # A dry run stays a single scan unless its mode is explicitly selected.
    # Non-Windows source launches retain one-shot behavior; the tray is Windows-only.
    if not (args.watch or args.once or args.background or args.tray_worker or args.stop or args.status or args.dry_run):
        args.background = os.name == 'nt'
    if (args.background or args.tray_worker or args.stop or args.status) and os.name != 'nt':
        parser.error('Background/tray controls are available on Windows only')
    tray = None
    try:
        if args.stop or args.status:
            from windows_tray import stop_worker, worker_interval
            if args.stop:
                print('Stop requested.' if stop_worker(args.wow) else 'Helper is not running.')
            else:
                interval = worker_interval(args.wow)
                print(f'Helper is running. Checks every {interval} seconds.' if interval else 'Helper is not running.')
            return 0
        if args.background:
            return launch_background(args)
        control = WatchControl(args.interval)
        if args.tray_worker:
            from windows_tray import Tray
            tray = Tray(args.wow, control)
            tray.start()
            args.watch = interactive = True
        if args.watch:
            print(f'\nWatching {args.wow}\nChecks every {args.interval} seconds. Press Ctrl+C or close the console to stop.', flush=True)
        return watch_files(args.wow, args.dry_run, args.watch, interactive, control, tray.update if tray else None)
    except OSError as error:
        print(f'Ding Tracker Log Helper: {error}', file=sys.stderr, flush=True)
        return 1
    finally:
        if tray:
            tray.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

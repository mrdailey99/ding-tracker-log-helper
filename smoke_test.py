"""Verify a Windows release ZIP on synthetic data, without Python on its PATH."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time
import zipfile


def check(archive):
    with tempfile.TemporaryDirectory(prefix='ding-helper-smoke-') as folder:
        root = Path(folder)
        bundle = root / 'bundle'
        with zipfile.ZipFile(archive) as zipped:
            expected = {'DingTracker-LogHelper.exe', 'README.md', 'CHANGELOG.md', 'LICENSE-Python.txt',
                        'LICENSE-PyInstaller.txt', 'LICENSE-DingTracker-LogHelper.txt', 'BUILD-INFO.json', 'SHA256SUMS.txt'}
            assert set(zipped.namelist()) == expected, 'Unexpected release files'
            zipped.extractall(bundle)
        exe = bundle / 'DingTracker-LogHelper.exe'
        checksum = (bundle / 'SHA256SUMS.txt').read_text().split()[0]
        assert hashlib.sha256(exe.read_bytes()).hexdigest() == checksum
        zip_checksum = Path(str(archive) + '.sha256').read_text().split()[0]
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == zip_checksum
        pe = exe.read_bytes()
        offset = struct.unpack_from('<I', pe, 0x3c)[0]
        assert pe[:2] == b'MZ' and pe[offset:offset + 4] == b'PE\0\0'
        assert struct.unpack_from('<H', pe, offset + 4)[0] == 0x8664, 'Expected x64 PE'
        environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('PYTHON')
                       and k.casefold() not in ('programfiles', 'programfiles(x86)', 'programw6432')}
        system = Path(os.environ['SystemRoot'])
        environment['PATH'] = os.pathsep.join([str(system / 'System32'), str(system)])

        def run(*args, code=0):
            result = subprocess.run([str(exe), *map(str, args)], env=environment, cwd=root,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=60)
            assert result.returncode == code, (result.returncode, result.stdout, result.stderr)
            return result

        version = json.loads((bundle / 'BUILD-INFO.json').read_text())['helperVersion']
        assert version in run('--version').stdout
        help_text = run('--help').stdout
        assert all(option in help_text for option in ('--dry-run', '--interval', '--background', '--stop', '--status'))
        assert '5 to 3600' in run('--wow', root, '--watch', '--interval', '0', code=2).stderr
        assert '--wow is required' in run('--watch', code=2).stderr
        # Windows considers the NUL device a TTY. EOF must exit quietly, too.
        run()
        client = root / 'Synthetic WoW Client'
        target = client / 'Interface/AddOns/DingTracker_LogData/Data.lua'
        target.parent.mkdir(parents=True); target.write_text('blank\n')
        saved = client / 'WTF/Account/TEST/SavedVariables/DingTracker.lua'
        saved.parent.mkdir(parents=True)
        guid = 'Player-1-ABC'
        # Broad synthetic interval: these times and identities never come from the user's files.
        saved.write_text('''DingTrackerDB = { ["sentinel"] = "must not change" }
DingTrackerLogContext = { ["schema"] = 1, ["characters"] = {
  ["Player-1-ABC"] = { ["enabled"] = true, ["exportedAt"] = 1791390000,
    ["markers"] = {}, ["records"] = { [9] = {
      ["startedAt"] = 1791384000, ["endedAt"] = 1791390000,
      ["coverage"] = { ["abilities"] = {{ ["enabled"] = true,
        ["startedAt"] = 1791384000, ["endedAt"] = 1791390000 }} }
    }} }
} }
''', encoding='utf-8')
        log = client / 'Logs/WoWCombatLog-test.txt'
        log.parent.mkdir()
        fields = ['SPELL_DAMAGE', guid, 'Synthetic Player', '0x511', '0x0',
                  'Creature-0-1-2-3-123-000001', 'Synthetic Wolf', '0xa48', '0x0',
                  '8044', 'Earth Shock', '0x8', guid, '0000', '15', '234']
        fields += ['0'] * 13 + ['51', '51', '-1', '8', '0', '0', '7', 'true', 'false', 'false', 'ST']
        line = io.StringIO(); csv.writer(line, lineterminator='\n').writerow(fields)
        log.write_text('10/7/2026 10:01:00.000-5  COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,0,BUILD_VERSION,1.60.1,PROJECT_ID,18\n'
                       + '10/7/2026 10:01:15.714-5  ' + line.getvalue(), encoding='utf-8')
        before = {p.relative_to(client).as_posix(): p.read_bytes() for p in client.rglob('*') if p.is_file()}
        report = json.loads(run('--wow', client, '--dry-run').stdout)
        assert report['dryRun'] and report['characters'][guid]['highestHit'] == 51, report
        assert before == {p.relative_to(client).as_posix(): p.read_bytes() for p in client.rglob('*') if p.is_file()}
        result = run('--wow', client)
        report, _ = json.JSONDecoder().raw_decode(result.stdout)
        assert report['characters'][guid]['highestHit'] == 51
        assert 'DingTrackerLogPayload' in target.read_text() and '["amount"]=51' in target.read_text().replace(' ', '')
        after = {p.relative_to(client).as_posix(): p.read_bytes() for p in client.rglob('*') if p.is_file()}
        assert set(after) == set(before)
        assert [name for name in before if before[name] != after[name]] == ['Interface/AddOns/DingTracker_LogData/Data.lua']
        assert 'Install the DingTracker_LogData' in run('--wow', root, code=1).stderr

        def literal(value):
            if value is True: return 'true'
            if value is False: return 'false'
            if value is None: return 'nil'
            if isinstance(value, str): return json.dumps(value)
            if isinstance(value, (int, float)): return str(value)
            if isinstance(value, list): return '{' + ','.join(literal(v) for v in value) + '}'
            return '{' + ','.join('[' + literal(k) + ']=' + literal(v) for k, v in value.items()) + '}'

        def close_case(name, samples, expected):
            # Exercise the frozen parser through its real CLI and disk format.
            # This fixture does not import the source parser or user files.
            case = root / name
            output = case / 'Interface/AddOns/DingTracker_LogData/Data.lua'
            output.parent.mkdir(parents=True); output.write_text('blank\n')
            snapshot = case / 'WTF/Account/TEST/SavedVariables/DingTracker.lua'
            snapshot.parent.mkdir(parents=True)
            base = 1791385260
            enabled = {key: True for key in ('nearDeaths', 'abilities', 'mobs', 'toughest')}
            markers = [dict(at=base + second, session='synthetic', level=9, combat=combat,
                            alive=True, duel=False, closed=False, proof=True, enabled=enabled)
                       for second, combat in ((0, False), (2, True), (10, False))]
            coverage = {key: [dict(startedAt=base - 100, endedAt=base + 20, enabled=True)] for key in enabled}
            context = dict(schema=1, characters={guid: dict(enabled=True, exportedAt=base + 20,
                markers=markers, records={9: dict(startedAt=base - 100, endedAt=base + 20, coverage=coverage)})})
            original = 'DingTrackerLogContext=' + literal(context)
            snapshot.write_text(original, encoding='utf-8')
            case_log = case / 'Logs/WoWCombatLog-close.txt'
            case_log.parent.mkdir()
            rows = ['10/7/2026 10:01:00.000-5  COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,0,BUILD_VERSION,1.60.1,PROJECT_ID,18\n']
            for second, hp, maximum in samples:
                sample_fields = fields.copy()
                sample_fields[14:16] = [str(hp), str(maximum)]
                sample_line = io.StringIO(); csv.writer(sample_line, lineterminator='\n').writerow(sample_fields)
                rows.append(f'10/7/2026 10:01:{second:06.3f}-5  ' + sample_line.getvalue())
            case_log.write_text(''.join(rows), encoding='utf-8')
            result, _ = json.JSONDecoder().raw_decode(run('--wow', case).stdout)
            assert result['characters'][guid]['confirmedCloseCalls'] == expected, (name, result)
            first_payload = output.read_bytes()
            repeated, _ = json.JSONDecoder().raw_decode(run('--wow', case).stdout)
            assert repeated['characters'][guid]['confirmedCloseCalls'] == expected, (name, repeated)
            # A new export timestamp is expected; episode identities and evidence must remain stable.
            def stable_payload(payload):
                return re.sub(rb'\["generatedAt"\]\s*=\s*\d+', b'["generatedAt"]=0', payload)
            assert stable_payload(output.read_bytes()) == stable_payload(first_payload), 'Repeated import changed episode evidence'
            assert snapshot.read_text(encoding='utf-8') == original, 'Close-call scan changed SavedVariables'

        close_case('Maximum-only crossing', [(3.5, 12, 100), (4.5, 12, 130), (8.5, 50, 130)], 0)
        close_case('Heal before exit', [(3.5, 15, 234), (8.5, 99, 234)], 1)
        close_case('Heal after exit', [(3.5, 15, 234), (12.5, 99, 234)], 1)
        close_case('Maximum-only recovery', [(3.5, 8, 100), (6.5, 20, 100), (8.5, 20, 50)], 0)
        close_case('Low health in exit second', [(3.5, 8, 100), (8.5, 40, 100), (10.5, 5, 100)], 0)
        close_case('Zero HP at recovery time', [(3.5, 15, 234), (11.5, 99, 234), (11.5, 0, 234)], 0)

        import windows_tray
        user, _, _ = windows_tray.api()
        try:
            assert 'not running' in run('--wow', client, '--status').stdout
            assert 'Running in the Windows tray' in run('--wow', client, '--background', '--interval', '300').stdout
            assert 'every 300 seconds' in run('--wow', client, '--status').stdout
            assert 'already running' in run('--wow', client, '--background').stdout
            window = windows_tray.find_worker(client)
            assert window
            # Use native window commands for this temporary helper only.
            assert user.PostMessageW(window, 0x111, 200, 0)
            for _ in range(50):
                if windows_tray.worker_interval(client) == 30: break
                time.sleep(0.1)
            assert windows_tray.worker_interval(client) == 30
            assert user.PostMessageW(window, 0x111, 203, 0)
            for _ in range(50):
                if windows_tray.worker_interval(client) == 300: break
                time.sleep(0.1)
            assert windows_tray.worker_interval(client) == 300
            # A forced scan works immediately even with a five-minute interval.
            before_scan = target.stat().st_mtime_ns
            assert user.PostMessageW(window, 0x111, 102, 0)
            for _ in range(50):
                if target.stat().st_mtime_ns != before_scan: break
                time.sleep(0.1)
            assert target.stat().st_mtime_ns != before_scan
            assert saved.read_bytes() == before['WTF/Account/TEST/SavedVariables/DingTracker.lua']
        finally:
            run('--wow', client, '--stop')
            for _ in range(50):
                if not windows_tray.find_worker(client, ready=False): break
                time.sleep(0.1)
            assert not windows_tray.find_worker(client, ready=False), 'Background helper did not stop'
            # The frozen bootloader can still be cleaning up after its window exits.
            # Wait for it to release this temporary copy, never the release artifact.
            for _ in range(100):
                try:
                    exe.unlink()
                    break
                except PermissionError:
                    time.sleep(0.1)
            assert not exe.exists(), 'Background executable remained in use after Exit'
        print('PASS: Windows x64 bundle, SHA-256, CLI validation, dry run, 51-damage import, six close-call scenarios, repeat imports, SavedVariables preservation, tray startup, duplicate prevention, live intervals, Scan now and Exit; no Python on executable PATH.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    check(parser.parse_args().archive.resolve(strict=True))

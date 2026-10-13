"""Verify a Windows release ZIP on synthetic data, without Python on its PATH."""
import argparse
import base64
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
import zlib


def check(archive):
    with tempfile.TemporaryDirectory(prefix='ding-helper-smoke-') as folder:
        root = Path(folder)
        bundle = root / 'bundle'
        with zipfile.ZipFile(archive) as zipped:
            expected = {'DingTracker-LogHelper.exe', 'README.md', 'CHANGELOG.md', 'LICENSE-Python.txt',
                        'LICENSE-PyInstaller.txt', 'LICENSE-DingTracker-LogHelper.txt', 'BUILD-INFO.json', 'SHA256SUMS.txt',
                        'Start-JournalTransfer.cmd'}
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
        assert all(option in help_text for option in ('--dry-run', '--interval', '--background', '--foreground', '--once', '--stop', '--status'))
        assert 'default 10' in help_text
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
        result = run('--wow', client, '--once')
        report, _ = json.JSONDecoder().raw_decode(result.stdout)
        assert report['characters'][guid]['highestHit'] == 51
        assert 'DingTrackerLogPayload' in target.read_text() and '["amount"]=51' in target.read_text().replace(' ', '')
        after = {p.relative_to(client).as_posix(): p.read_bytes() for p in client.rglob('*') if p.is_file()}
        assert set(after) == set(before)
        assert [name for name in before if before[name] != after[name]] == ['Interface/AddOns/DingTracker_LogData/Data.lua']
        # The frozen helper must keep individual damage and a delayed volley
        # distinct, even when the unmitigated column contains larger values.
        cast = f'10/7/2026 10:01:14.714-5  SPELL_CAST_SUCCESS,{guid},Synthetic Player,0x511,0x0,nil,nil,0x0,0x0,2643,Multi-Shot,0x1\n'
        volley_lines = []
        for index, amount in enumerate((51, 50, 53)):
            hit_fields = fields.copy()
            hit_fields[5] = f'Creature-0-1-2-3-123-00000{index + 1}'
            hit_fields[9:11] = ['2643', 'Multi-Shot']
            hit_fields[-11:-9] = [str(amount), '74']
            output = io.StringIO(); csv.writer(output, lineterminator='\n').writerow(hit_fields)
            volley_lines.append(f'10/7/2026 10:01:15.{714 + index * 100:03d}-5  ' + output.getvalue())
        log.write_text('10/7/2026 10:01:00.000-5  COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,0,BUILD_VERSION,1.60.1,PROJECT_ID,18\n'
            + cast + ''.join(volley_lines) + '10/7/2026 10:01:20.000-5  ' + line.getvalue(), encoding='utf-8')
        result = run('--wow', client, '--once')
        report, _ = json.JSONDecoder().raw_decode(result.stdout)
        assert report['characters'][guid]['highestHit'] == 53
        assert report['characters'][guid]['highestVolley'] == 154
        assert '["targets"]=3' in target.read_text().replace(' ', '')
        assert saved.read_bytes() == before[saved.relative_to(client).as_posix()]
        assert 'Install the DingTracker_LogData' in run('--wow', root, '--once', code=1).stderr
        assert 'Install the DingTracker_LogData' in run('--wow', root, code=1).stderr

        def literal(value):
            if value is True: return 'true'
            if value is False: return 'false'
            if value is None: return 'nil'
            if isinstance(value, str): return json.dumps(value)
            if isinstance(value, (int, float)): return str(value)
            if isinstance(value, list): return '{' + ','.join(literal(v) for v in value) + '}'
            return '{' + ','.join('[' + literal(k) + ']=' + literal(v) for k, v in value.items()) + '}'

        # Exercise the new frozen transport CLI on its own synthetic client.
        # A literal-only LZSS fixture does not depend on the source compressor.
        def typed(value):
            if isinstance(value, bool): return b'b1' if value else b'b0'
            if isinstance(value, (int, float)):
                raw = str(value).encode('ascii')
                return b'n' + str(len(raw)).encode() + b':' + raw
            if isinstance(value, str):
                raw = value.encode('utf-8').hex().upper().encode('ascii')
                return b's' + str(len(raw)).encode() + b':' + raw
            return b't' + str(len(value)).encode() + b':' + b''.join(typed(k) + typed(v) for k, v in value.items())

        journal_client = root / 'Synthetic Journal Client'
        journal_saved = journal_client / 'WTF/Account/TEST/SavedVariables/DingTracker.lua'
        journal_saved.parent.mkdir(parents=True)
        journal_bridge = journal_client / 'Interface/AddOns/DingTracker_LogData'
        journal_bridge.mkdir(parents=True)
        journal_target = journal_bridge / 'Journal.lua'
        journal_target.write_text('DingTrackerJournalPayload={schema=1,text=""}\n')
        (journal_bridge / 'Data.lua').write_text('combat payload unchanged\n')
        core = journal_client / 'Interface/AddOns/DingTracker'
        core.mkdir()
        (core / 'DingTracker.toc').write_text('## Version: synthetic\n')
        identity = dict(guid=guid, name='Synthetic Traveller', realm='Synthetic Realm', **{'class': 'HUNTER'})
        journal_raw = typed(dict(schema=1, version=version, exportedAt=1791384000,
            character=dict(identity=identity, currentLevel=19, levels={19: dict(level=19, counters=dict(mobs=25), coverage={})})))
        journal_encoded = base64.b64encode(b''.join(b'\x00' + journal_raw[index:index+8] for index in range(0, len(journal_raw), 8))).decode('ascii')
        journal_text = f'DINGTRACKER-DATA:2:{zlib.adler32(journal_raw):08X}:{len(journal_raw)}\n{journal_encoded}'
        journal_saved.write_text('DingTrackerDB=' + literal(dict(journalTransfer=dict(fileExport=dict(
            text=journal_text, identity=identity, mode='file', exportedAt=1791384000)), sentinel='unchanged')), encoding='utf-8')
        journal_before = {p.relative_to(journal_client).as_posix(): p.read_bytes() for p in journal_client.rglob('*') if p.is_file()}
        ui_check = run('--journal-transfer', '--wow', journal_client, '--check-ui')
        assert 'four actions' in ui_check.stdout and 'Tcl/Tk resources verified' in ui_check.stdout
        assert journal_before == {p.relative_to(journal_client).as_posix(): p.read_bytes() for p in journal_client.rglob('*') if p.is_file()}
        export = root / 'Synthetic Transfer.dingjournal'
        run('--journal-transfer', '--wow', journal_client, '--export', export)
        assert export.read_text() == journal_text
        assert 'verified' in run('--journal-transfer', '--validate', export).stdout
        run('--journal-transfer', '--wow', journal_client, '--import', export)
        assert 'DingTrackerJournalPayload' in journal_target.read_text() and journal_encoded in journal_target.read_text()
        journal_after = {p.relative_to(journal_client).as_posix(): p.read_bytes() for p in journal_client.rglob('*') if p.is_file()}
        assert set(journal_before) == set(journal_after)
        assert [name for name in journal_before if journal_before[name] != journal_after[name]] == ['Interface/AddOns/DingTracker_LogData/Journal.lua']
        staged = journal_target.read_bytes()
        corrupt = root / 'Corrupt Transfer.dingjournal'
        corrupt.write_text(journal_text[:-4])
        run('--journal-transfer', '--wow', journal_client, '--import', corrupt, code=1)
        assert journal_target.read_bytes() == staged

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
            result, _ = json.JSONDecoder().raw_decode(run('--wow', case, '--once').stdout)
            assert result['characters'][guid]['confirmedCloseCalls'] == expected, (name, result)
            first_payload = output.read_bytes()
            repeated, _ = json.JSONDecoder().raw_decode(run('--wow', case, '--once').stdout)
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
        # Exercise an explicit foreground launch without showing a console during CI.
        foreground_output = root / 'foreground-output.txt'
        with foreground_output.open('w', encoding='utf-8') as output:
            foreground = subprocess.Popen([str(exe), '--wow', str(client), '--foreground'],
                env=environment, cwd=root, stdin=subprocess.DEVNULL, stdout=output,
                stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                for _ in range(100):
                    if 'Watching ' in foreground_output.read_text(encoding='utf-8'): break
                    assert foreground.poll() is None, 'Foreground helper exited unexpectedly'
                    time.sleep(0.1)
                assert 'Checks every 10 seconds' in foreground_output.read_text(encoding='utf-8')
                assert not windows_tray.find_worker(client, ready=False), 'Foreground mode created a tray worker'
            finally:
                # PyInstaller's one-file bootloader has a child process. Stop the
                # complete tree belonging to this temporary foreground fixture.
                stopped = subprocess.run([str(system / 'System32/taskkill.exe'), '/PID', str(foreground.pid), '/T', '/F'],
                    capture_output=True, text=True, timeout=10)
                assert stopped.returncode == 0, (stopped.stdout, stopped.stderr)
                foreground.wait(timeout=10)
        try:
            assert 'not running' in run('--wow', client, '--status').stdout
            # Passing only --wow uses the same 10-second tray defaults as setup.
            assert 'Running in the Windows tray' in run('--wow', client).stdout
            assert 'every 10 seconds' in run('--wow', client, '--status').stdout
            assert 'already running' in run('--wow', client).stdout
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
            assert user.PostMessageW(window, 0x111, 204, 0)
            for _ in range(50):
                if windows_tray.worker_interval(client) == 10: break
                time.sleep(0.1)
            assert windows_tray.worker_interval(client) == 10
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
        print('PASS: Windows x64 bundle, SHA-256, CLI defaults/one-shot validation, dry run, 51-damage import, six close-call scenarios, repeat imports, SavedVariables preservation, default 10-second tray startup, duplicate prevention, live intervals, Scan now and Exit; no Python on executable PATH.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    check(parser.parse_args().archive.resolve(strict=True))

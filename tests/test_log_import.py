import copy
import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

test_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(test_root / 'companion' if (test_root / 'companion').is_dir() else test_root))
import log_import as logs

GUID = 'Player-1-ABC'
MOB = 'Creature-0-1-2-3-123-000001'
BASE = logs.timestamp('10/7/2026 10:01:00.000-5')


def marker(second, combat=False, **changes):
    result = dict(at=int(BASE + second), session='one', level=9, combat=combat, alive=True, duel=False, closed=False, proof=True, enabled={k: True for k in logs.METRICS})
    result.update(changes)
    return result


def context(markers=None):
    return {'schema': 1, 'characters': {GUID: {'enabled': True, 'exportedAt': int(BASE + 100), 'markers': markers or [], 'records': {9: {'startedAt': int(BASE - 100), 'endedAt': int(BASE + 100), 'coverage': {k: [{'startedAt': int(BASE - 100), 'endedAt': int(BASE + 100), 'enabled': True}] for k in logs.METRICS}}}}}}


def health(second, hp, entity=GUID, maximum=234):
    return (BASE + second, 'health', entity, {'health': hp, 'maxHealth': maximum, 'name': 'Test'})


class LiteralTests(unittest.TestCase):
    def test_roundtrip_and_skip_history(self):
        data = context([marker(0), marker(1, True)])
        source = 'DingTrackerDB={ ["history"]={ "ignored", true, 17 } }\nDingTrackerLogContext=' + logs.lua_literal(data)
        parsed = logs.LiteralReader(source).context()
        character = parsed['characters'][GUID]
        self.assertEqual(logs.array(character['markers']), data['characters'][GUID]['markers'])
        self.assertEqual(logs.array(character['records'][9]['coverage']['abilities']), data['characters'][GUID]['records'][9]['coverage']['abilities'])

    def test_escaped_strings(self):
        value = 'quote"\\\n\t\x00 é'
        source = 'DingTrackerLogContext=' + logs.lua_literal(value)
        self.assertEqual(logs.LiteralReader(source).context(), value)

    def test_code_and_malformed_input_rejected(self):
        for value in ('os.execute("bad")', '{}; os.execute("bad")', 'function() end', '{[{}]=1}', '{["x"]=1,["x"]=2}', '{"unfinished', '{1 2}', '1/0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                logs.LiteralReader('DingTrackerLogContext=' + value).context()

    def test_schema_limits(self):
        data = context([marker(0)])
        data['characters'][GUID]['markers'][0]['proof'] = 'yes'
        with self.assertRaises(ValueError): logs.build_payload(data, [])

    def test_timezone_not_machine_timezone(self):
        self.assertEqual(logs.timestamp('10/7/2026 10:01:00.000-5'), logs.timestamp('10/7/2026 15:01:00.000+0'))
        self.assertEqual(logs.timestamp('10/7/2026 10:01:00.000-05:30'), logs.timestamp('10/7/2026 15:31:00.000+0'))
        with self.assertRaises(ValueError): logs.timestamp('10/7 10:01:00.000')


class RecordTests(unittest.TestCase):
    def build(self, points, markers=None, data=None):
        payload, report = logs.build_payload(data or context(markers), points, int(BASE + 101))
        return payload['characters'][GUID]['levels'], report[GUID]

    def test_highest_individual_hit_is_not_sum(self):
        levels, _ = self.build([(BASE + 2, 'hit', GUID, {'id': 8044, 'amount': 51, 'name': 'Shock', 'critical': True}), (BASE + 3, 'hit', GUID, {'id': 6603, 'amount': 20, 'name': 'Attack', 'critical': False})])
        self.assertEqual(levels[9]['hits'][0]['amount'], 51)
        self.assertTrue(levels[9]['hits'][0]['critical'])

    def test_wrong_character_and_tracking_off(self):
        data = context()
        data['characters'][GUID]['records'][9]['coverage']['abilities'][0]['enabled'] = False
        levels, _ = self.build([(BASE + 2, 'hit', GUID, {'id': 8044, 'amount': 51}), (BASE + 2, 'hit', 'Player-2-other', {'id': 8044, 'amount': 999})], data=data)
        self.assertEqual(levels, {})

    def test_boundary_second_is_discarded(self):
        data = context()
        data['characters'][GUID]['records'][9]['endedAt'] = int(BASE + 10)
        data['characters'][GUID]['records'][10] = copy.deepcopy(data['characters'][GUID]['records'][9])
        data['characters'][GUID]['records'][10].update(startedAt=int(BASE + 10), endedAt=int(BASE + 100))
        self.assertIsNone(logs.record_at(data['characters'][GUID], BASE + 10.999, 'abilities'))
        self.assertEqual(logs.record_at(data['characters'][GUID], BASE + 11.1, 'abilities'), 10)

    def test_health_requires_exact_credited_kill(self):
        points = [health(2, 100, MOB, 239), (BASE + 3, 'kill', GUID, {'guid': MOB, 'name': 'Wolf'})]
        levels, _ = self.build(points)
        self.assertEqual(levels[9]['health'][0]['maxHealth'], 239)
        self.assertEqual(levels[9]['health'][0]['id'], 123)
        self.assertEqual(self.build([points[0]])[0], {})
        points[1][3]['guid'] = MOB + 'x'
        self.assertEqual(self.build(points)[0], {})

    def test_stale_mob_hp_not_attached(self):
        levels, _ = self.build([health(-150, 100, MOB, 239), (BASE + 3, 'kill', GUID, {'guid': MOB, 'name': 'Wolf'})])
        self.assertEqual(levels, {})

    def test_close_call_requires_end_of_combat_and_recovery(self):
        markers = [marker(0), marker(2, True), marker(18)]
        levels, report = self.build([health(15.714, 15), health(20.112, 99)], markers)
        close = levels[9]['closeCalls'][0]
        self.assertEqual(close['health'], 15)
        self.assertAlmostEqual(close['percent'], 100 * 15 / 234)
        self.assertEqual(close['combatEndedAt'], BASE + 18)
        self.assertTrue(close['duelExcluded'])
        self.assertEqual(report['confirmedCloseCalls'], 1)
        self.assertEqual(self.build([health(15.714, 15), health(20.112, 99)], markers[:2])[1]['confirmedCloseCalls'], 0)

    def test_recovery_during_combat_waits_for_combat_end(self):
        points = [health(5.5, 15), health(8.5, 99)]
        self.assertEqual(self.build(points, [marker(0), marker(2, True), marker(10)])[1]['confirmedCloseCalls'], 1)

    def test_death_duel_tracking_session_and_level_cancel(self):
        for event in (marker(17, True, alive=False), marker(17, True, duel=True), marker(17, True, session='two'), marker(17, True, level=10), marker(17, True, closed=True), marker(17, True, enabled={k: False for k in logs.METRICS})):
            with self.subTest(event=event):
                markers = [marker(0), marker(2, True), event, marker(18)]
                self.assertEqual(self.build([health(15.7, 15), health(20.1, 99)], markers)[1]['confirmedCloseCalls'], 0)
        points = [health(15.7, 15), (BASE + 16, 'death', GUID, {}), health(20.1, 99)]
        self.assertEqual(self.build(points, [marker(0), marker(2, True), marker(18)])[1]['confirmedCloseCalls'], 0)

    def test_unproven_old_logs_only_report_candidates(self):
        levels, report = self.build([health(15.714, 15), health(20.112, 99)])
        self.assertEqual(levels, {})
        self.assertEqual(report['lowHealthSamples'], 1)
        self.assertEqual(report['confirmedCloseCalls'], 0)

    def test_ambiguous_transition_second_and_missing_duel_hooks(self):
        for markers in ([marker(0), marker(15, True), marker(18)], [marker(0), marker(2, True, proof=False), marker(18, proof=False)]):
            self.assertEqual(self.build([health(15.7, 15), health(20.1, 99)], markers)[1]['confirmedCloseCalls'], 0)

    def test_episode_ids_are_stable(self):
        points = [health(15.7, 15), health(20.1, 99)]
        markers = [marker(0), marker(2, True), marker(18)]
        self.assertEqual(self.build(points, markers)[0], self.build(points, markers)[0])

    def test_death_in_combat_end_second_cannot_confirm_survival(self):
        points = [health(5.5, 15), health(8.5, 99), (BASE + 10.5, 'death', GUID, {})]
        self.assertEqual(self.build(points, [marker(0), marker(2, True), marker(10)])[1]['confirmedCloseCalls'], 0)

    def test_deeper_drop_requires_a_later_recovery(self):
        points = [health(5.5, 15), health(8.5, 99), health(9.5, 7)]
        self.assertEqual(self.build(points, [marker(0), marker(2, True), marker(10)])[1]['confirmedCloseCalls'], 0)

    def test_brief_duel_within_one_second_cancels_episode(self):
        markers = [marker(0), marker(2, True), marker(7, True, duel=True), marker(7, True), marker(10)]
        self.assertEqual(self.build([health(5.5, 15), health(8.5, 99)], markers)[1]['confirmedCloseCalls'], 0)


class DiskTests(unittest.TestCase):
    def line(self, event='SPELL_DAMAGE', guid=GUID, hp=15, maximum=234):
        fields = [event, guid, 'Player, Quoted', '0x511', '0x0', MOB, 'Wolf', '0xa48', '0x0', '8044', 'Earth Shock', '0x8', guid, '0000', str(hp), str(maximum)] + ['0'] * 13 + ['51', '51', '-1', '8', '0', '0', '7', 'true', 'false', 'false', 'ST']
        stream = io.StringIO(); csv.writer(stream, lineterminator='\n').writerow(fields)
        return '10/7/2026 10:01:15.714-5  ' + stream.getvalue()

    def test_schema_duplicate_quotes_partial_line_and_secret_health(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'log.txt'
            header = '10/7/2026 10:01:00.000-5  COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,0,BUILD_VERSION,1.60.1,PROJECT_ID,18\n'
            path.write_text(header + self.line() * 2 + self.line(hp=-1) + self.line().rstrip('\n'))
            points, _ = logs.read_logs([path], {GUID: {}})
            self.assertEqual(len([p for p in points if p[1] == 'hit']), 1)
            self.assertEqual(len([p for p in points if p[1] == 'health']), 1)
            self.assertEqual(points[0][3]['name'], 'Player, Quoted')
            path.write_text(header.replace('VERSION,22', 'VERSION,23') + self.line())
            self.assertEqual(logs.read_logs([path], {GUID: {}})[0], [])

    def test_landed_damage_not_double_counted_and_other_player_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'log.txt'
            header = '10/7/2026 10:01:00.000-5  COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,0,BUILD_VERSION,1.60.1,PROJECT_ID,18\n'
            path.write_text(header + self.line(guid='Player-2-DEF') + self.line(event='SWING_DAMAGE_LANDED'))
            self.assertFalse(any(p[1] == 'hit' for p in logs.read_logs([path], {GUID: {}})[0]))

    def test_only_companion_written_and_dry_run_preserves_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'Interface/AddOns/DingTracker_LogData/Data.lua'
            target.parent.mkdir(parents=True); target.write_text('blank')
            saved = root / 'WTF/Account/TEST/SavedVariables/DingTracker.lua'
            saved.parent.mkdir(parents=True)
            content = 'DingTrackerLogContext=' + logs.lua_literal(context())
            saved.write_text(content)
            logs.run_once(root, True)
            self.assertEqual(target.read_text(), 'blank')
            logs.run_once(root)
            self.assertIn('DingTrackerLogPayload', target.read_text())
            self.assertEqual(saved.read_text(), content)
            self.assertEqual(len(list(target.parent.iterdir())), 1)


class LaunchTests(unittest.TestCase):
    def test_discovery_requires_installed_companion_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            client = base / 'World of Warcraft/_classic_beta_'
            target = client / 'Interface/AddOns/DingTracker_LogData/Data.lua'
            target.parent.mkdir(parents=True); target.write_text('blank')
            other = base / 'World of Warcraft/_retail_'
            other.mkdir()
            self.assertEqual(logs.discover_clients({'ProgramFiles': folder, 'ProgramW6432': folder}, [client]), [client.resolve()])

    def test_double_click_choice_validates_selection_and_path(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Path(folder)
            target = client / 'Interface/AddOns/DingTracker_LogData/Data.lua'
            target.parent.mkdir(parents=True); target.write_text('blank')
            with patch.object(logs, 'discover_clients', return_value=[client]), patch('builtins.input', side_effect=['9', '/missing', '1']), patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(logs.choose_client(), client.resolve())
            with patch.object(logs, 'discover_clients', return_value=[]), patch('builtins.input', return_value=''), patch('sys.stdout', new=io.StringIO()):
                self.assertIsNone(logs.choose_client())
            with patch.object(logs, 'discover_clients', return_value=[]), patch('builtins.input', side_effect=EOFError), patch('sys.stdout', new=io.StringIO()):
                self.assertIsNone(logs.choose_client())

    def test_noninteractive_without_path_fails_without_hanging(self):
        with patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as result:
            logs.main([])
        self.assertEqual(result.exception.code, 2)

    def test_double_click_watches_and_cancel_exits_without_writes(self):
        with patch('sys.stdin.isatty', return_value=True), patch.object(logs, 'choose_client', return_value=None), patch.object(logs, 'run_once') as run:
            self.assertEqual(logs.main([]), 0)
            run.assert_not_called()
        report = {'dryRun': False, 'characters': {}, 'warnings': []}
        with patch('sys.stdin.isatty', return_value=True), patch.object(logs, 'choose_client', return_value=Path('/missing')), patch.object(logs, 'choose_watch_options', return_value=(120, 'console')), patch.object(logs, 'run_once', return_value=report) as run, patch('sys.stdout', new=io.StringIO()), patch('sys.stderr', new=io.StringIO()), patch.object(logs.WatchControl, 'wait', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            logs.main([])
        run.assert_called_once_with(Path('/missing'), False)

    def test_setup_and_interval_choices(self):
        self.assertIn("press Enter to open chat", logs.GAME_SETUP)
        self.assertIn("'/ding logs on'", logs.GAME_SETUP)
        self.assertIn("'/reload'", logs.GAME_SETUP)
        with patch('builtins.input', side_effect=['4', '300', 'no', 'b']), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(logs.choose_watch_options(), (300, 'background'))
        with patch('builtins.input', side_effect=['', 'o']), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(logs.choose_watch_options(), (60, 'once'))
        for value in ('0', '4', '3601', '1.5', 'abc'):
            with self.subTest(value=value), self.assertRaises(logs.argparse.ArgumentTypeError):
                logs.interval_seconds(value)

    def test_cli_uses_selected_interval_and_rejects_invalid_value(self):
        for value in (60, 300):
            args = ['--wow', '/missing', '--watch'] + (['--interval', str(value)] if value != 60 else [])
            with patch.object(logs, 'watch_files', return_value=0) as watch, patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(logs.main(args), 0)
            self.assertEqual(watch.call_args.args[4].interval, value)
        with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as result:
            logs.main(['--wow', '/missing', '--watch', '--interval', '0'])
        self.assertEqual(result.exception.code, 2)

    def test_control_wakes_for_manual_scan_interval_change_and_exit(self):
        control = logs.WatchControl(60)
        control.scan_now()
        self.assertTrue(control.wake.is_set())
        self.assertTrue(control.consume_scan())
        self.assertFalse(control.consume_scan())
        control.wait()
        self.assertFalse(control.wake.is_set())
        control.set_interval(300)
        self.assertEqual(control.interval, 300)
        self.assertTrue(control.wake.is_set())
        control.stop()
        self.assertTrue(control.stopped.is_set())

    def test_watch_does_not_repeat_failed_scans_of_unchanged_files(self):
        with tempfile.TemporaryDirectory() as folder:
            control = logs.WatchControl(60)
            waits = []
            def wait():
                waits.append(True)
                if len(waits) == 3: control.stop()
            with patch.object(control, 'wait', side_effect=wait), patch.object(logs, 'run_once', side_effect=ValueError('not enabled')) as run, patch('sys.stderr', new=io.StringIO()) as stderr:
                logs.watch_files(Path(folder), watch=True, control=control)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(stderr.getvalue().count('not enabled'), 1)

    def test_watch_detects_changes_during_scan_without_rescanning_own_output(self):
        for source_changes, force in ((False, False), (True, False), (False, True)):
            with self.subTest(source_changes=source_changes, force=force), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                saved = root / 'WTF/Account/TEST/SavedVariables/DingTracker.lua'
                saved.parent.mkdir(parents=True); saved.write_text('a')
                target = root / 'Interface/AddOns/DingTracker_LogData/Data.lua'
                target.parent.mkdir(parents=True); target.write_text('blank')
                control = logs.WatchControl(60)
                waits, scans = [], []
                def scan(*args):
                    scans.append(True); target.write_text('output' + str(len(scans)))
                    if source_changes and len(scans) == 1: saved.write_text('longer changed context')
                    return {'dryRun': False, 'characters': {}, 'warnings': []}
                def wait():
                    waits.append(True)
                    if force and len(waits) == 1: control.scan_now()
                    if len(waits) == 3: control.stop()
                with patch.object(control, 'wait', side_effect=wait), patch.object(logs, 'run_once', side_effect=scan), patch('sys.stdout', new=io.StringIO()):
                    logs.watch_files(root, watch=True, control=control)
                self.assertEqual(len(scans), 2 if source_changes or force else 1)


if __name__ == '__main__': unittest.main()

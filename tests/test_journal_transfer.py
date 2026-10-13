from pathlib import Path
import base64
import hashlib
import errno
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

test_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(test_root / 'companion' if (test_root / 'companion').is_dir() else test_root))
import journal_transfer as transfer
from log_import import lua_literal


def typed(value):
    if isinstance(value, bool):
        return b'b1' if value else b'b0'
    if isinstance(value, (int, float)):
        literal = str(value).encode('ascii')
        return b'n' + str(len(literal)).encode() + b':' + literal
    if isinstance(value, str):
        literal = value.encode('utf-8').hex().upper().encode('ascii')
        return b's' + str(len(literal)).encode() + b':' + literal
    if isinstance(value, dict):
        return b't' + str(len(value)).encode() + b':' + b''.join(typed(k) + typed(v) for k, v in value.items())
    raise TypeError(value)


def fixture(legacy=False):
    character = {'identity': {'guid': 'Player-1-ABC', 'name': 'Traveller', 'realm': 'Forever Test', 'class': 'HUNTER'},
                 'currentLevel': 19, 'levels': {19: {'level': 19, 'counters': {'mobs': 25}, 'coverage': {},
                                                   'quests': {index: {'id': index, 'name': 'A repeated long journal quest name'} for index in range(1, 401)}}}}
    raw = typed({'schema': 1, 'version': '0.15.26', 'exportedAt': 1791900000, 'character': character})
    body = raw.decode('ascii') if legacy else transfer.compress(raw)
    header = f'DINGTRACKER-DATA:{1 if legacy else 2}:{zlib.adler32(raw):08X}' + ('' if legacy else f':{len(raw)}')
    return header + '\n' + '\n'.join(body[index:index + 96] for index in range(0, len(body), 96)), raw


def client(root):
    addon = root / 'Interface/AddOns/DingTracker'
    addon.mkdir(parents=True)
    (addon / 'DingTracker.toc').write_text('## Version: test\n')
    bridge = root / 'Interface/AddOns/DingTracker_LogData'
    bridge.mkdir()
    (bridge / 'Journal.lua').write_text('DingTrackerJournalPayload = {schema=1,text=""}\n')
    (bridge / 'Data.lua').write_text('combat log payload untouched')
    saved = root / 'WTF/Account/TEST/SavedVariables'
    saved.mkdir(parents=True)
    text, _ = fixture()
    request = {'text': text, 'identity': {'guid': 'Player-1-ABC'}, 'mode': 'file', 'exportedAt': 1791900000}
    (saved / 'DingTracker.lua').write_text('DingTrackerDB = ' + lua_literal({'journalTransfer': {'fileExport': request}, 'settings': {'enabled': True}}))
    (saved / 'DingTracker.lua.bak').write_text('backup untouched')
    return saved / 'DingTracker.lua', bridge


class JournalTransferTests(unittest.TestCase):
    def test_compact_and_legacy_preserve_same_data_with_major_size_reduction(self):
        compact, raw = fixture()
        legacy, _ = fixture(True)
        self.assertLess(len(compact), len(legacy) * .4)
        self.assertEqual(transfer.validate(compact)['identity'], transfer.validate(legacy)['identity'])
        self.assertEqual(transfer.decompress(transfer.compress(raw), len(raw)), raw)

    def test_all_bytes_window_edges_and_overlapping_matches_roundtrip(self):
        for raw in (b'x', b'xx', b'xxx', bytes(range(256)) * 40, b'a' * 100000,
                    bytes(range(251)) * 19 + b'unique' + bytes(range(251)) * 3):
            self.assertEqual(transfer.decompress(transfer.compress(raw), len(raw)), raw)

    def test_corruption_truncation_and_decompression_bombs_fail(self):
        compact, _ = fixture()
        for malformed in (compact[:-3], compact + 'x', compact.replace(':2:', ':9:', 1),
                          'DINGTRACKER-DATA:2:00000000:4194305\nAA==', 'return {character={}}'):
            with self.assertRaises(ValueError):
                transfer.validate(malformed)
        for encoded, size in ((base64.b64encode(b'\x01\x00\x00').decode(), 3), ('AB==', 1),
                              (base64.b64encode(b'\x00a\x00').decode(), 1), ('AA==', 1),
                              (base64.b64encode(b'\x00abcdefgh\x00').decode(), 8)):
            with self.assertRaises(ValueError):
                transfer.decompress(encoded, size)

    def test_nonexecutable_parser_rejects_duplicates_depth_and_bad_strings(self):
        for raw in (b't2:s2:61b1s2:61b0', b's2:00', b'n3:nan', b'function()end', b't1:n1:1' * 30 + b'b0'):
            reader = transfer.JournalReader(raw)
            with self.assertRaises((ValueError, UnicodeError)):
                reader.value()

    def test_file_roundtrip_stages_only_separate_payload_without_touching_savedvariables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved, bridge = client(root)
            protected = {path: path.read_bytes() for path in (saved, saved.with_suffix('.lua.bak'), bridge / 'Data.lua')}
            request = transfer.latest_request(root)
            output = transfer.export_file(request, root / 'export.dingjournal')
            summary = transfer.import_file(root, output)
            self.assertEqual(summary['identity']['guid'], 'Player-1-ABC')
            staged = (bridge / 'Journal.lua').read_text()
            self.assertIn(hashlib.sha256(request['text'].encode()).hexdigest(), staged)
            self.assertIn('DingTrackerJournalPayload = ', staged)
            for path, original in protected.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(bridge.glob('*.tmp')))

    def test_invalid_import_preserves_existing_payload_and_export_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bridge = client(root)
            before = (bridge / 'Journal.lua').read_bytes()
            with self.assertRaises(ValueError):
                transfer.stage_text(root, 'not a journal')
            self.assertEqual((bridge / 'Journal.lua').read_bytes(), before)
            request = transfer.latest_request(root)
            output = transfer.export_file(request, root / 'export.dingjournal')
            with self.assertRaises(ValueError):
                transfer.export_file(request, output)
            self.assertEqual(output.read_text(), request['text'])

    def test_export_to_fat_filesystem_fallback_still_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client(root)
            request = transfer.latest_request(root)
            with patch.object(transfer.os, 'link', side_effect=OSError(errno.EOPNOTSUPP, 'no hard links')):
                output = transfer.export_file(request, root / 'usb.dingjournal')
            self.assertEqual(output.read_text(), request['text'])
            with self.assertRaises(ValueError):
                transfer.export_file(request, output)

    def test_savedvariables_executable_expression_is_never_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'DingTracker.lua'
            path.write_text('DingTrackerDB = os.execute("touch dangerous")')
            with self.assertRaises(ValueError):
                transfer.saved_request(path)
            self.assertFalse((path.parent / 'dangerous').exists())

    def test_clipboard_actions_use_actual_os_bridge(self):
        text, _ = fixture()
        with patch.object(transfer.os, 'name', 'posix'), patch.object(transfer.shutil, 'which', return_value='/usr/bin/wl-copy'), patch.object(transfer.subprocess, 'run') as process:
            transfer.clipboard_write(text)
            self.assertEqual(process.call_args.args[0], ['wl-copy'])
            self.assertEqual(process.call_args.kwargs['input'], text.encode('ascii'))

    def test_clipboard_reader_times_out_unresponsive_process_and_bounds_large_output(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'timed out'):
            transfer._clipboard_process([sys.executable, '-c', 'import time;time.sleep(60)'], timeout=.1)
        self.assertLess(time.monotonic() - started, 3)
        with patch.object(transfer, 'MAX_BYTES', 128), self.assertRaisesRegex(ValueError, 'exceeds'):
            transfer._clipboard_process([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(b"x"*10000);sys.stdout.flush()'], timeout=3)

    def test_cli_validation_is_read_only_and_helper_dispatch_supports_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            text, _ = fixture()
            path = Path(temporary) / 'journal.dingjournal'
            path.write_text(text)
            before = path.read_bytes()
            self.assertEqual(transfer.main(['--validate', str(path)]), 0)
            self.assertEqual(path.read_bytes(), before)
        import log_import
        with patch.object(transfer, 'main', return_value=0) as main:
            self.assertEqual(log_import.main(['--journal-transfer', '--wow', 'client']), 0)
            main.assert_called_once_with(['--wow', 'client'])

    def test_hidden_ui_check_routes_to_withdrawn_gui_without_transfer_actions(self):
        with patch.object(transfer, 'gui', return_value=0) as gui, patch.object(transfer, 'clipboard_read') as read, patch.object(transfer, 'clipboard_write') as write, patch.object(transfer, 'export_file') as export, patch.object(transfer, 'import_file') as import_file:
            self.assertEqual(transfer.main(['--wow', 'synthetic-client', '--check-ui']), 0)
            gui.assert_called_once_with(Path('synthetic-client'), check_ui=True)
            read.assert_not_called()
            write.assert_not_called()
            export.assert_not_called()
            import_file.assert_not_called()

    def test_open_window_observes_only_new_explicit_clipboard_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved, _ = client(root)
            original = saved.read_bytes()
            text, _ = fixture()
            def save(mode, token):
                saved.write_text('DingTrackerDB = ' + lua_literal({'journalTransfer': {'fileExport': {
                    'text': text, 'identity': {'guid': 'Player-1-ABC'}, 'mode': mode,
                    'exportedAt': 1791900000, 'requestID': token}}}))
            save('clipboard', 'old-request')
            watcher = transfer.ClipboardRequestWatcher()
            entries, pending = watcher.poll(root)
            self.assertEqual(pending, [], 'Opening a window must not replace the clipboard with an old request')
            with patch.object(transfer, 'requests', side_effect=AssertionError('Unchanged files must not be parsed')):
                self.assertEqual(watcher.poll(root), (None, []))
            save('file', 'file-request')
            self.assertEqual(watcher.poll(root)[1], [])
            save('clipboard', 'new-request')
            pending = watcher.poll(root)[1]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]['text'], text)
            self.assertEqual(len(pending[0]['text']), pending[0]['summary']['encodedBytes'])
            self.assertEqual(watcher.poll(root, force=True)[1], [], 'Refresh never copies the same request twice')
            save('clipboard', 'another-request')
            self.assertEqual(len(watcher.poll(root)[1]), 1, 'Repeated explicit copies of one snapshot still work')
            other = root / 'Other client'
            client(other)
            self.assertEqual(watcher.poll(other)[1], [])

    def test_repeated_file_exports_preserve_exact_saved_text_and_do_not_grow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved, bridge = client(root)
            before = saved.read_bytes()
            request = transfer.latest_request(root)
            for index in range(4):
                current = transfer.latest_request(root)
                destination = transfer.export_file(current, root / f'export-{index}.dingjournal')
                self.assertEqual(destination.read_bytes(), request['text'].encode('ascii'))
                self.assertEqual(destination.stat().st_size, current['summary']['encodedBytes'])
            self.assertEqual(saved.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

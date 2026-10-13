"""Portable journal file/clipboard bridge. Never executes Lua or edits SavedVariables."""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib

try:
    from .log_import import LiteralReader, NAMED, lua_literal, discover_clients
except ImportError:
    from log_import import LiteralReader, NAMED, lua_literal, discover_clients

MAX_BYTES = 4 * 1024 * 1024
MAX_SAVED_BYTES = 64 * 1024 * 1024
MAX_NODES, MAX_DEPTH, MAX_STRING = 150000, 24, 8192


def decompress(encoded: str, expected: int) -> bytes:
    if not 1 <= expected <= MAX_BYTES or len(encoded) > MAX_BYTES:
        raise ValueError("Journal exceeds the 4 MB decoded limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid compressed journal") from error
    if base64.b64encode(data).decode("ascii") != encoded:
        raise ValueError("Noncanonical journal encoding")
    out, at = bytearray(), 0
    while at < len(data):
        flags = data[at]
        at += 1
        if at == len(data):
            raise ValueError("Empty journal token group")
        for bit in range(8):
            if at == len(data):
                if flags >> bit:
                    raise ValueError("Truncated journal match")
                break
            if not flags & (1 << bit):
                if len(out) >= expected:
                    raise ValueError("Journal expands past its declared size")
                out.append(data[at])
                at += 1
            else:
                if at + 1 >= len(data):
                    raise ValueError("Truncated journal match")
                a, b = data[at:at + 2]
                at += 2
                distance, size = a * 16 + b // 16 + 1, b % 16 + 3
                if distance > len(out) or len(out) + size > expected:
                    raise ValueError("Invalid journal match")
                for _ in range(size):
                    out.append(out[-distance])
    if len(out) != expected:
        raise ValueError("Incomplete compressed journal")
    return bytes(out)


def compress(data: bytes) -> str:
    """Matches JournalCodec.lua, including its 4096-byte bounded search chain."""
    if len(data) > MAX_BYTES:
        raise ValueError("Journal exceeds the 4 MB decoded limit")
    heads, previous, keys, positions = {}, {}, {}, {}
    at, out = 0, bytearray()

    def remember(index):
        if index + 2 >= len(data):
            return
        slot = index % 4096
        old_key, old_position = keys.get(slot), positions.get(slot)
        if old_key is not None and heads.get(old_key) == old_position:
            del heads[old_key]
        key = data[index:index + 3]
        previous[slot] = heads.get(key)
        keys[slot], positions[slot], heads[key] = key, index, index

    while at < len(data):
        flags, tokens = 0, bytearray()
        for bit in range(8):
            if at >= len(data):
                break
            best, length = 0, 0
            if at + 2 < len(data):
                candidate, attempts = heads.get(data[at:at + 3]), 0
                while candidate is not None and candidate >= at - 4096 and candidate < at and attempts < 16:
                    size = 3
                    while size < 18 and at + size < len(data) and data[candidate + size] == data[at + size]:
                        size += 1
                    if size > length:
                        best, length = candidate, size
                    if length == 18:
                        break
                    attempts += 1
                    slot = candidate % 4096
                    if positions.get(slot) != candidate:
                        break
                    candidate = previous.get(slot)
            if length >= 3:
                offset = at - best - 1
                tokens.extend((offset // 16, (offset % 16) * 16 + length - 3))
                flags |= 1 << bit
                for index in range(at, at + length):
                    remember(index)
                at += length
            else:
                tokens.append(data[at])
                remember(at)
                at += 1
        out.append(flags)
        out.extend(tokens)
    return base64.b64encode(out).decode("ascii")


class JournalReader:
    """Bounded data-only decoder for the addon's typed journal format."""

    def __init__(self, source):
        self.source, self.at, self.nodes = source, 0, 0

    def length(self):
        end = self.source.find(b":", self.at, self.at + 10)
        literal = self.source[self.at:end] if end >= 0 else b""
        if not literal or not literal.isdigit():
            raise ValueError("Invalid journal length")
        self.at = end + 1
        result = int(literal)
        if result > MAX_BYTES:
            raise ValueError("Journal length exceeds limits")
        return result

    def value(self, depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH or self.at >= len(self.source):
            raise ValueError("Journal exceeds its depth/node limits or is truncated")
        kind = self.source[self.at:self.at + 1]
        self.at += 1
        if kind == b"b":
            value = self.source[self.at:self.at + 1]
            self.at += 1
            if value not in (b"0", b"1"):
                raise ValueError("Invalid journal boolean")
            return value == b"1"
        if kind in (b"s", b"n"):
            size = self.length()
            if self.at + size > len(self.source):
                raise ValueError("Truncated journal scalar")
            literal = self.source[self.at:self.at + size]
            self.at += size
            if kind == b"s":
                if size % 2 or size > MAX_STRING * 2 or re.search(rb"[^0-9A-Fa-f]", literal):
                    raise ValueError("Invalid journal string")
                raw = bytes.fromhex(literal.decode("ascii"))
                if re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", raw):
                    raise ValueError("Unsupported journal string character")
                return raw.decode("utf-8")
            if size > 32 or not re.fullmatch(rb"[0-9.eE+\-]+", literal):
                raise ValueError("Invalid journal number")
            value = float(literal)
            if not math.isfinite(value) or abs(value) > 1e15:
                raise ValueError("Invalid journal number")
            return int(value) if value.is_integer() else value
        if kind == b"t":
            count, result = self.length(), {}
            if count > MAX_NODES:
                raise ValueError("Journal table exceeds limits")
            for _ in range(count):
                key = self.value(depth + 1)
                if isinstance(key, bool) or not isinstance(key, (str, int, float)) or key in result:
                    raise ValueError("Duplicate or invalid journal key")
                result[key] = self.value(depth + 1)
            return result
        raise ValueError("Unsupported journal type")


def validate(text: str) -> dict:
    if not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= MAX_BYTES:
        raise ValueError("Journal must contain at most 4 MB")
    match = re.fullmatch(r"\s*DINGTRACKER-DATA:([12]):([0-9A-Fa-f]{8})(?::([0-9]{1,9}))?[\r\n]+(.*)", text, re.S)
    if not match or (match[1] == "2") != (match[3] is not None):
        raise ValueError("Select a complete Ding Tracker journal export")
    body = re.sub(r"\s", "", match[4])
    try:
        payload = decompress(body, int(match[3])) if match[1] == "2" else body.encode("ascii")
    except UnicodeError as error:
        raise ValueError("Invalid journal encoding") from error
    if len(payload) > MAX_BYTES or f"{zlib.adler32(payload):08X}" != match[2].upper():
        raise ValueError("Journal checksum failed; the export is incomplete or changed")
    reader = JournalReader(payload)
    parsed = reader.value()
    if reader.at != len(payload) or not isinstance(parsed, dict) or parsed.get("schema") != 1:
        raise ValueError("Invalid journal data")
    character = parsed.get("character")
    if not isinstance(character, dict) or not isinstance(character.get("identity"), dict) or not isinstance(character.get("levels"), dict):
        raise ValueError("Invalid journal character")
    identity = character["identity"]
    if not re.fullmatch(r"Player-[0-9]+-[0-9A-Fa-f]+", str(identity.get("guid", ""))) or not all(
        isinstance(identity.get(k), str) and 0 < len(identity[k]) <= 128 for k in ("name", "realm", "class")
    ):
        raise ValueError("Invalid journal identity")
    return {"identity": identity, "level": character.get("currentLevel"), "exportedAt": parsed.get("exportedAt"),
            "encodedBytes": len(text), "decodedBytes": len(payload)}


def saved_request(path: Path) -> dict | None:
    if path.stat().st_size > MAX_SAVED_BYTES:
        raise ValueError("SavedVariables exceeds the 64 MB reader limit")
    reader = LiteralReader(path.read_text(encoding="utf-8-sig"))
    database = None
    while True:
        reader.space()
        if reader.pos == len(reader.text):
            break
        match = NAMED.match(reader.text, reader.pos)
        if not match:
            raise ValueError("Unsupported SavedVariables assignment")
        reader.pos = match.end()
        keep = match[1] == "DingTrackerDB"
        value = reader.value(keep)
        if keep:
            if database is not None:
                raise ValueError("Duplicate journal database")
            database = value
    store = database.get("journalTransfer") if isinstance(database, dict) else None
    request = store.get("fileExport") if isinstance(store, dict) else None
    if request is None:
        return None
    if not isinstance(request, dict) or request.get("mode") not in ("file", "clipboard"):
        raise ValueError("Invalid journal export request")
    if "requestID" in request and (not isinstance(request["requestID"], str) or not 0 < len(request["requestID"]) <= 128):
        raise ValueError("Invalid journal copy request identity")
    summary = validate(request.get("text"))
    identity = request.get("identity")
    if not isinstance(identity, dict) or identity.get("guid") != summary["identity"]["guid"]:
        raise ValueError("Journal request identity mismatch")
    return {**request, "summary": summary, "savedPath": path}


def client_path(folder: Path) -> Path:
    client = folder.expanduser().resolve(strict=True)
    if not all((client / part).is_dir() for part in ("Interface", "WTF")) or not (
        client / "Interface/AddOns/DingTracker/DingTracker.toc"
    ).is_file():
        raise ValueError("Choose the WoW client folder containing Interface and WTF, with Ding Tracker installed")
    return client


def requests(folder: Path) -> list[dict]:
    client = client_path(folder)
    result = []
    for path in sorted((client / "WTF/Account").glob("*/SavedVariables/DingTracker.lua")):
        request = saved_request(path)
        if request:
            result.append(request)
    return sorted(result, key=lambda row: row.get("exportedAt", 0), reverse=True)


def latest_request(folder: Path) -> dict:
    result = requests(folder)
    if not result:
        raise ValueError("In WoW, open Settings > Data > Export journal > Export to file. WoW saves and reloads; then refresh this helper.")
    if len(result) > 1:
        raise ValueError("Multiple accounts have saved exports. Select one in the journal transfer window.")
    return result[0]


class ClipboardRequestWatcher:
    """Observe new explicit copy requests while the transfer window is open."""
    def __init__(self):
        self.folder = None
        self.signature = None
        self.seen = set()

    def poll(self, folder: Path, force=False):
        client = client_path(folder)
        signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                          for path in sorted((client / "WTF/Account").glob("*/SavedVariables/DingTracker.lua")))
        first = client != self.folder
        if not force and not first and signature == self.signature:
            return None, []
        entries = requests(client)
        keys = {(str(entry["savedPath"]), entry.get("requestID")) for entry in entries
                if isinstance(entry.get("requestID"), str) and 0 < len(entry["requestID"]) <= 128}
        if first:
            self.seen = set(keys)  # Opening/changing folders never copies an old request.
        pending = [entry for entry in entries if entry.get("mode") == "clipboard"
                   and (str(entry["savedPath"]), entry.get("requestID")) in keys - self.seen]
        self.seen.update(keys)
        if len(self.seen) > 256:
            self.seen = set(keys)
        self.folder, self.signature = client, signature
        return entries, pending


def export_file(request: dict, destination: Path, replace=False) -> Path:
    validate(request.get("text"))
    destination = destination.expanduser().absolute()
    if destination.suffix.lower() != ".dingjournal":
        destination = destination.with_suffix(".dingjournal")
    if destination.is_symlink() or destination.exists() and not replace:
        raise ValueError("The export file already exists; choose a new name")
    if not destination.parent.is_dir():
        raise ValueError("Choose an existing destination folder")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="ascii", dir=destination.parent, delete=False, suffix=".tmp", newline="\n") as stream:
            temporary = Path(stream.name)
            stream.write(request["text"])
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            # A hard link fails if another process created the destination;
            # it publishes a fully written file without overwriting anything.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise ValueError("The export file already exists; choose a new name")
            except OSError as error:
                if error.errno not in (errno.EPERM, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL):
                    raise
                # FAT/exFAT removable drives do not support hard links. An
                # exclusive creation still prevents overwriting any file.
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as output, temporary.open("rb") as source:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                except Exception:
                    destination.unlink()
                    raise
            temporary.unlink()
        return destination
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def stage_text(folder: Path, text: str, stamp=None) -> dict:
    summary = validate(text)
    client = client_path(folder)
    target = client / "Interface/AddOns/DingTracker_LogData/Journal.lua"
    if not target.is_file() or target.is_symlink() or target.resolve().parent != target.parent:
        raise ValueError("Install the updated DingTracker_LogData component in this client first")
    token = hashlib.sha256(text.encode("ascii")).hexdigest()
    payload = {"schema": 1, "generatedAt": int(time.time() if stamp is None else stamp), "token": token, "text": text}
    output = "-- Staged locally for explicit journal preview. SavedVariables are never modified.\nDingTrackerJournalPayload = " + lua_literal(payload) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp", newline="\n") as stream:
            temporary = Path(stream.name)
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return summary


def import_file(folder: Path, source: Path) -> dict:
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Journal file exceeds the 4 MB transfer limit")
    return stage_text(folder, source.read_text(encoding="utf-8-sig"))


def _clipboard_process(command, timeout=10) -> bytes:
    """Bound the pipe read itself, not just the wait after it returns."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    result = {}

    def read():
        try:
            result["bytes"] = process.stdout.read(MAX_BYTES + 1)
        except OSError as error:
            result["error"] = error

    reader = threading.Thread(target=read, name="DingTrackerClipboard", daemon=True)
    started = time.monotonic()
    reader.start()
    try:
        reader.join(timeout)
        if reader.is_alive():
            raise ValueError("Clipboard read timed out")
        if "error" in result:
            raise result["error"]
        output = result.get("bytes", b"")
        if len(output) > MAX_BYTES:
            raise ValueError("Clipboard exceeds the 4 MB transfer limit")
        try:
            code = process.wait(timeout=max(.01, timeout - (time.monotonic() - started)))
        except subprocess.TimeoutExpired as error:
            raise ValueError("Clipboard read timed out") from error
        if code:
            raise ValueError("Clipboard is unavailable")
        return output
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        reader.join(2)
        process.stdout.close()


def clipboard_read(root=None) -> str:
    if root is not None:
        return root.clipboard_get()
    if os.name != "nt":
        command = ["wl-paste", "--no-newline"] if shutil.which("wl-paste") else ["xclip", "-selection", "clipboard", "-o"] if shutil.which("xclip") else None
        if command is None:
            raise ValueError("Use the journal transfer window or install wl-clipboard/xclip for command-line clipboard transfer")
        return _clipboard_process(command).decode("utf-8")
    import tkinter as tk
    window = tk.Tk()
    window.withdraw()
    try:
        return window.clipboard_get()
    finally:
        window.destroy()


def clipboard_write(text: str, root=None):
    validate(text)
    if root is not None:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        return
    if os.name != "nt":
        command = ["wl-copy"] if shutil.which("wl-copy") else ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None
        if command is None:
            raise ValueError("Use the journal transfer window or install wl-clipboard/xclip for command-line clipboard transfer")
        subprocess.run(command, input=text.encode("ascii"), check=True, timeout=10)
        return
    import tkinter as tk
    window = tk.Tk()
    window.withdraw()
    try:
        window.clipboard_clear()
        window.clipboard_append(text)
        window.update()
    finally:
        window.destroy()


def gui(initial=None, check_ui=False):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    if os.name == "nt" and not check_ui:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetConsoleWindow.restype = ctypes.c_void_p
        console = kernel.GetConsoleWindow()
        processes = (ctypes.c_ulong * 2)()
        if console and kernel.GetConsoleProcessList(processes, 2) == 1:
            show = ctypes.windll.user32.ShowWindow
            show.argtypes = [ctypes.c_void_p, ctypes.c_int]
            show(console, 0)
    root = tk.Tk()
    if check_ui:
        root.withdraw()
    root.title("Ding Tracker - Journal transfer")
    root.geometry("680x460")
    root.minsize(600, 420)
    panel = ttk.Frame(root, padding=18)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="Move your journal between computers", font=("Segoe UI", 16)).pack(anchor="w")
    ttk.Label(panel, text="Export: In WoW, Export to file saves the displayed snapshot and reloads. This window automatically copies new Copy via helper requests while open.\nImport: Choose a file or clipboard here, reload WoW, then preview and merge in Settings > Data > Import journal.", wraplength=640).pack(anchor="w", pady=(8, 16))
    candidates = discover_clients()
    if os.name != "nt":
        # Steam/Proton installs normally place the Windows client below a user
        # selected prefix; only inspect common WoW/Steam locations, not the disk.
        for base in (Path.home() / "Games/World of Warcraft", Path.home() / ".local/share/Steam/steamapps/common/World of Warcraft"):
            for child in (base, base / "_classic_beta_", base / "_classic_"):
                if (child / "Interface/AddOns/DingTracker/DingTracker.toc").is_file():
                    candidates.append(child)
    candidates = list(dict.fromkeys(map(str, candidates)))
    folder = tk.StringVar(value=str(initial) if initial else candidates[0] if len(candidates) == 1 else "")
    row = ttk.Frame(panel)
    row.pack(fill="x")
    ttk.Label(row, text="WoW client folder").pack(anchor="w")
    ttk.Combobox(row, textvariable=folder, values=candidates).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="Choose folder...", command=lambda: run(lambda: (folder.set(filedialog.askdirectory(title="Choose the client folder containing Interface and WTF") or folder.get()), refresh()))).pack(side="right", padx=(8, 0))
    ttk.Label(panel, text="Saved export").pack(anchor="w", pady=(16, 4))
    selected = tk.StringVar()
    picker = ttk.Combobox(panel, textvariable=selected, state="readonly")
    picker.pack(fill="x")
    status = tk.StringVar(value="Select your WoW client folder, then Refresh saved exports.")
    entries = []
    watcher = ClipboardRequestWatcher()

    def run(action):
        try:
            action()
        except Exception as error:
            messagebox.showerror("Journal transfer", str(error))

    def refresh(force=True):
        nonlocal entries
        if folder.get():
            updated, pending = watcher.poll(Path(folder.get()), force=force)
            if updated is None:
                return
            entries = updated
        else:
            entries, pending = [], []
        labels = [f'{entry["summary"]["identity"]["name"]} - {entry["summary"]["identity"]["realm"]} - Level {entry["summary"]["level"]} - {entry["summary"]["encodedBytes"]:,} characters - {time.strftime("%I:%M:%S %p", time.localtime(entry["summary"]["exportedAt"]))}' for entry in entries]
        picker.configure(values=labels)
        picker.current(0) if labels else selected.set("")
        status.set("Choose Export to file or Copy export to clipboard." if entries else "No saved export request. In WoW, open Settings > Data > Export journal > Export to file to save and reload.")
        for entry in pending:
            try:
                clipboard_write(entry["text"], root)
                status.set(f'Copied new request: {entry["summary"]["encodedBytes"]:,} characters. Paste the complete snapshot on your other computer.')
            except Exception as error:
                status.set(f'Automatic copy failed: {error}. Select the export and click Copy export to clipboard to retry.')

    def request():
        index = picker.current()
        if not 0 <= index < len(entries):
            raise ValueError("Refresh and select a saved export first")
        return entries[index]

    def save():
        entry = request()
        chosen = filedialog.asksaveasfilename(title="Save journal", defaultextension=".dingjournal", filetypes=[("Ding Tracker journal", "*.dingjournal")], initialfile="DingTracker-journal.dingjournal")
        if chosen:
            export_file(entry, Path(chosen), replace=True)
            status.set("Journal file saved. Transfer that file to your other computer.")

    def copy():
        entry = request()
        clipboard_write(entry["text"], root)
        status.set(f'Copied {entry["summary"]["encodedBytes"]:,} characters. Keep this window open until pasting on Linux.')

    def load():
        chosen = filedialog.askopenfilename(title="Open journal", filetypes=[("Ding Tracker journal", "*.dingjournal"), ("All files", "*")])
        if chosen:
            import_file(Path(folder.get()), Path(chosen))
            status.set("Import staged. Reload WoW, open Settings > Data > Import journal > Import from file, then preview and combine. Restart WoW if unavailable.")

    def paste():
        stage_text(Path(folder.get()), clipboard_read(root))
        status.set("Clipboard import staged. Reload WoW, open Settings > Data > Import journal > Paste via helper, then preview and combine. Restart WoW if unavailable.")

    ttk.Button(panel, text="Refresh saved exports", command=lambda: run(refresh)).pack(anchor="w", pady=(8, 12))
    buttons = ttk.Frame(panel)
    buttons.pack(fill="x")
    transfer_buttons = []
    for index, (caption, action) in enumerate((("Export to file...", save), ("Import from file...", load), ("Copy export to clipboard", copy), ("Import from clipboard", paste))):
        control = ttk.Button(buttons, text=caption, command=lambda callback=action: run(callback))
        control.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 8), pady=5)
        transfer_buttons.append(control)
    buttons.columnconfigure(0, weight=1)
    buttons.columnconfigure(1, weight=1)
    ttk.Label(panel, textvariable=status, wraplength=620).pack(anchor="w", pady=(16, 0))
    ttk.Label(panel, text="Existing statistics are combined only after your confirmation inside WoW.\nThis helper never changes SavedVariables or starts combat-log tracking.", wraplength=620).pack(anchor="w", pady=(12, 0))
    if check_ui:
        try:
            if folder.get():
                refresh()
            root.update_idletasks()
            assert len(transfer_buttons) == 4
            expected = {"Export to file...", "Import from file...", "Copy export to clipboard", "Import from clipboard"}
            assert {str(control.cget("text")) for control in transfer_buttons} == expected
            for index, control in enumerate(transfer_buttons):
                assert control.winfo_reqwidth() >= 60 and control.winfo_reqheight() >= 18
                layout = control.grid_info()
                assert int(layout["row"]) == index // 2 and int(layout["column"]) == index % 2
            width, height = panel.winfo_reqwidth(), panel.winfo_reqheight()
            assert 500 <= width <= 1800 and 300 <= height <= 1400, (width, height)
            print(f"Journal transfer UI constructed: four actions; requested layout {width}x{height}; Tcl/Tk resources verified.")
            return 0
        finally:
            root.destroy()
    if folder.get():
        run(refresh)
    def watch():
        try:
            if folder.get():
                refresh(force=False)
        except Exception as error:
            status.set(f'Waiting for a saved export: {error}')
        root.after(1000, watch)
    root.after(1000, watch)
    root.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wow", type=Path, help="Client folder containing Interface and WTF")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--export", type=Path, metavar="FILE", help="Write the saved request to a .dingjournal file")
    mode.add_argument("--import", dest="import_path", type=Path, metavar="FILE", help="Stage a file for explicit in-game preview")
    mode.add_argument("--copy", action="store_true", help="Copy the saved export to the OS clipboard")
    mode.add_argument("--paste", action="store_true", help="Stage the OS clipboard for explicit in-game preview")
    mode.add_argument("--validate", type=Path, metavar="FILE", help="Check a journal file without writing anything")
    mode.add_argument("--check-ui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.check_ui:
            return gui(args.wow, check_ui=True)
        if args.validate:
            if args.validate.stat().st_size > MAX_BYTES:
                raise ValueError("Journal exceeds the 4 MB transfer limit")
            validate(args.validate.read_text(encoding="utf-8-sig"))
            print("Journal checksum and bounded data format verified.")
        elif args.export or args.import_path or args.copy or args.paste:
            if not args.wow:
                parser.error("--wow is required for this operation")
            if args.export:
                export_file(latest_request(args.wow), args.export)
                print("Journal file saved.")
            elif args.import_path:
                import_file(args.wow, args.import_path)
                print("Import staged. Reload WoW, then open Settings > Data > Import journal > Import from file. Restart WoW if unavailable.")
            elif args.copy:
                clipboard_write(latest_request(args.wow)["text"])
                print("Journal copied to clipboard.")
            else:
                stage_text(args.wow, clipboard_read())
                print("Clipboard import staged. Reload WoW, then open Settings > Data > Import journal > Paste via helper. Restart WoW if unavailable.")
        else:
            return gui(args.wow)
        return 0
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        print(f"Journal transfer failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

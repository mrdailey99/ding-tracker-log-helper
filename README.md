# Ding Tracker Log Helper

This optional desktop helper brings readable combat-log facts into Ding Tracker
on WoW Forever **1.60.1**, combat-log format **22**. It runs on your computer,
outside the game. Imports are off by default and enabled separately for each
character; profiles do not enable them. This is an experimental import feature.

## Download

**[Get the Windows helper from Releases](https://github.com/mrdailey99/ding-tracker-log-helper/releases/latest).**

Under **Assets**, choose `DingTracker-LogHelper-<version>-windows-x64.zip`.
Extract the ZIP before running `DingTracker-LogHelper.exe`. **Python is not
required.** The automatic **Source code** downloads are for developers.

Install the [Ding Tracker addon](https://www.curseforge.com/wow/addons/ding-tracker)
separately. This helper download supports **64-bit Windows** and WoW: Forever;
it does not include the addon or install it for you.

## Set up

1. Install Ding Tracker **0.15.21** or newer, including
   `DingTracker_LogData`. **Fully restart WoW** after adding this new folder.
2. **In WoW, press Enter to open chat, type `/ding logs on`, and press Enter.**
   Then type **`/reload` in WoW chat and press Enter** to save your character.
   `/ding logs on` enables this character's imports and starts combat logging.
   You can also enable **Import combat logs with the desktop helper** in
   **Settings → Tracking**.
   Keep Ability damage, Creatures killed, Toughest mobs and Close calls survived
   tracking enabled for the corresponding imports.
3. Extract the **Windows x64 helper ZIP** somewhere outside
   `Interface/AddOns`. Double-click **DingTracker-LogHelper.exe**.
   **Python is not required.** If it finds one installed client, press **Enter**
   to use that folder. Otherwise choose the numbered WoW client folder, or paste
   its full path, such as
   `C:\Program Files (x86)\World of Warcraft\_classic_beta_`.
   Choose the client folder containing **Interface**, **Logs** and **WTF**,
   with `DingTracker_LogData` installed. It checks common install locations;
   installations elsewhere can be entered manually.
4. The helper automatically starts **10-second checks** in **Background** mode.
   There are no interval or mode questions. It moves to the Windows tray beside the clock;
   it may be inside the **^** menu. Right-click its icon for **Scan now** or **Exit helper**.
   Console and one-shot modes are available through command-line options below.
5. Play, then type **`/reload` in WoW chat** to save your latest progress.
   Wait for a helper scan, or choose **Scan now** from its tray menu. Type
   **`/reload` in WoW chat again** to load the results. Check `/ding logs status`.
   View **Levels → Abilities**, **Mobs** and **Deaths** for the records.

The two reloads have different jobs: the first saves your character's tracking
context; the second loads the helper's output. WoW may write combat-log lines a
little later, so a recent encounter can take another scan and reload to appear.

## If nothing appears

- Confirm `/ding logs status` shows imports enabled for the character you are playing.
- Select the **Forever client folder containing Interface, Logs and WTF**, not
  the parent World of Warcraft folder or its `Interface/AddOns` subfolder.
- After adding `DingTracker_LogData`, fully restart WoW once so it detects the folder.
- Save with `/reload`, choose **Scan now**, then `/reload` again. Check the helper
  console for unsupported-log or missing-folder messages.
- Close calls need newly recorded combat/duel markers. Older logs can contain
  readable hits and HP without enough evidence to confirm a survived close call.

## Interval and background controls

The default check interval is **10 seconds**. Use `--interval` for **5–3600 seconds**.
In Background mode, the setup console closes and the helper runs with a
**Windows notification-area icon**, possibly under the **^** arrow beside the
clock. Right-click the Ding Tracker icon to:

- **Show console / Hide console** to inspect results and return to the tray.
- **Scan now** to import immediately after saving your character in WoW.
- Choose **10, 30, 60, 120 or 300 seconds** between checks, without restarting.
- **Exit helper** to stop it.

Double-click the tray icon to show the console. Closing that console also stops
the helper. A second launch for the same client reports that it is already running.
In Console mode, close the window or press Ctrl+C to stop. Once mode scans and exits.

Each timer tick checks file sizes and modification times. It parses only when
files change or you choose Scan now. Unchanged errors are not repeatedly parsed
or printed. The helper never reloads WoW for you; enter `/reload` in game to
read the generated import. It does not start automatically with Windows.
Folder choices and custom intervals are session-only. The next launch uses the
defaults unless you provide command-line options. The helper only asks which
WoW client folder to use; Enter accepts a single detected folder. If several
clients are installed, choose one explicitly. Use **Q** to exit folder selection.

To launch with the default 10-second background checks, run:

```powershell
.\DingTracker-LogHelper.exe --wow "C:\Program Files (x86)\World of Warcraft\_classic_beta_"
```

For a single scan, run this command in PowerShell from the extracted folder:

```powershell
.\DingTracker-LogHelper.exe --wow "C:\Program Files (x86)\World of Warcraft\_classic_beta_" --once
```

Use `--foreground --interval 120` (or `--watch --interval 120`) to check every two minutes in the console, or
`--background --interval 300` to use the tray with five-minute checks.
`--wow "<client folder>" --status` shows the running interval; use the same path
with `--stop` to stop its background helper. `--dry-run` reports without writing
import data and scans once unless you also choose a watch mode.
`--help` lists options and `--version` shows the helper version.
The ZIP includes a SHA-256 checksum and embedded-runtime license notices.
The executable is unsigned. A short console folder selection starts the tray worker;
there is no installer, Windows service or Python prerequisite for users.

## What gets imported

| Record | Rules |
| --- | --- |
| Highest hit | Largest individual damage event from your exact player GUID, including overkill. Absorbed damage is not added. Pet hits and aggregate damage totals are excluded. |
| Creature HP | An observed maximum HP sample for the exact creature GUID, followed within 120 seconds by your logged `PARTY_KILL`. The same NPC must already have a credited journal kill in that level. No kills are added. |
| Close call | Reach **10% HP or less in combat**, survive the end of combat and recover to **35% HP or more**, in the same level/session with tracking enabled. Deaths, duels and missing combat/duel proof cancel the episode. Recovery may occur during combat or afterwards. |

Highest hits and creature HP can backfill logged encounters within saved level
and tracking intervals. Old logs without the new combat/duel markers cannot
confirm close calls; the helper reports low-health samples as diagnostic
candidates only.

Maximum-health changes alone cannot create or deepen a close call when a prior
health sample establishes that HP did not fall. An initial low-health sample can
still qualify when logs are sparse and combat/session/duel proof is present.
Out-of-combat damage does not change the encounter minimum. A later low-health
drop cancels earlier recovery until the player heals again.

Level/state markers have one-second precision. Ambiguous boundary seconds are
excluded. Logging gaps remain gaps; imported records describe the observed
encounters and do not claim full coverage. Repeated imports do not add damage
totals, creature kills or duplicate close calls.

## Files and updates

The helper reads `Logs/WoWCombatLog*.txt` and the saved
`DingTrackerLogContext` literal in
`WTF/Account/<account>/SavedVariables/DingTracker.lua`. It does not execute Lua.
Its only persistent output is:

```text
<WoW client directory>\Interface\AddOns\DingTracker_LogData\Data.lua
```

Run the helper again after updating the addon if your addon manager overwrote
that generated file. The repository's install script preserves it. The addon
distribution always contains a blank template, never personal log results.

Use `/ding logs off` to disable imports. Previously imported journal records
remain. Logging stops if Ding Tracker started it during this session; if logging
was already active, control it with `/combatlog`. Log files can grow large;
archive older ones when you finish recording. The helper rejects unsupported log
formats and reports malformed lines rather than guessing their layout.

## Build from source (developers only)

Python is only needed to build or run the source code. Use 64-bit Python on
Windows (CI uses Python 3.13). The build creates an isolated environment and
installs the versions in `requirements-build.txt`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

The portable ZIP and its checksum appear in `dist`. The executable bundles its
Python runtime with PyInstaller; users do not install Python or build tools.
`python log_import.py --wow "<client folder>" --dry-run` runs the source directly.

The standalone repository layout includes `tests/test_log_import.py`:

```powershell
python -m unittest discover -s tests -p test_log_import.py
$dingVersion = (Get-Content -LiteralPath VERSION -Raw).Trim()
python smoke_test.py "dist\DingTracker-LogHelper-$dingVersion-windows-x64.zip"
```

The smoke test uses a temporary synthetic client and excludes Python from the
executable's PATH. It checks the bundle contents, checksums, version, dry run,
imported hit and failure handling. It also starts a temporary tray helper,
changes its interval, triggers Scan now and stops it through the native controls.
It does not touch your real WoW installation.

## Publishing a helper update

Update `VERSION` and add matching notes to `CHANGELOG.md`, then commit and push.
In GitHub **Actions → Publish Windows helper → Run workflow**, select `main`.
The manual workflow tests the parser, builds and smoke-tests the executable,
then publishes its ZIP and checksum as a versioned GitHub Release. Select
**Build without publishing** for a validation-only run.

## License and scope

The helper source is licensed under [MIT](https://github.com/mrdailey99/ding-tracker-log-helper/blob/main/LICENSE). The portable ZIP includes
the helper license and notices for its embedded Python and PyInstaller runtime.

It reads local combat logs and Ding Tracker's saved context, and writes only
`DingTracker_LogData/Data.lua`. It does not access the network, read WoW's memory,
or control gameplay. The addon works without it. Report helper problems in
this repository's [Issues](https://github.com/mrdailey99/ding-tracker-log-helper/issues).

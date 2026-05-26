# Batch Simulation

Sequential batch runner for CLI simulators invoked as `<exe> --scene-file <path>`. Iterates through a queue of scene files, runs each one, and tracks per-case results. Supports OpenMP thread limiting, MPI launch (per simulator profile), output compression with 7-Zip, and Telegram progress notifications.

Simulator-specific behavior (display name, MPI capability, step-line marker, default switch state) is fully data-driven via the `simulator_profiles` array in `config.json`. The matching profile is picked by case-insensitive substring match on the exe path. The template ships profiles for `SPlisHSPlasH` and `CAMMP`; add more for any other CLI simulator that follows the same `--scene-file` / `[ERROR]` / `[WARNING]` / `Output directory:` conventions.

Two frontends share the same core (`simulation.py`):

- **CLI** (`batch_simu_cli.py`) — interactive prompts, suitable for SSH / minimal envs. One configuration is applied to every case in the prompt batch.
- **TUI** (`batch_simu_tui.py`) — [Textual](https://textual.textualize.io/) terminal UI with **per-case** OMP / MPI / Zip / Remove settings, status-colored queue table, live log, CPU/Memory monitor, and per-case log tabs you can pop open on demand.

## Setup

> **Prebuilt Windows x64 binaries** are attached to each [release](https://github.com/Lazy-Beee/simulation-batch-runner/releases/latest). If you only want to run, grab `batch_simu_tui.exe` (TUI) or `batch_simu_cli.exe` (CLI) plus `config.example.json` from the latest release, drop them in the same folder, copy the json to `config.json`, fill in your paths (step 2 below), and run. No Python install needed. To run from source instead, follow all three steps:

1. Copy the config template:
   ```powershell
   Copy-Item config.example.json config.json
   ```
2. Edit `config.json` for your machine:
   - `simulator.default_exe` — exe path used when the Simulator input is blank
   - `simulator.zip_path` — path to `7z.exe`
   - `simulator.zip_ext` (optional) — archive file extension; 7z picks the format from this (`.7z` for LZMA2, `.zip` for Deflate). Defaults to `.zip`.
   - `simulator.zip_args` (optional) — extra switches passed to 7z between `a` and the archive name. Defaults to `["-mx=5", "-mmt=on"]` (7z's own defaults, made explicit). Common tweaks: `-mx=1` (fastest) / `-mx=9` (max compression), `-mmt=N` to cap thread count, `[]` to keep 7z's defaults silent.
   - `simulator.zip_async` (optional, TUI only) — `true` (default) runs zip + remove on a background thread so the next case starts immediately. Set to `false` to make each case's zip block the queue until done.
   - `defaults.omp_threads` / `defaults.mpi_ranks` — fallback values used when a switch is on but its numeric input is blank
   - `simulator_profiles[]` — one entry per simulator family:
     - `name` — display label
     - `path_marker` — case-insensitive substring matched against the exe path
     - `supports_mpi` — `false` disables MPI controls and skips the MPI prompt
     - `step_pattern` — Python regex (`re.search`) matching a step / progress line in stdout. Capture group 1 (if present) is the display text; otherwise the text from the first match position is shown. Drives the Running tab's step indicator and per-step Telegram messages. Escape regex metacharacters: SPlisHSPlasH's literal `[step]` becomes `\\[step\\]`.
     - `eta_pattern` (optional) — Python regex with one capture group; applied to every step line to extract an ETA token (e.g. `7h57m`, `1h 01m`, `<1m`). The captured value is reformatted as `H:MM:SS` (matching the **Time** column) in the queue table's **ETA** column. Omit to disable ETA extraction for this profile.
     - `default_omp` / `default_mpi` (TUI only) — initial Switch state when this profile is matched; re-applied only when the matched profile *transitions*
   - `telegram.enabled` — set to `true` and fill `bot_token` / `chat_id` for notifications

   The per-case output folder is auto-detected from the simulator's log (`Output directory:` line, quoted or unquoted). If the line isn't seen, zip and remove are skipped for that case.
3. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

`config.json` is gitignored — only the template is tracked.

## Usage — TUI

```powershell
python batch_simu_tui.py
```

![Queue tab: Simulator / Scene inputs, settings row, status-coloured queue table with accent border, run controls, progress bar](https://github.com/Lazy-Beee/simulation-batch-runner/releases/latest/download/tui-queue.png)

### Top bar

A single line at the top:

```
[Batch Simulation]                       CPU 12.3% | MEM 12.3 GB / 32.0 GB    13:42:08
```

Clock and stats refresh every second. CPU/Memory require `psutil` — without it the right side shows a `(install psutil for CPU/MEM)` hint instead.

### Two tabs

- **Queue** — primary workspace: Simulator / Scene inputs, settings row, queue table, run controls, status line, progress bar.
- **Running** — current case header (`Case N/M: name`), latest step line, live elapsed / warnings / errors, streaming RichLog with a Copy button to dump it to the clipboard.

![Running tab: case / step / stats header with Copy button over an accent-bordered live RichLog](https://github.com/Lazy-Beee/simulation-batch-runner/releases/latest/download/tui-running.png)

Additionally, **per-case log tabs** can be popped open from the Queue table (see *View log*). Each carries its own Close button (or use `Ctrl+W` on the active case tab). Queue and Running are pinned and can't be closed.

![Case-log tab: Case (with Close button) / Simulator / Status-Time-Warnings-Errors-Copy header over an accent-bordered replay of the case log](https://github.com/Lazy-Beee/simulation-batch-runner/releases/latest/download/tui-case-log.png)

### Queue workflow

1. **Simulator** field — paste a path or drag a file in. The detected profile name appears below; if the path doesn't resolve to a file, a red `(file not found)` hint joins it so the bad path is caught before Add. If the profile forbids MPI (e.g. SPHSimulator), MPI controls auto-disable. The **Clear** button on the right empties the field and re-focuses it.
2. **Scene** field — paste one or more scene paths (space-separated, quote paths with spaces) or drag files in. Press *Enter* or click **Add** to enqueue. **Clear** empties the field. Underneath, a `Drag target: ...` label shows which of the two inputs the next drag-drop / paste will land in (click either input to switch).
3. **Settings row** — OMP switch + thread count, MPI switch + rank count, Zip switch, Remove switch. These act as the defaults for cases added next. Switches snap to the profile's `default_omp` / `default_mpi` only when the matched profile transitions, so a manual toggle survives further typing in the exe field.
4. **Add** — snapshots the current widget state and appends one entry per scene path. Later toggles don't retroactively affect queued items. Adding **during** a batch is fine: the worker reads the queue live and picks up the new entries as soon as the current case finishes.
5. **Queue table** — 12 columns: `# / Simulator / Scene / OMP / MPI / Zip / Rmv / Status / Time / ETA / Warnings / Errors`. Simulator / Scene cells show just the filename minus its extension (`.exe` / `.json`), head+ellipsis+tail-truncated when they would otherwise overflow their column. The two columns absorb any terminal width beyond the baseline at a 1:2 ratio (rolling all surplus to whichever side is still truncated once the other fits its widest entry; falling back to 1:2 padding when both fit). A missing scene file is flagged with a ` [!]` marker on the row. Time / Warnings / Errors tick live (1 Hz) while a case is running, and the whole table re-stats files once a second so a simulator / scene file deleted between Add and run gets flagged promptly. ETA is filled in from `step_pattern + eta_pattern` if the profile has one. Each row's background reflects its status:

   | Background | Status |
   |---|---|
   | (default) | pending |
   | yellow | running |
   | dark green | done |
   | red | failed / missing / error |
   | grey | stopped (force-stopped) |

6. **Row actions** — with a row selected (cursor on it):
   - **Up / Down** — reorder pending entries. Running / finished rows are locked in place; pending rows can't jump over a non-pending neighbour.
   - **View log** — opens (or switches to) a new tab replaying that case's captured log. Only available for finished cases (done / failed / stopped / missing / error); pending / running rows are rejected with a hint. Close the tab with its Close button or `Ctrl+W`.
   - **Remove selected** — drops the row. Allowed for pending and stopped only; done / failed / etc. stay as a record. If any rows are multi-selected (see below) it removes all of them instead.
7. **Multi-select** — `Space` toggles a checkmark (`*`) in the `#` column for the cursor row. While anything is multi-selected, the row actions switch to bulk mode: **Remove selected** drops every removable entry in the selection, **Up / Down** moves the whole group as a unit (rows that hit a boundary or a non-pending neighbour stay put while the rest still shift), and **View log** opens one tab per finished entry (pending / running members are silently skipped). `Ctrl+A` selects every row, `Esc` clears the selection.
8. **Run controls** — bottom row:
   - **START** — resets every non-pending entry back to pending (clearing previous `Time` / `Warnings` / `Errors`) and runs the whole queue.
   - **STOP** — graceful: the current case finishes naturally, then the batch exits. Remaining pending cases stay pending.
   - **FORCE STOP** — kills the current process tree (`taskkill /F /T` on Windows). The in-flight entry is marked `stopped` (removable).
   - **RESUME** — re-queues `stopped` entries as pending and runs all pending. Done / failed / missing / error rows stay as a record.
   - **Reset** (bottom-right) — wipes the queue, log, progress, per-Add exe snapshots, and closes any open case tabs. Disabled while a batch is running.

### Drag-and-drop

Drag a file from your file manager onto the terminal. The drop lands in the **Simulator** input or the **Scene** input depending on which one you last clicked / Tab-focused — the current target is shown under the Scene input as `Drag target: Simulator / Scene`. Click the desired field once before dragging to switch the target; the **Clear** button next to each input also re-focuses it (so pressing Clear then dragging will land in that field).

Mouse-position routing isn't possible because Windows OLE drag-drop is modal: the terminal doesn't receive mouse-move events while you're holding the file, so the app can't see where the drop lands.

Quoted-with-spaces paths from Windows Terminal are stripped automatically.

### Key bindings

| Key | Action |
|---|---|
| `Ctrl+S` | START |
| `Ctrl+X` | STOP (graceful) — hidden from Footer to keep key order stable while an Input is focused |
| `Ctrl+L` | Clear log |
| `Ctrl+W` | Close current case tab (Queue / Running are pinned) — hidden from Footer; inside an Input it's the Input's own delete-word |
| `Ctrl+Q` | Quit |
| `F1` / `F2` | Jump to Queue / Running |
| `Space` | Toggle multi-select on the cursor row |
| `Ctrl+A` / `Esc` | Select all / clear selection (hidden from Footer) |

## Usage — CLI

```powershell
python batch_simu_cli.py [--no-zip] [--keep-output]
```

The script prompts for:
- **Simulator exe** — path to the simulator exe (blank = use `default_exe` from config)
- **Limit OMP_NUM_THREADS** — if yes, asks for thread count (Enter = configured default)
- **Launch with MPI** — if yes, asks for rank count and uses `mpiexec -n N` (skipped when the matched profile has `supports_mpi: false`)
- **Add scene file** — paste one or more scene paths (quote paths with spaces); empty line to finish

### Flags

| Flag | Default behavior | When set |
|---|---|---|
| `--no-zip` | Each case's output folder is compressed with 7-Zip | Skip compression |
| `--keep-output` | Original output folder is deleted after successful compression | Keep the uncompressed folder |

The CLI applies one configuration to every case in the prompt batch. For per-case configuration, use the TUI.

## How it works

- Each `Add` snapshots the simulator exe into `<base>.batch<ext>` (or `<base>.batch.1<ext>`, `.batch.2<ext>`, ... if the name's taken). All scenes from the same Add share one copy; a later Add gets a fresh snapshot. The bound entries keep using their snapshot for the whole batch life cycle, so you can rebuild the source exe mid-batch and queue more cases against the new version. Copies are reference-counted and cleaned up on Remove / Reset / app exit.
- `stdout` is streamed live and parsed for the matched profile's `step_pattern`, plus `[ERROR]` / `[WARNING]` / `Output directory:`. Each line fires the appropriate event exactly once (no duplicate dispatch).
- A failed case (non-zero exit code or missing scene file) is reported and the batch continues with the next file unless the user pressed STOP.
- (TUI only) When `simulator.zip_async` is `true` (default), zip and remove run on a background thread so the next case starts as soon as the previous one's simulator exits. Tasks are serialised behind a single worker so multiple 7z runs don't thrash disk; the batch waits for any pending tasks before declaring idle. Set `zip_async: false` to make zip / remove block the queue (next case waits).
- Telegram digest at batch end summarises per-case time costs and totals.

## Requirements

- Python 3.9+
- `requests` — Telegram notifications
- `textual>=1.0` — TUI only; CLI works without it. We rely on newer API (`App.copy_to_clipboard`, `Strip.text`, `events.DescendantFocus`, `Static.text_selection`); 0.x versions are missing some of these.
- `psutil` — TUI TopBar CPU/MEM stats (optional; the TUI runs fine without)
- 7-Zip — only if you want output compression
- `mpiexec` on PATH — only if a profile sets `supports_mpi: true` and the user toggles MPI on

## Testing

`test/` ships a fake simulator and sample scenes for exercising both frontends without a real simulator build. See [test/README.md](test/README.md).

## Building binaries

The Windows x64 binaries shipped with each release are built with PyInstaller (onefile mode). To reproduce locally:

```powershell
pip install pyinstaller
.\build.bat
```

Outputs:
- `dist\batch_simu_tui.exe` (~35 MB) — Textual TUI
- `dist\batch_simu_cli.exe` (~12 MB) — interactive CLI

The TUI command uses `--collect-submodules textual` because Textual lazy-loads widget modules via `__getattr__` (e.g. `textual.widgets._tab_pane`); without it the frozen exe fails at first widget access.

`simulation.py:_app_root()` resolves `config.json` next to `sys.executable` when frozen, and next to `simulation.py` in dev mode, so the same code path covers both. Users must place `config.json` (copied from `config.example.json`) alongside the exe.

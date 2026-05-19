# Batch Simulation

Sequential batch runner for CLI simulators invoked as `<exe> --scene-file <path>`. Iterates through a list of scene files, runs each one, and produces a per-case + batch summary. Supports OpenMP thread limiting, MPI launch (per simulator profile), output compression with 7-Zip, and Telegram progress notifications.

Simulator-specific behavior (display name, MPI capability, step-line marker, initial switch defaults) is fully data-driven via the `simulator_profiles` array in `config.json`. The matching profile is picked by case-insensitive substring match on the exe path. Out of the box, the template ships with profiles for `SPlisHSPlasH` and `CAMMP`; add more for any other CLI simulator that follows the same `--scene-file` / `[ERROR]` / `[WARNING]` / `Output directory:` conventions.

Two frontends share the same core (`simulation.py`):

- **CLI** (`batch_simu.py`) — interactive prompts, suitable for SSH / minimal envs.
- **TUI** (`batch_simu_tui.py`) — [Textual](https://textual.textualize.io/) terminal UI with live log, progress bar, and Start/Stop controls.

## Setup

1. Copy the config template:
   ```powershell
   Copy-Item config.example.json config.json
   ```
2. Edit `config.json` for your machine:
   - `simulator.default_exe` — exe path used when the user leaves the input blank
   - `simulator.zip_path` — path to `7z.exe`
   - `simulator_profiles[]` — one entry per simulator family. Each entry has:
     - `name` — display label (shown in TUI Type line)
     - `path_marker` — case-insensitive substring matched against the exe path
     - `supports_mpi` — `false` disables MPI controls and skips the MPI prompt
     - `step_marker` — substring that identifies a step/progress line in stdout; used to drive the Running tab's step indicator and Telegram per-step messages
     - `default_omp` / `default_mpi` (TUI only) — initial Switch state when this profile is matched; only re-applied when the matched profile transitions, so a manual toggle won't be clobbered by typing in the exe field
   - `telegram.enabled` — set to `true` and fill `bot_token` / `chat_id` if you want notifications

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

Three-tab layout:

- **Setup** — simulator path, OMP/MPI options, scene queue, zip/remove switches, Start/Stop, overall progress
- **Running** — currently executing case header, latest `[step]` line, elapsed time / warnings / errors counters, live log
- **Done** — per-case results table (case, status, time, warnings, errors) and batch summary

Auto-switches to **Running** on Start and to **Done** when the batch finishes.

Key bindings:

| Key | Action |
|---|---|
| `Ctrl+S` | Start batch |
| `Ctrl+X` | Stop current case (terminates the subprocess, ends the batch) |
| `Ctrl+L` | Clear log |
| `Ctrl+Q` | Quit |
| `F1` / `F2` / `F3` | Jump to Setup / Running / Done tab |

## Usage — CLI

```powershell
python batch_simu.py [--no-zip] [--keep-output]
```

The script will prompt for:
- `Simulator exe`: path to the simulator exe (blank = use `default_exe` from config)
- `Limit OMP_NUM_THREADS`: cap OpenMP threads to the configured default
- `Launch with MPI`: if yes, asks for rank count and uses `mpiexec -n N` (skipped when the matched profile has `supports_mpi: false`)
- `Add scene file`: paste one or more scene paths (quote paths with spaces); empty line to finish

### Flags

| Flag | Default behavior | When set |
|---|---|---|
| `--no-zip` | Each case's output folder is compressed with 7-Zip | Skip compression |
| `--keep-output` | Original output folder is deleted after successful compression | Keep the uncompressed folder |

## How it works

- The chosen simulator exe is copied to `*.batch.exe` before running, so you can rebuild the original while a batch is in progress. The copy is deleted on exit.
- `stdout` is streamed live and parsed for `[step]`, `[ERROR]`, `[WARNING]`, and `Average time:` lines.
- A failed case (non-zero exit code or missing file) is reported and the batch continues with the next file.

## Requirements

- Python 3.9+
- `requests` (Telegram; the script still runs without notifications if `telegram.enabled` is `false`)
- `textual>=0.50` (TUI only; CLI works without it)
- 7-Zip (only if you want output compression)
- `mpiexec` on PATH (only if you choose MPI)

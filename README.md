# Batch Simulation

Batch runner for [SPlisHSPlasH](https://github.com/InteractiveComputerGraphics/SPlisHSPlasH) `SPHSimulator.exe` and **CAMMP**. Iterates through a list of scene files, runs each one, and produces a per-case + batch summary. Supports OpenMP thread limiting, MPI launch (CAMMP only), output compression with 7-Zip, and Telegram progress notifications.

Both simulators are invoked the same way (`<exe> --scene-file <path>`) and share the same `[ERROR]` / `[WARNING]` log conventions. The simulator family is auto-detected from the exe path (`SPlisHSPlasH` → SPH, `CAMMP` → CAMMP). MPI is gated by simulator: SPHSimulator has no MPI build, so MPI controls are disabled/ignored when an SPH exe is selected.

Two frontends share the same core (`simulation.py`):

- **CLI** (`batch_simu.py`) — interactive prompts, suitable for SSH / minimal envs.
- **TUI** (`batch_simu_tui.py`) — [Textual](https://textual.textualize.io/) terminal UI with live log, progress bar, and Start/Stop controls.

## Setup

1. Copy the config template:
   ```powershell
   Copy-Item config.example.json config.json
   ```
2. Edit `config.json` for your machine:
   - `simulator.default_exe` — default exe path (SPHSimulator or CAMMP); used when the user input is blank
   - `simulator.output_path` — where the simulator writes case folders
   - `simulator.zip_path` — path to `7z.exe`
   - `telegram.enabled` — set to `true` and fill `bot_token` / `chat_id` if you want notifications
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
- `SPHSimulator`: path to the simulator exe (blank = use `default_exe` from config)
- `Limit OMP_NUM_THREADS`: cap OpenMP threads to the configured default
- `Launch with MPI`: if yes, asks for rank count and uses `mpiexec -n N` (skipped for SPH)
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

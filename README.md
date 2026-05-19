# Batch Simulation

Batch runner for [SPlisHSPlasH](https://github.com/InteractiveComputerGraphics/SPlisHSPlasH) `SPHSimulator.exe`. Iterates through a list of scene files, runs each one, and produces a per-case + batch summary. Supports OpenMP thread limiting, MPI launch, output compression with 7-Zip, and Telegram progress notifications.

## Setup

1. Copy the config template:
   ```powershell
   Copy-Item config.example.json config.json
   ```
2. Edit `config.json` for your machine:
   - `simulator.default_exe` — path to `SPHSimulator.exe`
   - `simulator.output_path` — where the simulator writes case folders
   - `simulator.zip_path` — path to `7z.exe`
   - `telegram.enabled` — set to `true` and fill `bot_token` / `chat_id` if you want notifications
   - `sequential_tasks` — map a label to an executable to launch after the batch finishes (e.g. `prime95`)
3. Install the one third-party dependency:
   ```powershell
   pip install requests
   ```

`config.json` is gitignored — only the template is tracked.

## Usage

```powershell
python batch_simu.py [--no-zip] [--keep-output] [--sequential-task <name>]
```

The script will prompt for:
- `SPHSimulator`: path to the simulator exe (blank = use `default_exe` from config)
- `Limit OMP_NUM_THREADS`: cap OpenMP threads to the configured default
- `Launch with MPI`: if yes, asks for rank count and uses `mpiexec -n N`
- `Add scene file`: paste one or more scene paths (quote paths with spaces); empty line to finish

### Flags

| Flag | Default behavior | When set |
|---|---|---|
| `--no-zip` | Each case's output folder is compressed with 7-Zip | Skip compression |
| `--keep-output` | Original output folder is deleted after successful compression | Keep the uncompressed folder |
| `--sequential-task <name>` | None | After the batch finishes, launch the executable mapped to `<name>` in `config.json` (e.g. `--sequential-task P95`) |

## How it works

- The chosen simulator exe is copied to `*.batch.exe` before running, so you can rebuild the original while a batch is in progress. The copy is deleted on exit.
- `stdout` is streamed live and parsed for `[step]`, `[ERROR]`, `[WARNING]`, and `Average time:` lines.
- A failed case (non-zero exit code or missing file) is reported and the batch continues with the next file.

## Requirements

- Python 3.8+
- `requests` (only for Telegram; the script still runs without notifications if `telegram.enabled` is `false`)
- 7-Zip (only if you want output compression)
- `mpiexec` on PATH (only if you choose MPI)

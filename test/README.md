# Test setup

A fake simulator that mimics SPHSimulator / CAMMP stdout for manual TUI/CLI testing — no real simulator build needed.

Generates SPH-style `[step]` lines at a configurable rate, with optional `[WARNING]` / `[ERROR]` injections and failure points.

## Files

- `fake_simulator.py` — the script
- `fake_simulator.bat` — Windows wrapper that calls `python fake_simulator.py %*`
- `scene_*.json` — sample scene files exercising different paths

## How to play

> **Before starting**: on the **Queue** tab, uncheck **Zip output** for scenes that don't set `output_files` — the fake doesn't create a real output folder for them, and the zip step would log an error per case (no files are touched, just noisy). `scene_output.json` is the exception: it writes a dummy output folder so you can leave Zip on to exercise that pipeline.

1. Launch the TUI:
   ```powershell
   python batch_simu_tui.py
   ```
2. On the **Queue** tab, in the **Simulator** field, replace the default with:
   ```
   test\fake_simulator.bat
   ```
   (The type label will show "Type: unknown" — that's fine; MPI stays enabled but the fake ignores it.)
3. Paste one or more scene files into the **Scene** field, hit Enter:
   ```
   test\scene_clean.json test\scene_warnings.json test\scene_errors.json
   ```
4. Hit **START** (or `Ctrl+S`) — the app auto-jumps to the **Running** tab. Watch the live log, the current step indicator, and the per-case warning/error counter.
5. When the batch finishes it auto-switches back to **Queue**, where the queue table now shows status / time / warnings / errors per row.
6. `F1` / `F2` jump between Queue and Running at any time. Select a finished row in the queue and click **View log** to pop its captured log into its own tab; `Ctrl+W` closes the current case tab.

## Scenes

| File | Behavior |
|---|---|
| `scene_clean.json` | 8 steps, no errors/warnings, exits 0 |
| `scene_warnings.json` | 6 steps with 2 `[WARNING]`, exits 0 |
| `scene_errors.json` | 5 steps with 1 warning + 2 errors, exits 0 |
| `scene_fail.json` | Fails at step 3 (exit 1) — tests failure handling |
| `scene_long.json` | 30 steps with `eta_per_step: 30` so the ETA column ticks down from `14h30m` — exercises long streaming + the Stop button |
| `scene_output.json` | 4 steps that write 3 x 4 KiB dummy files into `test\output_scene_output\` and emit an `Output directory:` marker — exercises the zip + remove pipeline. Run with **Zip output** on. |

> The ETA column only shows values when the matched simulator profile sets `eta_pattern` and the simulator emits a matching token. Because `test\fake_simulator.bat` doesn't match the `SPlisHSPlasH` / `CAMMP` path markers, it defaults to "Type: unknown" and ETA stays `-`. To see ETA extraction with the fake simulator, add a profile to `config.json` matching the fake exe:
> ```json
> {
>   "name": "fake test sim",
>   "path_marker": "fake_simulator",
>   "supports_mpi": false,
>   "step_pattern": "\\[step\\] (.*)",
>   "default_omp": false,
>   "default_mpi": false,
>   "eta_pattern": "eta:\\s*(\\S+)"
> }
> ```

## CLI alternative

```powershell
python batch_simu_cli.py
```
At the prompt, type `test\fake_simulator.bat` for the simulator and paste scene paths separated by spaces.

## Customizing scenes

| Key | Type | Default | Effect |
|---|---|---|---|
| `steps` | int | 10 | how many `[step]` lines |
| `step_time` | float | 0.2 | seconds between steps |
| `warnings_at` | list | `[]` | step indices that emit `[WARNING]` |
| `errors_at` | list | `[]` | step indices that emit `[ERROR]` |
| `fail_at` | int | `null` | step index after which to exit 1 |
| `eta_per_step` | int | 1 | minutes of virtual time per step; drives the `eta: XhYm` token on every step line |
| `output_files` | int | 0 | write N dummy files into `<scene_dir>\output_<scene_stem>\` and emit an `Output directory:` marker. 0 = no output. |
| `output_file_size` | int | 1024 | bytes per dummy file |

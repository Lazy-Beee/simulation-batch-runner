"""Fake simulator that mimics SPHSimulator / CAMMP stdout for manual TUI/CLI testing.

Reads a JSON scene file with these optional keys:
    steps          int    number of simulation steps (default 10)
    step_time      float  seconds per step (default 0.2)
    warnings_at    list   step indices that emit a [WARNING] line (default [])
    errors_at      list   step indices that emit an [ERROR] line (default [])
    fail_at        int    step index after which to exit with code 1 (default null)
    eta_per_step   int    minutes of virtual time each step represents,
                          used to emit an `eta: XhYm` field on every step
                          line so the batch runner's ETA column has
                          something to show (default 1).
"""

import argparse
import json
import sys
import time


def _format_eta(remaining_minutes):
    """Format remaining time so the batch runner's eta_pattern can parse it.
    Matches the SPlisHSPlasH/CAMMP display: `<1m`, `45m`, or `7h57m`."""
    if remaining_minutes <= 0:
        return "<1m"
    if remaining_minutes < 60:
        return f"{remaining_minutes}m"
    return f"{remaining_minutes // 60}h{remaining_minutes % 60}m"


def run_synthetic(scene, scene_path):
    steps = int(scene.get("steps", 10))
    step_time = float(scene.get("step_time", 0.2))
    warnings_at = set(scene.get("warnings_at", []))
    errors_at = set(scene.get("errors_at", []))
    fail_at = scene.get("fail_at")
    eta_per_step = int(scene.get("eta_per_step", 1))

    print(f"Fake simulator | scene: {scene_path}", flush=True)
    print(f"Total steps: {steps}, step time: {step_time:.3f}s", flush=True)

    elapsed = []
    for i in range(1, steps + 1):
        time.sleep(step_time)
        sim_t = i * 0.01
        eta_min = (steps - i) * eta_per_step
        print(f"--- [step] {i}, t={sim_t:.4f}, dt=0.01, eta: {_format_eta(eta_min)}", flush=True)
        elapsed.append(step_time)

        if i in warnings_at:
            print(f"[WARNING] sample warning at step {i}", flush=True)
        if i in errors_at:
            print(f"[ERROR] sample error at step {i}", flush=True)

        if fail_at is not None and i >= int(fail_at):
            print(f"[ERROR] failing intentionally at step {i}", flush=True)
            sys.exit(1)

    print("Done", flush=True)
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene-file", required=True)
    args = parser.parse_args()

    with open(args.scene_file, "r", encoding="utf-8") as f:
        scene = json.load(f)

    run_synthetic(scene, args.scene_file)


if __name__ == "__main__":
    main()

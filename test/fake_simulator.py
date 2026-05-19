"""Fake simulator that mimics SPHSimulator / CAMMP stdout for manual TUI/CLI testing.

Reads a JSON scene file with these optional keys:
    steps        int    number of simulation steps (default 10)
    step_time    float  seconds per step (default 0.2)
    warnings_at  list   step indices that emit a [WARNING] line (default [])
    errors_at    list   step indices that emit an [ERROR] line (default [])
    fail_at      int    step index after which to exit with code 1 (default null)
"""

import argparse
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene-file", required=True)
    args = parser.parse_args()

    with open(args.scene_file, "r", encoding="utf-8") as f:
        scene = json.load(f)

    steps = int(scene.get("steps", 10))
    step_time = float(scene.get("step_time", 0.2))
    warnings_at = set(scene.get("warnings_at", []))
    errors_at = set(scene.get("errors_at", []))
    fail_at = scene.get("fail_at")

    print(f"Fake simulator | scene: {args.scene_file}", flush=True)
    print(f"Total steps: {steps}, step time: {step_time:.3f}s", flush=True)

    elapsed = []
    for i in range(1, steps + 1):
        time.sleep(step_time)
        sim_t = i * 0.01
        print(f"--- [step] {i}, t={sim_t:.4f}, dt=0.01", flush=True)
        elapsed.append(step_time)

        if i in warnings_at:
            print(f"[WARNING] sample warning at step {i}", flush=True)
        if i in errors_at:
            print(f"[ERROR] sample error at step {i}", flush=True)

        if fail_at is not None and i >= int(fail_at):
            print(f"[ERROR] failing intentionally at step {i}", flush=True)
            sys.exit(1)

    avg = sum(elapsed) / len(elapsed) if elapsed else 0
    print(f"Average time: {avg:.4f} sec/step", flush=True)
    print("Done", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()

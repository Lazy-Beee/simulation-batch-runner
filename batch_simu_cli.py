"""CLI frontend for batch simulation."""

import os
import shlex
import argparse

from simulation import (
    Simulator, load_config, profile_name, profile_supports_mpi,
    CLEANUP_KEEP, CLEANUP_FOLDER, CLEANUP_BOTH,
)


def prompt_exe(simulator: Simulator) -> str:
    exe_path = input("Simulator exe: ").strip().replace('"', "")
    if not exe_path or not exe_path.lower().endswith(".exe") or not os.path.isfile(exe_path):
        if exe_path:
            if not exe_path.lower().endswith(".exe"):
                print("Error: The file must have a .exe extension.")
            elif not os.path.isfile(exe_path):
                print("Error: File does not exist.")
        exe_path = simulator.default_exe
        print(f"Using default simulator '{os.path.basename(exe_path)}'")
    return exe_path


def prompt_omp(simulator: Simulator):
    """Return an int thread count, or None for no limit."""
    while True:
        ans = input("Limit OMP_NUM_THREADS? (Y/N): ").strip().upper()
        if ans == "Y":
            while True:
                t = input(f"Number of OMP threads (default {simulator.default_omp_threads}): ").strip()
                if t == "":
                    return simulator.default_omp_threads
                try:
                    n = int(t)
                    if n < 1:
                        print("Thread count must be at least 1.")
                        continue
                    return n
                except ValueError:
                    print("Invalid number.")
        if ans == "N":
            return None
        print("Invalid input. Please type Y or N.")


def prompt_mpi(simulator: Simulator) -> int:
    while True:
        ans = input("Launch with MPI? (Y/N): ").strip().upper()
        if ans == "Y":
            while True:
                rank_input = input(f"Number of MPI ranks (default {simulator.default_mpi_ranks}): ").strip()
                if rank_input == "":
                    return simulator.default_mpi_ranks
                try:
                    ranks = int(rank_input)
                    if ranks < 1:
                        print("Rank count must be at least 1.")
                        continue
                    return ranks
                except ValueError:
                    print("Invalid number.")
        if ans == "N":
            return 0
        print("Invalid input. Please type Y or N.")


def prompt_files() -> list:
    files = []
    while True:
        text = input("Add scene file: ").strip()
        if not text:
            break
        try:
            # posix=False keeps Windows backslashes intact (POSIX mode would
            # eat them as escape chars). Outer quote tokens get stripped below.
            paths = shlex.split(text, posix=False)
        except ValueError as e:
            print(f"Error parsing input: {e}")
            continue
        for p in paths:
            if len(p) >= 2 and p[0] == p[-1] and p[0] in ('"', "'"):
                p = p[1:-1]
            if not os.path.exists(p):
                print(f"File '{p}' not found.")
            files.append(p)
    return files


def main():
    parser = argparse.ArgumentParser(description="Run a batch simulation with optional output handling.")
    parser.add_argument("--no-zip", action="store_true", help="Do not zip the output files.")
    parser.add_argument("--keep-output", action="store_true", help="Keep the raw output folder after zipping (default: delete it, keep the archive).")
    parser.add_argument("--purge", action="store_true", help="After a successful upload, delete both the output folder and the local archive. Implies folder removal; overrides --keep-output.")
    parser.add_argument("--no-upload", action="store_true", help="Do not upload the archive to the configured rclone remote.")
    args = parser.parse_args()

    config = load_config()
    sim = Simulator(config)

    exe_path = prompt_exe(sim)
    profile = sim.identify_profile(exe_path)
    sim_name = profile_name(profile)
    print(f"[INFO] Simulator profile: {sim_name}")
    batch_exe = sim.prepare_exe(exe_path)

    omp_threads = prompt_omp(sim)
    sim.set_omp_env(omp_threads)
    if omp_threads is None:
        print("[INFO] No OMP limit applied - system default.")
    else:
        print(f"[INFO] OMP_NUM_THREADS set to {omp_threads}.")

    if profile_supports_mpi(profile):
        mpi_ranks = prompt_mpi(sim)
        if mpi_ranks > 0:
            print(f"[INFO] MPI enabled: {mpi_ranks} ranks via mpiexec.")
        else:
            print("[INFO] MPI disabled - single-process.")
    else:
        mpi_ranks = 0
        print(f"[INFO] {sim_name} does not support MPI - single-process only.")

    scene_files = prompt_files()

    zip_output = not args.no_zip
    if args.purge:
        cleanup = CLEANUP_BOTH
    elif args.keep_output:
        cleanup = CLEANUP_KEEP
    else:
        cleanup = CLEANUP_FOLDER
    upload_output = sim.upload_enabled and not args.no_upload

    sim.info("Start processing", tag="Batch")
    sim.tg.queue_message("#Batch Batch settings:")
    sim.tg.queue_message(f"Zip output: {'True' if zip_output else 'False'}")
    sim.tg.queue_message(f"Cleanup: {cleanup}")
    sim.tg.queue_message(f"Upload output: {'True' if upload_output else 'False'}")
    sim.tg.queue_message(f"MPI: {f'{mpi_ranks} ranks' if mpi_ranks > 0 else 'disabled'}")
    sim.tg.queue_message(f"OMP threads: {os.environ.get('OMP_NUM_THREADS', 'system default')}")
    sim.tg.send_telegram_message_batch()

    time_costs = []
    case_names = []
    total_warnings = 0
    total_errors = 0
    total_failures = 0
    total = len(scene_files)

    for i, file_path in enumerate(scene_files):
        case_name = sim.case_name_from_path(file_path)
        case_names.append(case_name)
        if not os.path.exists(file_path):
            time_costs.append(-1)
            total_failures += 1
            sim.info(f"File '{file_path}' not found.", tag="Case")
            continue

        def on_line(line, kind):
            print(line, end="")

        result = sim.run_case(batch_exe, file_path, i, total, mpi_ranks, on_line=on_line)
        total_warnings += result.warnings
        total_errors += result.errors

        if result.returncode == 0:
            time_costs.append(result.elapsed)
            if zip_output:
                if not result.output_folder:
                    sim.info(
                        f"No output directory detected in log for '{case_name}'; skipping zip/upload/cleanup.",
                        tag="Case",
                    )
                else:
                    zipped = sim.zip_case_output(case_name, result.output_folder)
                    archive = f"{result.output_folder}{sim.zip_ext}"
                    uploaded = False
                    if zipped and upload_output:
                        uploaded = sim.upload_case_output(case_name, archive)
                    sim.cleanup_case(
                        case_name, result.output_folder, archive,
                        cleanup, zipped, uploaded,
                    )
        else:
            time_costs.append(-1)
            total_failures += 1

    sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)

    sim.info("All done", tag="Batch")

    if sim.cleanup_exe(batch_exe):
        print(f"'{batch_exe}' removed successfully.")
    else:
        print(f"'{batch_exe}' does not exist.")


if __name__ == "__main__":
    main()

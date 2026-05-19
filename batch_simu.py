"""CLI frontend for batch simulation."""

import os
import shlex
import argparse

from simulation import Simulator, load_config


def prompt_exe(simulator: Simulator) -> str:
    exe_path = input("SPHSimulator: ").strip().replace('"', "")
    if not exe_path or not exe_path.lower().endswith(".exe") or not os.path.isfile(exe_path):
        if exe_path:
            if not exe_path.lower().endswith(".exe"):
                print("Error: The file must have a .exe extension.")
            elif not os.path.isfile(exe_path):
                print("Error: File does not exist.")
        exe_path = simulator.default_exe
        print(f"Using default simulator '{os.path.basename(exe_path)}'")
    return exe_path


def prompt_omp(simulator: Simulator) -> bool:
    while True:
        ans = input(f"Limit OMP_NUM_THREADS to {simulator.default_omp_threads}? (Y/N): ").strip().upper()
        if ans == "Y":
            return True
        if ans == "N":
            return False
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
            paths = shlex.split(text)
        except ValueError as e:
            print(f"Error parsing input: {e}")
            continue
        for p in paths:
            if not os.path.exists(p):
                print(f"File '{p}' not found.")
            files.append(p)
    return files


def main():
    parser = argparse.ArgumentParser(description="Run a batch simulation with optional output handling.")
    parser.add_argument("--no-zip", action="store_true", help="Do not zip the output files.")
    parser.add_argument("--keep-output", action="store_true", help="Keep the original output files after processing.")
    parser.add_argument("--sequential-task", type=str, default="", help="Specify the sequential task to perform.")
    args = parser.parse_args()

    config = load_config()
    sim = Simulator(config)

    exe_path = prompt_exe(sim)
    batch_exe = sim.prepare_exe(exe_path)

    if prompt_omp(sim):
        sim.set_omp_env(sim.default_omp_threads)
        print(f"[INFO] OMP_NUM_THREADS set to {sim.default_omp_threads}.")
    else:
        sim.set_omp_env(None)
        print("[INFO] No OMP limit applied - system default.")

    mpi_ranks = prompt_mpi(sim)
    if mpi_ranks > 0:
        print(f"[INFO] MPI enabled: {mpi_ranks} ranks via mpiexec.")
    else:
        print("[INFO] MPI disabled - single-process.")

    scene_files = prompt_files()

    zip_output = not args.no_zip
    remove_output = not args.keep_output
    sequential_task = args.sequential_task

    sim.info("Start processing", tag="Batch")
    sim.tg.queue_message("#Batch Batch settings:")
    sim.tg.queue_message(f"Zip output: {'True' if zip_output else 'False'}")
    sim.tg.queue_message(f"Remove output: {'True' if remove_output else 'False'}")
    sim.tg.queue_message(f"Sequential task: {sequential_task if sequential_task else 'None'}")
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
            if kind == "raw":
                print(line, end="")

        result = sim.run_case(batch_exe, file_path, i, total, mpi_ranks, on_line=on_line)
        total_warnings += result.warnings
        total_errors += result.errors

        if result.returncode == 0:
            time_costs.append(result.elapsed)
            if zip_output:
                zipped = sim.zip_case_output(case_name)
                if remove_output:
                    if zipped:
                        sim.remove_case_output(case_name)
                    else:
                        sim.info(f"Output removal cancelled for of case '{case_name}'", tag="Case")
        else:
            time_costs.append(-1)
            total_failures += 1

    sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)

    err = sim.start_post_task(sequential_task)
    if err:
        sim.info(err, tag="Batch")
    elif sequential_task:
        sim.info(f"Sequential task '{sequential_task}' started.", tag="Batch")

    sim.info("All done", tag="Batch")

    if sim.cleanup_exe(batch_exe):
        print(f"'{batch_exe}' removed successfully.")
    else:
        print(f"'{batch_exe}' does not exist.")


if __name__ == "__main__":
    main()

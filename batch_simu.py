import os
import re
import json
import time
import shlex
import shutil
import datetime
import argparse
import subprocess
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    if not CONFIG_PATH.is_file():
        print(f"[ERROR] Config file not found: {CONFIG_PATH}")
        print("Please create config.json alongside this script.")
        raise SystemExit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class TelegramNotice:
    def __init__(self, tg_config):
        self.enabled = tg_config.get("enabled", False)
        self.bot_token = tg_config.get("bot_token", "")
        self.chat_id = tg_config.get("chat_id", "")
        self.message_batch = []

    def request_message(self, message):
        if not self.enabled or not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": message, "parse_mode": "MarkdownV2"}
        try:
            requests.post(url, data=data, timeout=1)
        except requests.exceptions.Timeout:
            print("Request timed out after 1 second. Skipping.")
        except requests.exceptions.RequestException as e:
            print(f"An error occurred: {e}")

    def escape_markdown_v2(self, text):
        special_chars = r'_*[]()~`>#+-=|{}.!'
        return re.sub(r'([{}])'.format(re.escape(special_chars)), r'\\\1', text)

    def send_telegram_message_batch(self):
        if self.message_batch:
            self.request_message(self.escape_markdown_v2(self.message_batch[0]) + "\n```\n" + "\n".join(self.message_batch[1:]) + "\n```")
            self.message_batch.clear()

    def queue_message(self, message):
        self.message_batch.append(message)

    def send_message(self, message, print_to_console=True, tag="", mono=False, escape=True):
        message_tg = message
        if tag:
            message_tg = f"#{tag} {message_tg}"
            message = f"[{tag}] {message}"
        if mono:
            message_tg = "`" + message + "`"
        if escape:
            message_tg = self.escape_markdown_v2(message_tg)

        self.request_message(message_tg)

        if print_to_console:
            print(message)


class BatchSimulation:
    def __init__(self, config):
        sim = config.get("simulator", {})
        self.default_simulator = sim.get("default_exe", "")
        self.output_path = sim.get("output_path", "")
        self.zip_path = sim.get("zip_path", "")

        defaults = config.get("defaults", {})
        self.default_omp_threads = defaults.get("omp_threads", 24)
        self.default_mpi_ranks = defaults.get("mpi_ranks", 4)

        self.sequential_tasks = config.get("sequential_tasks", {})

        self.tg = TelegramNotice(config.get("telegram", {}))

        self.exe_path = self.get_exe()
        self.setup_omp()
        self.mpi_ranks = self.setup_mpi()
        self.file_paths = self.get_files()

        self.time_cost = []
        self.case_names = []
        self.total_warning = 0
        self.total_error = 0
        self.total_failure = 0

    def get_exe(self):
        exe_path = input("SPHSimulator: ").strip().replace("\"", "")

        if not exe_path or not exe_path.lower().endswith(".exe") or not os.path.isfile(exe_path):
            if exe_path:
                if not exe_path.lower().endswith(".exe"):
                    print("Error: The file must have a .exe extension.")
                elif not os.path.isfile(exe_path):
                    print("Error: File does not exist.")

            exe_path = self.default_simulator
            print(f"Using default simulator '{os.path.basename(exe_path)}'")

        dir_name, file_name = os.path.split(exe_path)
        new_name = file_name.replace(".exe", ".batch.exe")
        new_path = os.path.join(dir_name, new_name)
        count = 1
        while os.path.isfile(new_path):
            if count == 1:
                new_path = new_path.replace(".exe", f".{count}.exe")
            else:
                new_path = new_path.replace(f".{count-1}.exe", f".{count}.exe")
            count += 1
        shutil.copy2(exe_path, new_path)

        return new_path

    def setup_omp(self):
        while True:
            use_limit = input(f"Limit OMP_NUM_THREADS to {self.default_omp_threads}? (Y/N): ").strip().upper()
            if use_limit == 'Y':
                os.environ["OMP_NUM_THREADS"] = str(self.default_omp_threads)
                print(f"[INFO] OMP_NUM_THREADS set to {self.default_omp_threads}.")
                return
            if use_limit == 'N':
                print("[INFO] No OMP limit applied - system default.")
                if "OMP_NUM_THREADS" in os.environ:
                    del os.environ["OMP_NUM_THREADS"]
                return
            print("Invalid input. Please type Y or N.")

    def setup_mpi(self):
        while True:
            use_mpi = input("Launch with MPI? (Y/N): ").strip().upper()
            if use_mpi == 'Y':
                while True:
                    rank_input = input(f"Number of MPI ranks (default {self.default_mpi_ranks}): ").strip()
                    if rank_input == '':
                        ranks = self.default_mpi_ranks
                        break
                    try:
                        ranks = int(rank_input)
                        if ranks < 1:
                            print("Rank count must be at least 1.")
                            continue
                        break
                    except ValueError:
                        print("Invalid number.")
                print(f"[INFO] MPI enabled: {ranks} ranks via mpiexec.")
                return ranks
            if use_mpi == 'N':
                print("[INFO] MPI disabled - single-process.")
                return 0
            print("Invalid input. Please type Y or N.")

    def get_files(self):
        file_paths = []
        while True:
            input_string = input("Add scene file: ").strip()
            if not input_string:
                break
            try:
                paths = shlex.split(input_string)
            except ValueError as e:
                print(f"Error parsing input: {e}")
                continue
            for p in paths:
                if not os.path.exists(p):
                    print(f"File '{p}' not found.")
                file_paths.append(p)
        return file_paths

    def run_simulation(self, i, file_path, zip_output, remove_output):
        start_time = time.time()
        case_name = os.path.basename(file_path)[:-5]
        self.case_names.append(case_name)

        if not os.path.exists(file_path):
            self.time_cost.append(-1)
            self.total_failure += 1
            self.tg.send_message(f"File '{file_path}' not found.", tag="Case")
            return

        self.tg.queue_message(f"#Case Summary '{case_name}' ({i+1}/{len(self.file_paths)}):")
        self.tg.send_message(
            f"Processing '{case_name}' with '{os.path.basename(self.exe_path)}' ({i+1}/{len(self.file_paths)})",
            tag="Case",
        )

        if self.mpi_ranks > 0:
            cmd = ["mpiexec", "-n", str(self.mpi_ranks), self.exe_path, "--scene-file", file_path]
        else:
            cmd = [self.exe_path, "--scene-file", file_path]

        process = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
        )

        average_printed = False
        for line in process.stdout:
            print(line, end="")
            if "[step]" in line:
                line_terms = line.split("]")[-1].split(", ")
                line_processed = line_terms[1].strip() + ", " + line_terms[-1].strip()
                self.tg.send_message(
                    f"({i+1}/{len(self.file_paths)}) {line_processed}",
                    print_to_console=False,
                    tag="Case",
                )
            elif "[ERROR]" in line:
                error_text = line.split("[ERROR]")[-1]
                self.tg.queue_message(f"Error: {error_text}")
                self.total_error += 1
            elif "[WARNING]" in line:
                warning_text = line.split("[WARNING]")[-1]
                self.tg.queue_message(f"Warning: {warning_text}")
                self.total_warning += 1

            if "Average time:" in line:
                if not average_printed:
                    self.tg.queue_message("Average time: ")
                    average_printed = True
                self.tg.queue_message(line[len("Average time: ") - 1:].strip())

        process.wait()

        if process.returncode == 0:
            time_diff = round(time.time() - start_time)
            self.time_cost.append(time_diff)
            self.tg.queue_message(f"\nCase cost: {datetime.timedelta(seconds=time_diff)}")
            self.tg.send_telegram_message_batch()
            self.tg.send_message(f"Successfully processed '{case_name}'", tag="Case")

            zip_success = False
            if zip_output:
                zip_success = self.zip_output(case_name)
            if remove_output:
                if zip_success:
                    self.remove_output(case_name)
                else:
                    self.tg.send_message(f"Output removal cancelled for of case '{case_name}'", tag="Case")
        else:
            self.time_cost.append(-1)
            self.total_failure += 1
            self.tg.send_telegram_message_batch()
            self.tg.send_message(f"Error processing '{case_name}'", tag="Case")

    def zip_output(self, case_name):
        output_folder = os.path.join(self.output_path, case_name)
        zip_file = f"{output_folder}.zip"
        try:
            subprocess.run([self.zip_path, 'a', zip_file, output_folder], check=True)
            self.tg.send_message(f"Compressed output of case '{case_name}'", tag="Case")
            return True
        except subprocess.CalledProcessError as e:
            self.tg.send_message(f"Error during compression output of case '{case_name}':\n{e}", tag="Case")
            os.remove(zip_file)
            return False

    def remove_output(self, case_name):
        output_folder = os.path.join(self.output_path, case_name)
        try:
            shutil.rmtree(output_folder)
            self.tg.send_message(f"Removed output of case '{case_name}'", tag="Case")
        except Exception as e:
            self.tg.send_message(f"Error while deleting output of case '{case_name}':\n{e}", tag="Case")

    def batch_report(self):
        self.tg.queue_message("#Batch Process summary:")
        self.tg.queue_message(
            f"Total: {len(self.file_paths)}\nFailure: {self.total_failure}\nError: {self.total_error}\nWarning: {self.total_warning}"
        )
        self.tg.queue_message("\nCase costs:")
        for i, file_path in enumerate(self.file_paths):
            self.tg.queue_message(f"{i+1}. {self.case_names[i]}: {datetime.timedelta(seconds=self.time_cost[i])}")
        self.tg.send_telegram_message_batch()

    def cleanup(self):
        if os.path.exists(self.exe_path):
            os.remove(self.exe_path)
            print(f"'{self.exe_path}' removed successfully.")
        else:
            print(f"'{self.exe_path}' does not exist.")

    def start_sequential_task(self, task):
        if not task:
            return

        task_exe = self.sequential_tasks.get(task)
        if not task_exe:
            self.tg.send_message(f"Sequential task '{task}' not found in config.", tag="Batch")
            return
        if not os.path.isfile(task_exe):
            self.tg.send_message(f"Sequential task '{task}' executable not found: {task_exe}", tag="Batch")
            return

        subprocess.Popen([task_exe])
        self.tg.send_message(f"Sequential task '{task}' started.", tag="Batch")

    def process(self, zip_output=False, remove_output=False, sequential_task=""):
        self.tg.send_message("Start processing", tag="Batch")
        self.tg.queue_message("#Batch Batch settings:")
        zip_status = "True" if zip_output else "False"
        remove_status = "True" if remove_output else "False"
        task_status = sequential_task if sequential_task else "None"
        mpi_status = f"{self.mpi_ranks} ranks" if self.mpi_ranks > 0 else "disabled"
        omp_status = os.environ.get("OMP_NUM_THREADS", "system default")
        self.tg.queue_message(f"Zip output: {zip_status}")
        self.tg.queue_message(f"Remove output: {remove_status}")
        self.tg.queue_message(f"Sequential task: {task_status}")
        self.tg.queue_message(f"MPI: {mpi_status}")
        self.tg.queue_message(f"OMP threads: {omp_status}")
        self.tg.send_telegram_message_batch()

        for i, file_path in enumerate(self.file_paths):
            self.run_simulation(i, file_path, zip_output, remove_output)
        self.batch_report()
        self.start_sequential_task(sequential_task)
        self.tg.send_message("All done", tag="Batch")
        self.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a batch simulation with optional output handling.")
    parser.add_argument("--no-zip", action="store_true", help="Do not zip the output files.")
    parser.add_argument("--keep-output", action="store_true", help="Keep the original output files after processing.")
    parser.add_argument("--sequential-task", type=str, default="", help="Specify the sequential task to perform.")
    args = parser.parse_args()

    config = load_config()
    BatchSimulation(config).process(not args.no_zip, not args.keep_output, args.sequential_task)

"""Core batch simulation logic shared by the CLI and TUI frontends."""

import os
import re
import json
import time
import shutil
import datetime
import subprocess
from pathlib import Path
from typing import NamedTuple, Callable, Optional, List

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Config file not found: {CONFIG_PATH}\n"
            "Copy config.example.json to config.json and edit it."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class CaseResult(NamedTuple):
    returncode: int
    elapsed: int
    warnings: int
    errors: int
    output_folder: Optional[str]


def extract_output_dir(line: str) -> Optional[str]:
    """Pull the case output directory out of a log line.

    Two payload formats are accepted after the 'Output directory:' marker:
        - bare path, taken verbatim:   'Output directory: C:/forward/slashes/case'
        - quoted path with backslashes JSON-escaped (\\\\ -> \\):
                                       'Output directory: "C:\\\\back\\\\slashes\\\\case"'
    Returns the resolved path, or None if the line doesn't contain the marker.
    """
    idx = line.find("Output directory:")
    if idx < 0:
        return None
    rest = line[idx + len("Output directory:"):].strip()
    if not rest:
        return None
    if rest.startswith('"'):
        end = rest.find('"', 1)
        if end < 0:
            return None
        path = rest[1:end].replace("\\\\", "\\")
        return path or None
    return rest


def detect_simulator_type(exe_path: str) -> str:
    """Identify the simulator family from the exe path.

    Returns 'sph' if the path contains 'SPlisHSPlasH', 'cammp' if it contains
    'CAMMP', otherwise 'unknown'. Case-insensitive.
    """
    p = exe_path.lower()
    if "splishsplash" in p:
        return "sph"
    if "cammp" in p:
        return "cammp"
    return "unknown"


def simulator_supports_mpi(sim_type: str) -> bool:
    """SPHSimulator has no MPI build; CAMMP does. Unknown defaults to allow."""
    return sim_type != "sph"


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
        except requests.exceptions.RequestException:
            pass

    def escape_markdown_v2(self, text):
        special_chars = r'_*[]()~`>#+-=|{}.!'
        return re.sub(r'([{}])'.format(re.escape(special_chars)), r'\\\1', text)

    def send_telegram_message_batch(self):
        if self.message_batch:
            self.request_message(
                self.escape_markdown_v2(self.message_batch[0])
                + "\n```\n"
                + "\n".join(self.message_batch[1:])
                + "\n```"
            )
            self.message_batch.clear()

    def queue_message(self, message):
        self.message_batch.append(message)

    def send_message(self, message, tag="", mono=False, escape=True):
        message_tg = message
        if tag:
            message_tg = f"#{tag} {message_tg}"
        if mono:
            message_tg = "`" + message_tg + "`"
        if escape:
            message_tg = self.escape_markdown_v2(message_tg)
        self.request_message(message_tg)


class Simulator:
    """Core simulation runner. UI-agnostic; emits messages through a console writer hook."""

    def __init__(self, config):
        sim = config.get("simulator", {})
        self.default_exe = sim.get("default_exe", "")
        self.zip_path = sim.get("zip_path", "")

        defaults = config.get("defaults", {})
        self.default_omp_threads = defaults.get("omp_threads", 24)
        self.default_mpi_ranks = defaults.get("mpi_ranks", 4)

        self.tg = TelegramNotice(config.get("telegram", {}))

        # Frontend hook for info/warning/error lines. CLI keeps the default print;
        # TUI replaces it with a thread-safe log-widget writer.
        self.write_console: Callable[[str, str], None] = lambda msg, kind="info": print(msg)

    def info(self, msg: str, tag: str = ""):
        self.tg.send_message(msg, tag=tag)
        self.write_console(f"[{tag}] {msg}" if tag else msg, "info")

    @staticmethod
    def case_name_from_path(file_path: str) -> str:
        return Path(file_path).stem

    def prepare_exe(self, exe_path: str) -> str:
        dir_name, file_name = os.path.split(exe_path)
        base, ext = os.path.splitext(file_name)
        new_path = os.path.join(dir_name, f"{base}.batch{ext}")
        count = 1
        while os.path.isfile(new_path):
            new_path = os.path.join(dir_name, f"{base}.batch.{count}{ext}")
            count += 1
        shutil.copy2(exe_path, new_path)
        return new_path

    def cleanup_exe(self, exe_path: str) -> bool:
        if os.path.exists(exe_path):
            os.remove(exe_path)
            return True
        return False

    def set_omp_env(self, threads: Optional[int]):
        if threads is None:
            os.environ.pop("OMP_NUM_THREADS", None)
        else:
            os.environ["OMP_NUM_THREADS"] = str(threads)

    def build_cmd(self, exe_path: str, file_path: str, mpi_ranks: int = 0):
        if mpi_ranks > 0:
            return ["mpiexec", "-n", str(mpi_ranks), exe_path, "--scene-file", file_path]
        return [exe_path, "--scene-file", file_path]

    def run_case(
        self,
        exe_path: str,
        file_path: str,
        index: int,
        total: int,
        mpi_ranks: int = 0,
        on_line: Optional[Callable[[str, str], None]] = None,
        process_holder: Optional[List] = None,
    ) -> CaseResult:
        """Run a single simulation case.

        on_line(line, kind): called per stdout line with the full original line.
            kind is the most-specific match: "step" (SPH "[step]" or CAMMP
            "Processing job"), "error" ("[ERROR]"), "warning" ("[WARNING]"),
            or "raw" otherwise. Fires exactly once per line.
        process_holder: if a list is passed, the Popen handle is appended so
            the caller can terminate it.
        """
        case_name = self.case_name_from_path(file_path)
        start_time = time.time()
        warnings = 0
        errors = 0
        output_folder: Optional[str] = None

        self.tg.queue_message(f"#Case Summary '{case_name}' ({index+1}/{total}):")
        self.info(
            f"Processing '{case_name}' with '{os.path.basename(exe_path)}' ({index+1}/{total})",
            tag="Case",
        )

        cmd = self.build_cmd(exe_path, file_path, mpi_ranks)
        process = subprocess.Popen(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, universal_newlines=True,
        )
        if process_holder is not None:
            process_holder.append(process)

        for line in process.stdout:
            if output_folder is None and "Output directory:" in line:
                candidate = extract_output_dir(line)
                if candidate:
                    output_folder = candidate

            if "[step]" in line:
                # SPHSimulator step line:
                # [<ts>] Debug:   [LoggerPro][step] n: 100/..., t: ..., p: ..., dt_A: ..., ...
                line_terms = line.split("]")[-1].split(", ")
                line_processed = line_terms[1].strip() + ", " + line_terms[-1].strip()
                self.tg.send_message(
                    f"({index+1}/{total}) {line_processed}",
                    tag="Case",
                )
                if on_line:
                    on_line(line, "step")
            elif "Processing job " in line:
                # CAMMP job step line:
                # [<ts>] [INFO][Simulator] Processing job N on layer L track T
                start = line.find("Processing job ")
                line_processed = line[start:].rstrip()
                self.tg.send_message(
                    f"({index+1}/{total}) {line_processed}",
                    tag="Case",
                )
                if on_line:
                    on_line(line, "step")
            elif "[ERROR]" in line:
                error_text = line.split("[ERROR]")[-1].strip()
                self.tg.queue_message(f"Error: {error_text}")
                errors += 1
                if on_line:
                    on_line(line, "error")
            elif "[WARNING]" in line:
                warning_text = line.split("[WARNING]")[-1].strip()
                self.tg.queue_message(f"Warning: {warning_text}")
                warnings += 1
                if on_line:
                    on_line(line, "warning")
            else:
                if on_line:
                    on_line(line, "raw")

        process.wait()
        elapsed = round(time.time() - start_time)

        if process.returncode == 0:
            self.tg.queue_message(f"\nCase cost: {datetime.timedelta(seconds=elapsed)}")
            self.tg.send_telegram_message_batch()
            self.info(f"Successfully processed '{case_name}'", tag="Case")
        else:
            self.tg.send_telegram_message_batch()
            self.info(f"Error processing '{case_name}' (returncode {process.returncode})", tag="Case")

        return CaseResult(process.returncode, elapsed, warnings, errors, output_folder)

    def zip_case_output(self, case_name: str, output_folder: str) -> bool:
        zip_file = f"{output_folder}.zip"
        try:
            subprocess.run([self.zip_path, "a", zip_file, output_folder], check=True)
            self.info(f"Compressed output of case '{case_name}'", tag="Case")
            return True
        except subprocess.CalledProcessError as e:
            self.info(f"Error during compression output of case '{case_name}': {e}", tag="Case")
            return False

    def remove_case_output(self, case_name: str, output_folder: str):
        try:
            shutil.rmtree(output_folder)
            self.info(f"Removed output of case '{case_name}'", tag="Case")
        except Exception as e:
            self.info(f"Error while deleting output of case '{case_name}': {e}", tag="Case")

    def send_batch_report(self, case_names, time_costs, total_failures, total_errors, total_warnings):
        self.tg.queue_message("#Batch Process summary:")
        self.tg.queue_message(
            f"Total: {len(case_names)}\nFailure: {total_failures}\n"
            f"Error: {total_errors}\nWarning: {total_warnings}"
        )
        self.tg.queue_message("\nCase costs:")
        for i, case_name in enumerate(case_names):
            self.tg.queue_message(f"{i+1}. {case_name}: {datetime.timedelta(seconds=time_costs[i])}")
        self.tg.send_telegram_message_batch()

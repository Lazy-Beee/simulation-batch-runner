"""Core batch simulation logic shared by the CLI and TUI frontends."""

import os
import re
import sys
import json
import time
import shutil
import datetime
import subprocess
from pathlib import Path
from typing import NamedTuple, Callable, Optional, List

import requests


def _app_root() -> Path:
    """Folder to load config.json from. Frozen builds look next to the exe; source runs look next to this file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


CONFIG_PATH = _app_root() / "config.json"


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
    """Pull a path out of an 'Output directory:' log line.

    Accepts either a bare path (taken verbatim) or a double-quoted path with
    JSON-style \\\\ -> \\ escaping.
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


def match_profile(exe_path: str, profiles: List[dict]) -> Optional[dict]:
    """First profile whose path_marker (case-insensitive substring) occurs in exe_path."""
    p = exe_path.lower()
    for prof in profiles:
        marker = (prof.get("path_marker") or "").lower()
        if marker and marker in p:
            return prof
    return None


def profile_name(profile: Optional[dict]) -> str:
    return profile["name"] if profile else "unknown"


def profile_supports_mpi(profile: Optional[dict]) -> bool:
    # Unknown simulators are allowed to use MPI - we don't know enough to forbid it.
    return True if profile is None else bool(profile.get("supports_mpi", True))


def profile_step_pattern(profile: Optional[dict]) -> Optional[str]:
    """Regex matched against each stdout line. Capture group 1, if present, is the display text."""
    if profile is None:
        return None
    pattern = profile.get("step_pattern")
    return pattern or None


def profile_eta_pattern(profile: Optional[dict]) -> Optional[str]:
    """Regex pulling an ETA token (capture group 1) from step lines."""
    if profile is None:
        return None
    pattern = profile.get("eta_pattern")
    return pattern or None


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
    """UI-agnostic batch runner. Frontends swap write_console for their own log sink."""

    def __init__(self, config):
        sim = config.get("simulator", {})
        self.default_exe = sim.get("default_exe", "")
        self.zip_path = sim.get("zip_path", "")
        self.zip_ext = sim.get("zip_ext", ".zip")
        self.zip_args: List[str] = list(sim.get("zip_args", []))
        # When True (default), the TUI runs zip + remove on a background
        # thread so the next case can start immediately. False forces the
        # batch worker to wait for each archive before moving on.
        self.zip_async = bool(sim.get("zip_async", True))

        defaults = config.get("defaults", {})
        self.default_omp_threads = defaults.get("omp_threads", 24)
        self.default_mpi_ranks = defaults.get("mpi_ranks", 4)

        self.profiles: List[dict] = list(config.get("simulator_profiles", []))

        self.tg = TelegramNotice(config.get("telegram", {}))

        self.write_console: Callable[[str, str], None] = lambda msg, kind="info": print(msg)

    def info(self, msg: str, tag: str = ""):
        self.tg.send_message(msg, tag=tag)
        self.write_console(f"[{tag}] {msg}" if tag else msg, "info")

    def identify_profile(self, exe_path: str) -> Optional[dict]:
        return match_profile(exe_path, self.profiles)

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
        total,
        mpi_ranks: int = 0,
        on_line: Optional[Callable[[str, str], None]] = None,
        process_holder: Optional[List] = None,
    ) -> CaseResult:
        """Run a single case.

        total: int or zero-arg callable. Re-evaluated for every '(i+1/total)'
            so a frontend that grows the queue mid-case (the TUI's mid-batch
            Add) sees a live denominator.
        on_line(line, kind): kind is the most-specific match - 'step', 'error',
            'warning', or 'raw'. Fires exactly once per stdout line.
        process_holder: if given, the Popen is appended so the caller can kill it.
        """
        case_name = self.case_name_from_path(file_path)
        start_time = time.time()
        warnings = 0
        errors = 0
        output_folder: Optional[str] = None
        step_pattern_str = profile_step_pattern(self.identify_profile(exe_path))
        step_re = re.compile(step_pattern_str) if step_pattern_str else None

        def _t():
            return total() if callable(total) else total

        self.tg.queue_message(f"#Case Summary '{case_name}' ({index+1}/{_t()}):")
        self.info(
            f"Processing '{case_name}' with '{os.path.basename(exe_path)}' ({index+1}/{_t()})",
            tag="Case",
        )

        cmd = self.build_cmd(exe_path, file_path, mpi_ranks)
        # Merge stderr into stdout so one reader drains both - separate
        # unread PIPEs deadlock once the Windows pipe buffer fills.
        process = subprocess.Popen(
            cmd, text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
        )
        if process_holder is not None:
            process_holder.append(process)

        for line in process.stdout:
            if output_folder is None and "Output directory:" in line:
                candidate = extract_output_dir(line)
                if candidate:
                    output_folder = candidate

            step_match = step_re.search(line) if step_re else None
            if step_match:
                if step_match.lastindex:
                    line_processed = step_match.group(1).rstrip()
                else:
                    line_processed = line[step_match.start():].rstrip()
                self.tg.send_message(
                    f"({index+1}/{_t()}) {line_processed}",
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

    def zip_case_output(
        self,
        case_name: str,
        output_folder: str,
        on_line: Optional[Callable[[str, str], None]] = None,
    ) -> bool:
        """Archive the case output via 7-Zip.

        Output filename gets zip_ext (default '.zip'); 7z auto-picks the
        format from that extension. zip_args (e.g. ['-mx=3', '-mmt=8']) are
        passed straight through between 'a' and the archive name.

        on_line: if given, 7z output streams through this instead of
            inheriting stdio. The TUI must pass one or 7z's ANSI/CR
            sequences corrupt the Textual render.
        """
        zip_file = f"{output_folder}{self.zip_ext}"
        cmd = [self.zip_path, "a", *self.zip_args, zip_file, output_folder]
        try:
            if on_line is None:
                subprocess.run(cmd, check=True)
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                for line in proc.stdout:
                    on_line(line, "raw")
                proc.wait()
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, cmd)
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

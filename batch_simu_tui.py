"""Textual TUI frontend for batch simulation - 3-tab layout."""

import os
import time as _time
import shlex
import datetime

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.widgets import (
    Header, Footer, Input, Label, Button, Switch,
    Static, RichLog, ListView, ListItem, ProgressBar,
    TabbedContent, TabPane, DataTable,
)

from simulation import Simulator, load_config, detect_simulator_type, simulator_supports_mpi


def format_sim_type_text(exe_path: str) -> str:
    sim_type = detect_simulator_type(exe_path)
    if sim_type == "sph":
        return "Type: SPH (single-process only - MPI not supported)"
    if sim_type == "cammp":
        return "Type: CAMMP"
    return "Type: unknown"


class BatchSimuApp(App):
    CSS = """
    Screen { layout: vertical; }
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; }

    #setup_scroll { width: 100%; height: 1fr; }

    .row { width: 100%; height: 3; align: left middle; }
    Input { width: 1fr; }
    .narrow { width: 10; }
    Button { margin-right: 1; }
    Label { margin: 0 1; }
    Static { width: 100%; }

    #scene_list { height: 8; border: tall $panel; }
    #log_panel { height: 1fr; border: solid $accent; }
    #done_table { height: 1fr; }

    #current_case_label, #current_step_label, #current_stats_label {
        padding: 0 1;
    }
    #current_case_label { color: $accent; text-style: bold; }

    #summary_label { padding: 1; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+s", "start", "Start", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "show_tab('setup')", "Setup"),
        Binding("f2", "show_tab('running')", "Running"),
        Binding("f3", "show_tab('done')", "Done"),
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.simulator = Simulator(self.config)
        self.scene_files: list[str] = []
        self.batch_exe: str | None = None
        self.process_holder: list = []
        self.stop_requested = False
        self.batch_running = False
        self.current_case_start: float | None = None
        self.current_warnings = 0
        self.current_errors = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="setup"):
            with TabPane("Setup", id="setup"):
                with VerticalScroll(id="setup_scroll"):
                    with Horizontal(classes="row"):
                        yield Label("Simulator:")
                        yield Input(
                            value=self.simulator.default_exe,
                            id="exe_input",
                            placeholder="path to SPHSimulator.exe or CAMMP.exe",
                        )
                    yield Static(format_sim_type_text(self.simulator.default_exe), id="sim_type_label")
                    with Horizontal(classes="row"):
                        yield Switch(value=True, id="omp_switch")
                        yield Label(f"Limit OMP to {self.simulator.default_omp_threads}")
                        yield Switch(value=False, id="mpi_switch")
                        yield Label("MPI ranks:")
                        yield Input(
                            value=str(self.simulator.default_mpi_ranks),
                            id="mpi_input",
                            classes="narrow",
                            type="integer",
                        )
                    with Horizontal(classes="row"):
                        yield Label("Scene:")
                        yield Input(
                            id="add_file_input",
                            placeholder="path (Enter to add; quote paths with spaces; multiple allowed)",
                        )
                        yield Button("Add", id="add_btn", variant="primary")
                        yield Button("Remove", id="remove_btn")
                    yield ListView(id="scene_list")
                    with Horizontal(classes="row"):
                        yield Switch(value=True, id="zip_switch")
                        yield Label("Zip output")
                        yield Switch(value=True, id="remove_switch")
                        yield Label("Remove after zip")
                    with Horizontal(classes="row"):
                        yield Button("START", id="start_btn", variant="success")
                        yield Button("STOP", id="stop_btn", variant="error", disabled=True)
                    yield Static("Idle", id="status_label")
                    yield ProgressBar(id="progress", total=100, show_eta=False)
            with TabPane("Running", id="running"):
                with Vertical():
                    yield Static("No case running", id="current_case_label")
                    yield Static("Step: -", id="current_step_label")
                    yield Static("Elapsed: - | Warnings: 0 | Errors: 0", id="current_stats_label")
                    yield RichLog(
                        id="log_panel",
                        highlight=False,
                        markup=True,
                        wrap=False,
                        max_lines=10000,
                    )
            with TabPane("Done", id="done"):
                with Vertical():
                    yield Static("No cases completed yet", id="summary_label")
                    yield DataTable(id="done_table", zebra_stripes=True)
        yield Footer()

    # ---------- helpers ----------

    def log_line(self, line: str, kind: str = "raw"):
        widget = self.query_one("#log_panel", RichLog)
        line = line.rstrip("\n")
        if kind == "error":
            widget.write(f"[red]{line}[/red]")
            self.current_errors += 1
            self._refresh_current_stats()
        elif kind == "warning":
            widget.write(f"[yellow]{line}[/yellow]")
            self.current_warnings += 1
            self._refresh_current_stats()
        elif kind == "step":
            widget.write(f"[cyan]{line}[/cyan]")
            step_text = line[line.find("[step]"):] if "[step]" in line else line
            self.query_one("#current_step_label", Static).update(f"Step: {step_text}")
        elif kind == "info":
            widget.write(f"[blue]{line}[/blue]")
        else:
            widget.write(line)

    def _refresh_current_stats(self):
        if self.current_case_start is None:
            return
        elapsed = round(_time.time() - self.current_case_start)
        self.query_one("#current_stats_label", Static).update(
            f"Elapsed: {datetime.timedelta(seconds=elapsed)} | "
            f"Warnings: {self.current_warnings} | Errors: {self.current_errors}"
        )

    def set_status(self, text: str):
        self.query_one("#status_label", Static).update(text)

    def set_progress(self, percent: float):
        self.query_one("#progress", ProgressBar).update(progress=percent)

    def refresh_scene_list(self):
        lst = self.query_one("#scene_list", ListView)
        lst.clear()
        for p in self.scene_files:
            marker = "" if os.path.exists(p) else "  [MISSING]"
            lst.append(ListItem(Label(f"{p}{marker}")))

    def reset_run_controls(self):
        self.query_one("#start_btn", Button).disabled = False
        self.query_one("#stop_btn", Button).disabled = True

    def apply_sim_type(self, exe_path: str):
        self.query_one("#sim_type_label", Static).update(format_sim_type_text(exe_path))
        supports = simulator_supports_mpi(detect_simulator_type(exe_path))
        mpi_switch = self.query_one("#mpi_switch", Switch)
        mpi_input = self.query_one("#mpi_input", Input)
        if not supports:
            mpi_switch.value = False
        mpi_switch.disabled = not supports
        mpi_input.disabled = not supports

    def switch_tab(self, tab_id: str):
        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            pass

    def start_current_case(self, case_name: str, idx: int, total: int):
        self.current_case_start = _time.time()
        self.current_warnings = 0
        self.current_errors = 0
        self.query_one("#current_case_label", Static).update(f"Case: {case_name} ({idx+1}/{total})")
        self.query_one("#current_step_label", Static).update("Step: -")
        self._refresh_current_stats()

    def finish_current_case(self):
        self.current_case_start = None

    def add_done_row(self, idx: int, case_name: str, returncode: int, elapsed_secs: int, warnings: int, errors: int):
        table = self.query_one("#done_table", DataTable)
        if returncode == -2:
            status = "MISSING"
        elif returncode == -3:
            status = "EXCEPTION"
        elif returncode == 0:
            status = "OK"
        else:
            status = f"FAIL({returncode})"
        elapsed_str = "-" if elapsed_secs < 0 else str(datetime.timedelta(seconds=elapsed_secs))
        table.add_row(str(idx + 1), case_name, status, elapsed_str, str(warnings), str(errors))

    def update_summary(self, total: int, done: int, failures: int, errors: int, warnings: int):
        self.query_one("#summary_label", Static).update(
            f"Total: {total} | Done: {done} | Failed: {failures} | "
            f"Errors: {errors} | Warnings: {warnings}"
        )

    # ---------- mount ----------

    def on_mount(self):
        table = self.query_one("#done_table", DataTable)
        table.add_columns("#", "Case", "Status", "Time", "Warnings", "Errors")
        self.apply_sim_type(self.query_one("#exe_input", Input).value)
        self.set_interval(1.0, self._refresh_current_stats)

    # ---------- event handlers ----------

    @on(Input.Changed, "#exe_input")
    def on_exe_changed(self, event: Input.Changed):
        self.apply_sim_type(event.value)

    @on(Button.Pressed, "#add_btn")
    def on_add(self):
        self._add_from_input()

    @on(Input.Submitted, "#add_file_input")
    def on_add_submit(self):
        self._add_from_input()

    def _add_from_input(self):
        inp = self.query_one("#add_file_input", Input)
        text = inp.value.strip()
        if not text:
            return
        try:
            paths = shlex.split(text)
        except ValueError as e:
            self.log_line(f"Error parsing input: {e}", "error")
            return
        for p in paths:
            self.scene_files.append(p)
            if not os.path.exists(p):
                self.log_line(f"Warning: file not found: {p}", "warning")
        self.refresh_scene_list()
        inp.value = ""
        inp.focus()

    @on(Button.Pressed, "#remove_btn")
    def on_remove(self):
        lst = self.query_one("#scene_list", ListView)
        idx = lst.index
        if idx is not None and 0 <= idx < len(self.scene_files):
            removed = self.scene_files.pop(idx)
            self.refresh_scene_list()
            self.log_line(f"Removed: {removed}", "info")

    def action_clear_log(self):
        self.query_one("#log_panel", RichLog).clear()

    def action_show_tab(self, tab_id: str):
        self.switch_tab(tab_id)

    @on(Button.Pressed, "#start_btn")
    def on_start(self):
        if self.batch_running:
            return
        self._launch_batch()

    def action_start(self):
        self.on_start()

    @on(Button.Pressed, "#stop_btn")
    def on_stop(self):
        if not self.batch_running:
            return
        self.stop_requested = True
        for proc in self.process_holder:
            try:
                proc.terminate()
            except Exception:
                pass
        self.log_line("--- Stop requested ---", "warning")

    def action_stop(self):
        self.on_stop()

    # ---------- batch orchestration ----------

    def _launch_batch(self):
        exe_input = self.query_one("#exe_input", Input).value.strip().replace('"', "")
        exe_path = exe_input if exe_input and os.path.isfile(exe_input) else self.simulator.default_exe
        if not os.path.isfile(exe_path):
            self.log_line(f"Simulator exe not found: {exe_path}", "error")
            return
        if not self.scene_files:
            self.log_line("No scene files added.", "error")
            return

        use_omp = self.query_one("#omp_switch", Switch).value
        use_mpi = self.query_one("#mpi_switch", Switch).value
        mpi_input = self.query_one("#mpi_input", Input).value.strip()
        if use_mpi:
            try:
                mpi_ranks = int(mpi_input) if mpi_input else self.simulator.default_mpi_ranks
                if mpi_ranks < 1:
                    mpi_ranks = self.simulator.default_mpi_ranks
            except ValueError:
                mpi_ranks = self.simulator.default_mpi_ranks
        else:
            mpi_ranks = 0

        sim_type = detect_simulator_type(exe_path)
        if not simulator_supports_mpi(sim_type) and mpi_ranks > 0:
            self.log_line(f"{sim_type.upper()} does not support MPI - forcing single-process.", "warning")
            mpi_ranks = 0

        zip_output = self.query_one("#zip_switch", Switch).value
        remove_output = self.query_one("#remove_switch", Switch).value

        self.simulator.set_omp_env(self.simulator.default_omp_threads if use_omp else None)
        self.simulator.write_console = lambda msg, kind="info": self.call_from_thread(self.log_line, msg, kind)

        try:
            self.batch_exe = self.simulator.prepare_exe(exe_path)
        except Exception as e:
            self.log_line(f"Failed to prepare exe: {e}", "error")
            return
        self.log_line(f"Prepared batch exe: {self.batch_exe}", "info")

        # Reset Done tab for the new batch
        self.query_one("#done_table", DataTable).clear()
        self.update_summary(len(self.scene_files), 0, 0, 0, 0)

        self.batch_running = True
        self.stop_requested = False
        self.process_holder = []
        self.query_one("#start_btn", Button).disabled = True
        self.query_one("#stop_btn", Button).disabled = False
        self.set_progress(0)
        self.switch_tab("running")

        self._run_batch_worker(list(self.scene_files), mpi_ranks, zip_output, remove_output)

    @work(thread=True, exclusive=True)
    def _run_batch_worker(self, scene_files, mpi_ranks, zip_output, remove_output):
        sim = self.simulator
        total = len(scene_files)
        time_costs: list[int] = []
        case_names: list[str] = []
        total_warnings = 0
        total_errors = 0
        total_failures = 0

        try:
            sim.info("Start processing", tag="Batch")
            sim.tg.queue_message("#Batch Batch settings:")
            sim.tg.queue_message(f"Zip output: {'True' if zip_output else 'False'}")
            sim.tg.queue_message(f"Remove output: {'True' if remove_output else 'False'}")
            sim.tg.queue_message(f"MPI: {f'{mpi_ranks} ranks' if mpi_ranks > 0 else 'disabled'}")
            sim.tg.queue_message(f"OMP threads: {os.environ.get('OMP_NUM_THREADS', 'system default')}")
            sim.tg.send_telegram_message_batch()

            for i, file_path in enumerate(scene_files):
                if self.stop_requested:
                    self.call_from_thread(self.log_line, "--- Batch stopped by user ---", "warning")
                    break

                case_name = sim.case_name_from_path(file_path)
                case_names.append(case_name)
                self.call_from_thread(self.set_status, f"Case {i+1}/{total}: {case_name}")
                self.call_from_thread(self.set_progress, (i / total) * 100)
                self.call_from_thread(self.start_current_case, case_name, i, total)

                if not os.path.exists(file_path):
                    time_costs.append(-1)
                    total_failures += 1
                    sim.info(f"File '{file_path}' not found.", tag="Case")
                    self.call_from_thread(self.add_done_row, i, case_name, -2, -1, 0, 0)
                    self.call_from_thread(self.update_summary, total, i + 1, total_failures, total_errors, total_warnings)
                    continue

                self.process_holder.clear()

                def on_line(line, kind):
                    self.call_from_thread(self.log_line, line, kind)

                try:
                    result = sim.run_case(
                        self.batch_exe, file_path, i, total, mpi_ranks,
                        on_line=on_line, process_holder=self.process_holder,
                    )
                except Exception as e:
                    self.call_from_thread(self.log_line, f"Exception in case: {e}", "error")
                    time_costs.append(-1)
                    total_failures += 1
                    self.call_from_thread(self.add_done_row, i, case_name, -3, -1, 0, 0)
                    self.call_from_thread(self.update_summary, total, i + 1, total_failures, total_errors, total_warnings)
                    continue

                total_warnings += result.warnings
                total_errors += result.errors

                if result.returncode == 0:
                    time_costs.append(result.elapsed)
                    if zip_output:
                        if not result.output_folder:
                            sim.info(
                                f"No output directory detected in log for '{case_name}'; skipping zip/remove.",
                                tag="Case",
                            )
                        else:
                            zipped = sim.zip_case_output(case_name, result.output_folder)
                            if remove_output:
                                if zipped:
                                    sim.remove_case_output(case_name, result.output_folder)
                                else:
                                    sim.info(f"Output removal cancelled for case '{case_name}'", tag="Case")
                else:
                    time_costs.append(-1)
                    total_failures += 1

                self.call_from_thread(self.finish_current_case)
                self.call_from_thread(
                    self.add_done_row, i, case_name, result.returncode,
                    result.elapsed if result.returncode == 0 else -1,
                    result.warnings, result.errors,
                )
                self.call_from_thread(self.update_summary, total, i + 1, total_failures, total_errors, total_warnings)
                self.call_from_thread(
                    self.set_status,
                    f"Done {i+1}/{total} | "
                    f"Warnings: {total_warnings} | Errors: {total_errors} | Failures: {total_failures}",
                )

            self.call_from_thread(self.set_progress, 100)
            sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)
            sim.info("All done", tag="Batch")

        finally:
            if self.batch_exe and sim.cleanup_exe(self.batch_exe):
                self.call_from_thread(self.log_line, f"Cleaned up: {self.batch_exe}", "info")
            self.batch_exe = None
            self.batch_running = False
            self.call_from_thread(self.reset_run_controls)
            self.call_from_thread(self.finish_current_case)
            self.call_from_thread(self.switch_tab, "done")
            self.call_from_thread(
                self.set_status,
                f"Idle | Total: {total} | "
                f"Warnings: {total_warnings} | Errors: {total_errors} | Failures: {total_failures}",
            )

    def on_unmount(self):
        if self.batch_exe:
            try:
                self.simulator.cleanup_exe(self.batch_exe)
            except Exception:
                pass


def main():
    BatchSimuApp().run()


if __name__ == "__main__":
    main()

"""Textual TUI frontend for batch simulation."""

import os
import shlex

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Header, Footer, Input, Label, Button, Switch,
    Static, RichLog, ListView, ListItem, ProgressBar, Select,
)

from simulation import Simulator, load_config, detect_simulator_type, simulator_supports_mpi

NONE_TASK = "__none__"


def format_sim_type_text(exe_path: str) -> str:
    sim_type = detect_simulator_type(exe_path)
    if sim_type == "sph":
        return "Type: SPH (single-process only - MPI not supported)"
    if sim_type == "cammp":
        return "Type: CAMMP"
    return "Type: unknown"


class BatchSimuApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #config_panel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }

    #log_panel {
        height: 1fr;
        min-height: 8;
        border: solid $accent;
    }

    #status_panel {
        height: 4;
        border: solid $accent;
        padding: 0 1;
    }

    .row {
        height: 3;
        align: left middle;
    }

    Input {
        width: 1fr;
    }

    .narrow {
        width: 10;
    }

    #scene_list {
        height: 6;
        border: tall $panel;
    }

    Button {
        margin-right: 1;
    }

    Label {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "start", "Start", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="config_panel"):
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
                yield Label("Post-task:")
                task_options = [("None", NONE_TASK)] + [(k, k) for k in self.simulator.sequential_tasks]
                yield Select(
                    options=task_options,
                    value=NONE_TASK,
                    id="task_select",
                    allow_blank=False,
                )
            with Horizontal(classes="row"):
                yield Button("START", id="start_btn", variant="success")
                yield Button("STOP", id="stop_btn", variant="error", disabled=True)
                yield Button("Clear log", id="clear_log_btn")

        yield RichLog(id="log_panel", highlight=False, markup=True, wrap=False, max_lines=10000)

        with Vertical(id="status_panel"):
            yield Static("Idle", id="status_label")
            yield ProgressBar(id="progress", total=100, show_eta=False)

        yield Footer()

    # ---------- helpers ----------

    def log_line(self, line: str, kind: str = "raw"):
        widget = self.query_one("#log_panel", RichLog)
        line = line.rstrip("\n")
        if kind == "error":
            widget.write(f"[red]{line}[/red]")
        elif kind == "warning":
            widget.write(f"[yellow]{line}[/yellow]")
        elif kind == "info":
            widget.write(f"[cyan]{line}[/cyan]")
        else:
            widget.write(line)

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
        """Update the type label and gate MPI controls based on the simulator family."""
        self.query_one("#sim_type_label", Static).update(format_sim_type_text(exe_path))
        supports = simulator_supports_mpi(detect_simulator_type(exe_path))
        mpi_switch = self.query_one("#mpi_switch", Switch)
        mpi_input = self.query_one("#mpi_input", Input)
        if not supports:
            mpi_switch.value = False
        mpi_switch.disabled = not supports
        mpi_input.disabled = not supports

    def on_mount(self):
        self.apply_sim_type(self.query_one("#exe_input", Input).value)

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

    @on(Button.Pressed, "#clear_log_btn")
    def on_clear_log(self):
        self.query_one("#log_panel", RichLog).clear()

    def action_clear_log(self):
        self.on_clear_log()

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
        post_raw = self.query_one("#task_select", Select).value
        post_task = "" if post_raw == NONE_TASK else str(post_raw)

        self.simulator.set_omp_env(self.simulator.default_omp_threads if use_omp else None)
        # Route simulator's info/warning/error messages into the log widget.
        self.simulator.write_console = lambda msg, kind="info": self.call_from_thread(self.log_line, msg, kind)

        try:
            self.batch_exe = self.simulator.prepare_exe(exe_path)
        except Exception as e:
            self.log_line(f"Failed to prepare exe: {e}", "error")
            return
        self.log_line(f"Prepared batch exe: {self.batch_exe}", "info")

        self.batch_running = True
        self.stop_requested = False
        self.process_holder = []
        self.query_one("#start_btn", Button).disabled = True
        self.query_one("#stop_btn", Button).disabled = False
        self.set_progress(0)

        self._run_batch_worker(
            list(self.scene_files), mpi_ranks, zip_output, remove_output, post_task
        )

    @work(thread=True, exclusive=True)
    def _run_batch_worker(self, scene_files, mpi_ranks, zip_output, remove_output, post_task):
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
            sim.tg.queue_message(f"Sequential task: {post_task if post_task else 'None'}")
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

                if not os.path.exists(file_path):
                    time_costs.append(-1)
                    total_failures += 1
                    sim.info(f"File '{file_path}' not found.", tag="Case")
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
                    continue

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

                self.call_from_thread(
                    self.set_status,
                    f"Done {i+1}/{total} | W: {total_warnings} E: {total_errors} F: {total_failures}",
                )

            self.call_from_thread(self.set_progress, 100)
            sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)

            if not self.stop_requested and post_task:
                err = sim.start_post_task(post_task)
                if err:
                    sim.info(err, tag="Batch")
                else:
                    sim.info(f"Sequential task '{post_task}' started.", tag="Batch")

            sim.info("All done", tag="Batch")

        finally:
            if self.batch_exe and sim.cleanup_exe(self.batch_exe):
                self.call_from_thread(self.log_line, f"Cleaned up: {self.batch_exe}", "info")
            self.batch_exe = None
            self.batch_running = False
            self.call_from_thread(self.reset_run_controls)
            self.call_from_thread(
                self.set_status,
                f"Idle | Total: {total} | W: {total_warnings} E: {total_errors} F: {total_failures}",
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

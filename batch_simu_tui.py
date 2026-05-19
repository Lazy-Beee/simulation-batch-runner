"""Textual TUI frontend for batch simulation - 2-tab layout (Setup + Running)."""

import os
import sys
import subprocess
import time as _time
import shlex
import datetime
from dataclasses import dataclass, field
from typing import Optional

from rich.text import Text

from textual import on, work, events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Header, Footer, Input, Label, Button, Switch,
    Static, RichLog, ProgressBar,
    TabbedContent, TabPane, DataTable,
)

from simulation import Simulator, load_config, profile_name, profile_supports_mpi, profile_step_marker


# Lifecycle states a queued entry passes through.
# Visual styling and reorder-locking both key off of this.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"   # scene file didn't exist when we tried to run it
STATUS_ERROR = "error"       # exception during run
STATUS_STOPPED = "stopped"   # process was force-stopped mid-run

# Statuses that are removable from the queue (anything not currently in flight).
REMOVABLE_STATUSES = {STATUS_PENDING, STATUS_STOPPED}

ROW_STYLE_BY_STATUS = {
    STATUS_PENDING: "",
    STATUS_RUNNING: "on yellow",
    STATUS_DONE: "on dark_green",
    STATUS_FAILED: "on red3",
    STATUS_MISSING: "on red3",
    STATUS_ERROR: "on red3",
    STATUS_STOPPED: "on grey35",
}

# Single Setup queue table now also carries result columns (formerly the Done tab).
QUEUE_COLS = [
    ("#", 4),
    ("Exe", 22),
    ("Scene", 26),
    ("OMP", 5),
    ("MPI", 5),
    ("Zip", 5),
    ("Rmv", 5),
    ("Status", 10),
    ("Time", 10),
    ("Warnings", 9),
    ("Errors", 7),
]


def status_display(entry) -> str:
    s = entry.status
    if s == STATUS_DONE:
        return "OK"
    if s == STATUS_FAILED:
        return f"FAIL({entry.returncode})" if entry.returncode is not None else "FAIL"
    if s == STATUS_MISSING:
        return "MISSING"
    if s == STATUS_ERROR:
        return "EXCEPTION"
    if s == STATUS_STOPPED:
        return "STOPPED"
    return s  # pending / running


def styled_cell(value, width: int, style: str) -> Text:
    """Build a left-justified Text padded to `width` with `style` applied to
    the whole span (including the trailing spaces), so the cell background
    colour fills the entire cell rather than just the printable characters."""
    return Text(str(value).ljust(width), style=style)


def kill_proc_tree(proc):
    """Best-effort terminate a process and all of its children.

    proc.terminate() on Windows only kills the immediate process, which is
    wrong for .bat / wrapper exes whose actual work runs in a child
    process. Windows' taskkill /F /T walks the process tree.
    """
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
            return
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        pass


@dataclass
class SceneEntry:
    """Snapshot of a queued case: exe + scene + per-case OMP/MPI/zip/remove settings,
    plus mutable run-state fields updated by the worker."""
    exe_path: str
    scene_path: str
    omp_threads: Optional[int]   # None = no OMP limit
    mpi_ranks: int               # 0 = MPI disabled
    zip_output: bool
    remove_output: bool
    # Run state (mutable; updated as the batch progresses)
    status: str = STATUS_PENDING
    returncode: Optional[int] = None
    elapsed: Optional[int] = None
    warnings: int = 0
    errors: int = 0


def format_sim_type_text(simulator: Simulator, exe_path: str) -> str:
    profile = simulator.identify_profile(exe_path)
    if profile is None:
        return "Type: unknown"
    name = profile_name(profile)
    if not profile_supports_mpi(profile):
        return f"Type: {name} (single-process only - MPI not supported)"
    return f"Type: {name}"


def strip_quotes(s: str) -> str:
    """Drag-and-drop on Windows Terminal often inserts quoted paths; strip outer quotes."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


class BatchSimuApp(App):
    CSS = """
    Screen { layout: vertical; }
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; }

    /* Setup tab: flat layout - top widgets + scene_queue (1fr) + bottom widgets.
       scene_queue is the only fr-sized child, so it absorbs all remaining
       vertical space inside setup_panel. */
    #setup_panel { height: 1fr; }
    #scene_queue { height: 1fr; border: tall $panel; }

    .row { width: 100%; height: 3; align: left middle; }
    Input { width: 1fr; }
    .narrow { width: 8; }
    Button { margin-right: 1; }
    Label { margin: 0 1; }

    /* Vertically center short widgets so they line up with Input/Button */
    .row > Label, .row > Switch {
        height: 3;
        content-align: left middle;
    }

    /* Push Reset to the right edge of its row */
    #bottom_filler { width: 1fr; }

    #sim_type_label, #status_label,
    #current_case_label, #current_step_label, #current_stats_label,
    #summary_label {
        width: 100%;
    }

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
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.simulator = Simulator(self.config)
        self.scene_entries: list[SceneEntry] = []
        # exe_path -> prepared batch_exe copy. Reused across cases that share an exe.
        self._prepared_exes: dict[str, str] = {}
        self.process_holder: list = []
        self.stop_requested = False
        self.force_stopped_current = False   # set by FORCE STOP; consumed by worker
        self.current_entry: Optional[SceneEntry] = None
        self.batch_running = False
        self.current_case_start: float | None = None
        self.current_warnings = 0
        self.current_errors = 0
        self.current_step_marker: str | None = None
        # Tracks the most recently applied profile so we only re-snap the
        # OMP/MPI switches when the matched profile actually transitions.
        self._last_profile_name: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="setup"):
            with TabPane("Setup", id="setup"):
                with Vertical(id="setup_panel"):
                    with Horizontal(classes="row"):
                        yield Label("Simulator:")
                        yield Input(
                            value=self.simulator.default_exe,
                            id="exe_input",
                            placeholder="path to simulator exe (drag a file in or paste)",
                        )
                    yield Static(format_sim_type_text(self.simulator, self.simulator.default_exe), id="sim_type_label")

                    with Horizontal(classes="row"):
                        yield Label("Scene:")
                        yield Input(
                            id="add_file_input",
                            placeholder="drag scene file(s) in or paste; Enter adds them with the settings below",
                        )

                    with Horizontal(classes="row"):
                        yield Switch(value=False, id="omp_switch")
                        yield Label("OMP")
                        yield Input(
                            value=str(self.simulator.default_omp_threads),
                            id="omp_input", classes="narrow", type="integer",
                        )
                        yield Switch(value=False, id="mpi_switch")
                        yield Label("MPI")
                        yield Input(
                            value=str(self.simulator.default_mpi_ranks),
                            id="mpi_input", classes="narrow", type="integer",
                        )
                        yield Switch(value=True, id="zip_switch")
                        yield Label("Zip")
                        yield Switch(value=True, id="remove_switch")
                        yield Label("Remove")
                        yield Button("Add", id="add_btn", variant="primary")

                    with Horizontal(classes="row"):
                        yield Button("Up", id="up_btn")
                        yield Button("Down", id="down_btn")
                        yield Button("Remove selected", id="remove_btn")

                    yield DataTable(id="scene_queue", zebra_stripes=True, cell_padding=0)

                    with Horizontal(classes="row"):
                        yield Button("START", id="start_btn", variant="success")
                        yield Button("STOP", id="stop_btn", variant="error", disabled=True)
                        yield Button("FORCE STOP", id="force_stop_btn", variant="error", disabled=True)
                        yield Button("RESUME", id="resume_btn", variant="primary")
                        yield Static("", id="bottom_filler")
                        yield Button("Reset", id="reset_btn", variant="warning")
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
            marker = self.current_step_marker
            if marker and marker in line:
                step_text = line[line.find(marker):].rstrip()
            else:
                step_text = line.rstrip()
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

    @staticmethod
    def _short(path: str) -> str:
        return os.path.basename(path) if path else "(empty)"

    @staticmethod
    def _fmt_omp(threads: Optional[int]) -> str:
        return str(threads) if threads is not None else "-"

    @staticmethod
    def _fmt_mpi(ranks: int) -> str:
        return str(ranks) if ranks > 0 else "-"

    @staticmethod
    def _fmt_bool(flag: bool) -> str:
        return "Y" if flag else "-"

    @staticmethod
    def _fmt_time(elapsed: Optional[int]) -> str:
        if elapsed is None or elapsed < 0:
            return "-"
        return str(datetime.timedelta(seconds=elapsed))

    def refresh_scene_queue(self):
        table = self.query_one("#scene_queue", DataTable)
        prev = table.cursor_row if table.row_count else None
        table.clear()
        widths = [w for _, w in QUEUE_COLS]
        for i, e in enumerate(self.scene_entries):
            scene_disp = self._short(e.scene_path)
            if not os.path.exists(e.scene_path):
                scene_disp += " [!]"
            sty = ROW_STYLE_BY_STATUS.get(e.status, "")
            values = [
                str(i + 1),
                self._short(e.exe_path),
                scene_disp,
                self._fmt_omp(e.omp_threads),
                self._fmt_mpi(e.mpi_ranks),
                self._fmt_bool(e.zip_output),
                self._fmt_bool(e.remove_output),
                status_display(e),
                self._fmt_time(e.elapsed),
                str(e.warnings),
                str(e.errors),
            ]
            table.add_row(*(styled_cell(v, w, sty) for v, w in zip(values, widths)))
        if prev is not None and 0 <= prev < table.row_count:
            table.move_cursor(row=prev)

    def reset_run_controls(self):
        self.query_one("#start_btn", Button).disabled = False
        self.query_one("#stop_btn", Button).disabled = True
        self.query_one("#force_stop_btn", Button).disabled = True
        self.query_one("#resume_btn", Button).disabled = False
        self.query_one("#reset_btn", Button).disabled = False

    def apply_sim_type(self, exe_path: str):
        profile = self.simulator.identify_profile(exe_path)
        self.query_one("#sim_type_label", Static).update(format_sim_type_text(self.simulator, exe_path))
        supports = profile_supports_mpi(profile)
        mpi_switch = self.query_one("#mpi_switch", Switch)
        mpi_input = self.query_one("#mpi_input", Input)
        omp_switch = self.query_one("#omp_switch", Switch)

        # Only re-apply OMP/MPI switch defaults when the matched profile
        # transitions (avoids clobbering manual toggles while the user types).
        current_id = profile_name(profile)
        if current_id != self._last_profile_name:
            self._last_profile_name = current_id
            omp_switch.value = bool(profile.get("default_omp", False)) if profile else False
            if supports:
                mpi_switch.value = bool(profile.get("default_mpi", False)) if profile else False
            else:
                mpi_switch.value = False

        if not supports:
            mpi_switch.value = False
        mpi_switch.disabled = not supports
        mpi_input.disabled = not supports

    def switch_tab(self, tab_id: str):
        try:
            self.query_one(TabbedContent).active = tab_id
            # Force a refresh - in some terminals the tab bar repaint can
            # otherwise be missed if the surrounding event loop is busy.
            self.refresh()
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

    # add_done_row / update_summary were removed when the Done tab was merged
    # into the Setup queue. Per-case results now live as columns on each row
    # of the Setup scene_queue table and refresh as the worker mutates the
    # SceneEntry fields.

    # ---------- mount ----------

    def on_mount(self):
        queue = self.query_one("#scene_queue", DataTable)
        for label, width in QUEUE_COLS:
            queue.add_column(label, width=width)
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

    def _read_int(self, value: str, fallback: int) -> int:
        v = value.strip()
        if not v:
            return fallback
        try:
            n = int(v)
            return n if n >= 1 else fallback
        except ValueError:
            return fallback

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

        # Snapshot the current widget state for these new entries
        exe_path = strip_quotes(self.query_one("#exe_input", Input).value)
        use_omp = self.query_one("#omp_switch", Switch).value
        omp = (
            self._read_int(self.query_one("#omp_input", Input).value, self.simulator.default_omp_threads)
            if use_omp else None
        )
        use_mpi = self.query_one("#mpi_switch", Switch).value
        mpi = (
            self._read_int(self.query_one("#mpi_input", Input).value, self.simulator.default_mpi_ranks)
            if use_mpi else 0
        )
        # Profile may forbid MPI; force off in that case (defense in depth)
        if not profile_supports_mpi(self.simulator.identify_profile(exe_path)):
            mpi = 0
        zip_out = self.query_one("#zip_switch", Switch).value
        rm_out = self.query_one("#remove_switch", Switch).value

        for p in paths:
            p = strip_quotes(p)
            entry = SceneEntry(
                exe_path=exe_path,
                scene_path=p,
                omp_threads=omp,
                mpi_ranks=mpi,
                zip_output=zip_out,
                remove_output=rm_out,
            )
            self.scene_entries.append(entry)
            if not os.path.exists(p):
                self.log_line(f"Warning: scene file not found: {p}", "warning")

        self.refresh_scene_queue()
        inp.value = ""
        inp.focus()

    @on(Button.Pressed, "#remove_btn")
    def on_remove(self):
        table = self.query_one("#scene_queue", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self.scene_entries)):
            return
        target = self.scene_entries[idx]
        if target.status not in REMOVABLE_STATUSES:
            self.log_line(
                f"Cannot remove '{target.scene_path}': {status_display(target)} cases stay as a record.",
                "warning",
            )
            return
        self.scene_entries.pop(idx)
        self.refresh_scene_queue()
        self.log_line(f"Removed: {target.scene_path}", "info")

    @on(Button.Pressed, "#up_btn")
    def on_move_up(self):
        self._move_selected(-1)

    @on(Button.Pressed, "#down_btn")
    def on_move_down(self):
        self._move_selected(+1)

    def _move_selected(self, delta: int):
        """Move the selected queue row up (delta=-1) or down (+1).

        Only pending entries may move, and they may not jump past a non-pending
        neighbour (i.e. cannot land before the running/done barrier).
        """
        table = self.query_one("#scene_queue", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self.scene_entries)):
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.scene_entries)):
            return
        if self.scene_entries[idx].status != STATUS_PENDING:
            self.log_line("Cannot reorder a case that is running or finished.", "warning")
            return
        if self.scene_entries[new_idx].status != STATUS_PENDING:
            self.log_line("Cannot move a pending case before a running/finished case.", "warning")
            return
        self.scene_entries[idx], self.scene_entries[new_idx] = (
            self.scene_entries[new_idx],
            self.scene_entries[idx],
        )
        self.refresh_scene_queue()
        # Move cursor along with the row we just shifted
        if 0 <= new_idx < table.row_count:
            table.move_cursor(row=new_idx)

    @on(Button.Pressed, "#reset_btn")
    def on_reset(self):
        if self.batch_running:
            self.log_line("Cannot reset while a batch is running. Stop first.", "warning")
            return
        # Best-effort cleanup of any leftover prepared exes
        for batch_exe in list(self._prepared_exes.values()):
            try:
                self.simulator.cleanup_exe(batch_exe)
            except Exception:
                pass
        self._prepared_exes = {}
        # Wipe queue
        self.scene_entries.clear()
        self.refresh_scene_queue()
        self.set_status("Idle")
        self.set_progress(0)
        self.query_one("#log_panel", RichLog).clear()
        # Restore exe input to the configured default + re-apply profile
        self.query_one("#exe_input", Input).value = self.simulator.default_exe
        self.query_one("#add_file_input", Input).value = ""
        self.current_step_marker = None
        self._last_profile_name = None
        self.stop_requested = False
        self.force_stopped_current = False
        self.current_entry = None
        self.apply_sim_type(self.simulator.default_exe)
        # Reset run-time stats
        self.current_warnings = 0
        self.current_errors = 0
        self.finish_current_case()
        self.query_one("#current_case_label", Static).update("No case running")
        self.query_one("#current_step_label", Static).update("Step: -")
        self.query_one("#current_stats_label", Static).update("Elapsed: - | Warnings: 0 | Errors: 0")
        self.reset_run_controls()
        self.switch_tab("setup")

    def on_paste(self, event: events.Paste):
        """Drag-and-drop into a Textual app comes through as a bracketed paste.
        If a target Input is focused, replace its value; otherwise route to the
        scene input so dragging a scene file anywhere on the Setup tab works."""
        if self.batch_running:
            return
        text = event.text or ""
        # Normalize: terminals often append CR/LF and may wrap with quotes
        text = text.replace("\r", "").replace("\n", " ").strip()
        if not text:
            return
        focused = self.focused
        if isinstance(focused, Input) and focused.id == "exe_input":
            focused.value = strip_quotes(text)
            event.stop()
            return
        # Default: route to scene input (works whether or not it had focus)
        add_input = self.query_one("#add_file_input", Input)
        add_input.value = text  # keep quotes; _add_from_input uses shlex.split
        add_input.focus()
        event.stop()

    def action_clear_log(self):
        self.query_one("#log_panel", RichLog).clear()

    def action_show_tab(self, tab_id: str):
        self.switch_tab(tab_id)

    @staticmethod
    def _reset_entry_run_state(entry: SceneEntry):
        """Clear the per-entry run-state fields so the worker picks it up
        as a fresh PENDING case again."""
        entry.status = STATUS_PENDING
        entry.returncode = None
        entry.elapsed = None
        entry.warnings = 0
        entry.errors = 0

    @on(Button.Pressed, "#start_btn")
    def on_start(self):
        """Re-run the whole queue: every non-pending entry (done / failed /
        missing / error / stopped) is reset to pending before launching."""
        if self.batch_running:
            return
        for entry in self.scene_entries:
            if entry.status != STATUS_PENDING:
                self._reset_entry_run_state(entry)
        self.refresh_scene_queue()
        self._launch_batch()

    def action_start(self):
        self.on_start()

    @on(Button.Pressed, "#stop_btn")
    def on_stop(self):
        """Graceful stop: let the current case finish naturally, then exit
        the batch. Pending cases remain pending so RESUME can pick up."""
        if not self.batch_running:
            return
        self.stop_requested = True
        self.log_line("--- Stop requested: current case will finish then batch exits ---", "warning")

    def action_stop(self):
        self.on_stop()

    @on(Button.Pressed, "#force_stop_btn")
    def on_force_stop(self):
        """Immediate stop: kill the running subprocess and every child it
        spawned (taskkill /F /T on Windows). The current entry is marked
        STOPPED (removable) instead of FAILED."""
        if not self.batch_running:
            return
        self.stop_requested = True
        self.force_stopped_current = True
        for proc in self.process_holder:
            kill_proc_tree(proc)
        self.log_line("--- Force stop: terminating current case (process tree) ---", "warning")

    @on(Button.Pressed, "#resume_btn")
    def on_resume(self):
        """Continue the batch: pending entries get processed, and any
        force-stopped entries are re-queued first. Already finished cases
        (done / failed / missing / error) stay as a record."""
        if self.batch_running:
            return
        for entry in self.scene_entries:
            if entry.status == STATUS_STOPPED:
                self._reset_entry_run_state(entry)
        self.refresh_scene_queue()
        self._launch_batch()

    # ---------- batch orchestration ----------

    def _launch_batch(self):
        if not self.scene_entries:
            self.log_line("No scene entries in the queue.", "error")
            return
        # Only entries still pending will be processed; skip everything else.
        pending = [e for e in self.scene_entries if e.status == STATUS_PENDING]
        if not pending:
            self.log_line("No pending entries to run.", "warning")
            return

        # Validate exes for pending entries
        unique_exes = []
        for e in pending:
            if e.exe_path not in unique_exes:
                unique_exes.append(e.exe_path)
        missing = [x for x in unique_exes if not os.path.isfile(x)]
        if missing:
            for m in missing:
                self.log_line(f"Simulator exe not found: {m}", "error")
            return

        self.simulator.write_console = lambda msg, kind="info": self.call_from_thread(self.log_line, msg, kind)

        self.batch_running = True
        self.stop_requested = False
        self.force_stopped_current = False
        self.process_holder = []
        self.query_one("#start_btn", Button).disabled = True
        self.query_one("#resume_btn", Button).disabled = True
        self.query_one("#stop_btn", Button).disabled = False
        self.query_one("#force_stop_btn", Button).disabled = False
        self.query_one("#reset_btn", Button).disabled = True
        self.set_progress(0)
        self.switch_tab("running")

        # Pass the FULL entry list so per-entry indices in status messages
        # reflect their queue position; worker skips non-pending internally.
        self._run_batch_worker(list(self.scene_entries))

    def _mark_status(self, entry: SceneEntry, status: str):
        """Update the entry's status field and repaint the queue table.

        Called from the worker thread via call_from_thread.
        """
        entry.status = status
        self.refresh_scene_queue()

    @work(thread=True, exclusive=True)
    def _run_batch_worker(self, entries: list[SceneEntry]):
        sim = self.simulator
        total = len(entries)
        case_names: list[str] = []
        time_costs: list[int] = []
        total_warnings = 0
        total_errors = 0
        total_failures = 0
        # Count only what we'll actually run, for the Telegram digest.
        runnable = sum(1 for e in entries if e.status == STATUS_PENDING)

        try:
            sim.info("Start processing", tag="Batch")
            sim.tg.queue_message("#Batch Batch settings:")
            sim.tg.queue_message(f"Pending cases to run: {runnable} / {total}")
            sim.tg.queue_message(f"Distinct simulators: {len({e.exe_path for e in entries if e.status == STATUS_PENDING})}")
            sim.tg.send_telegram_message_batch()

            for i, entry in enumerate(entries):
                if self.stop_requested:
                    self.call_from_thread(self.log_line, "--- Batch stopped by user ---", "warning")
                    break
                # Resume support: skip anything that's already been processed.
                if entry.status != STATUS_PENDING:
                    continue

                case_name = sim.case_name_from_path(entry.scene_path)
                case_names.append(case_name)
                self.current_entry = entry
                self.call_from_thread(self.set_status, f"Case {i+1}/{total}: {case_name}")
                self.call_from_thread(self.set_progress, (i / total) * 100)
                self.call_from_thread(self.start_current_case, case_name, i, total)
                self.call_from_thread(self._mark_status, entry, STATUS_RUNNING)

                if not os.path.exists(entry.scene_path):
                    entry.elapsed = -1
                    total_failures += 1
                    sim.info(f"File '{entry.scene_path}' not found.", tag="Case")
                    self.call_from_thread(self._mark_status, entry, STATUS_MISSING)
                    time_costs.append(-1)
                    self.current_entry = None
                    continue

                # Per-case OMP env (None unsets)
                sim.set_omp_env(entry.omp_threads)

                # Per-case prepared exe (cached per source exe path)
                if entry.exe_path not in self._prepared_exes:
                    try:
                        self._prepared_exes[entry.exe_path] = sim.prepare_exe(entry.exe_path)
                    except Exception as e:
                        self.call_from_thread(self.log_line, f"Failed to prepare exe: {e}", "error")
                        entry.elapsed = -1
                        total_failures += 1
                        self.call_from_thread(self._mark_status, entry, STATUS_ERROR)
                        time_costs.append(-1)
                        self.current_entry = None
                        continue
                batch_exe = self._prepared_exes[entry.exe_path]

                # Update step_marker for the current case's exe
                self.current_step_marker = profile_step_marker(sim.identify_profile(entry.exe_path))

                self.process_holder.clear()

                def on_line(line, kind):
                    self.call_from_thread(self.log_line, line, kind)

                try:
                    result = sim.run_case(
                        batch_exe, entry.scene_path, i, total, entry.mpi_ranks,
                        on_line=on_line, process_holder=self.process_holder,
                    )
                except Exception as e:
                    self.call_from_thread(self.log_line, f"Exception in case: {e}", "error")
                    entry.elapsed = -1
                    total_failures += 1
                    self.call_from_thread(self._mark_status, entry, STATUS_ERROR)
                    time_costs.append(-1)
                    self.current_entry = None
                    continue

                entry.returncode = result.returncode
                entry.warnings = result.warnings
                entry.errors = result.errors
                total_warnings += result.warnings
                total_errors += result.errors

                if self.force_stopped_current:
                    # Force-stop terminated the subprocess; categorise as STOPPED
                    # (removable) instead of FAILED so the user can clean it up.
                    self.force_stopped_current = False
                    entry.elapsed = -1
                    time_costs.append(-1)
                    self.call_from_thread(self.finish_current_case)
                    self.call_from_thread(self._mark_status, entry, STATUS_STOPPED)
                    self.current_entry = None
                    continue

                if result.returncode == 0:
                    entry.elapsed = result.elapsed
                    time_costs.append(result.elapsed)
                    if entry.zip_output:
                        if not result.output_folder:
                            sim.info(
                                f"No output directory detected in log for '{case_name}'; skipping zip/remove.",
                                tag="Case",
                            )
                        else:
                            zipped = sim.zip_case_output(case_name, result.output_folder)
                            if entry.remove_output:
                                if zipped:
                                    sim.remove_case_output(case_name, result.output_folder)
                                else:
                                    sim.info(f"Output removal cancelled for case '{case_name}'", tag="Case")
                    final_status = STATUS_DONE
                else:
                    entry.elapsed = -1
                    total_failures += 1
                    time_costs.append(-1)
                    final_status = STATUS_FAILED

                self.call_from_thread(self.finish_current_case)
                self.call_from_thread(self._mark_status, entry, final_status)
                self.call_from_thread(
                    self.set_status,
                    f"Done {i+1}/{total} | "
                    f"Warnings: {total_warnings} | Errors: {total_errors} | Failures: {total_failures}",
                )
                self.current_entry = None

            self.call_from_thread(self.set_progress, 100)
            sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)
            sim.info("All done", tag="Batch")

        finally:
            for batch_exe in list(self._prepared_exes.values()):
                if sim.cleanup_exe(batch_exe):
                    self.call_from_thread(self.log_line, f"Cleaned up: {batch_exe}", "info")
            self._prepared_exes = {}
            self.batch_running = False
            self.current_entry = None
            self.call_from_thread(self.reset_run_controls)
            self.call_from_thread(self.finish_current_case)
            self.call_from_thread(self.switch_tab, "setup")
            self.call_from_thread(
                self.set_status,
                f"Idle | "
                f"Warnings: {total_warnings} | Errors: {total_errors} | Failures: {total_failures}",
            )

    def on_unmount(self):
        for batch_exe in list(self._prepared_exes.values()):
            try:
                self.simulator.cleanup_exe(batch_exe)
            except Exception:
                pass


def main():
    BatchSimuApp().run()


if __name__ == "__main__":
    main()

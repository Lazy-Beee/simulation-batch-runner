"""Textual TUI frontend for batch simulation - 2-tab layout (Setup + Running)."""

import os
import sys
import queue
import threading
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
from textual.screen import ModalScreen
from textual.widgets import (
    Footer, Input, Label, Button, Switch,
    Static, RichLog, ProgressBar,
    TabbedContent, TabPane, DataTable,
)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:  # graceful: TopBar shows a hint instead of stats
    psutil = None
    HAS_PSUTIL = False

import re

from simulation import (
    Simulator, load_config,
    profile_name, profile_supports_mpi, profile_step_pattern, profile_eta_pattern,
    CLEANUP_KEEP, CLEANUP_FOLDER, CLEANUP_BOTH,
)


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"   # scene file didn't exist when we tried to run it
STATUS_ERROR = "error"       # exception during run
STATUS_STOPPED = "stopped"   # process was force-stopped mid-run

REMOVABLE_STATUSES = {STATUS_PENDING, STATUS_STOPPED}
FINISHED_STATUSES = {STATUS_DONE, STATUS_FAILED, STATUS_MISSING, STATUS_ERROR, STATUS_STOPPED}

ROW_STYLE_BY_STATUS = {
    STATUS_PENDING: "",
    STATUS_RUNNING: "black on yellow",
    STATUS_DONE: "on dark_green",
    STATUS_FAILED: "on red3",
    STATUS_MISSING: "on red3",
    STATUS_ERROR: "on red3",
    STATUS_STOPPED: "on grey35",
}

# Cleanup control: cycle order, Add-row button label, queue-column label.
CLEANUP_CYCLE = [CLEANUP_KEEP, CLEANUP_FOLDER, CLEANUP_BOTH]
CLEANUP_BTN_LABEL = {CLEANUP_KEEP: "Keep", CLEANUP_FOLDER: "Folder", CLEANUP_BOTH: "Both"}
CLEANUP_COL = {CLEANUP_KEEP: "keep", CLEANUP_FOLDER: "fldr", CLEANUP_BOTH: "both"}

QUEUE_COLS = [
    ("#", 5),
    ("Simulator", 22),
    ("Scene", 26),
    ("OMP", 5),
    ("MPI", 5),
    ("Zip", 5),
    ("Clean", 6),
    ("Upl", 5),
    ("Status", 10),
    ("Time", 10),
    ("ETA", 10),
    ("Warnings", 9),
    ("Errors", 7),
]

# rclone's --stats-one-line progress line, e.g.
# "... 30 MiB, 54%, 8.0 MiB/s, ETA 1s". Captures the percent so long uploads
# can be throttled to one log line per 20% band.
_UPLOAD_PROGRESS_RE = re.compile(r"(\d+)%,.*ETA")


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
    return s


def styled_cell(value, width: int, style: str) -> Text:
    # ljust pads with spaces and the style covers the whole span, so the row
    # background colour fills the entire cell instead of just the text.
    return Text(str(value).ljust(width), style=style)


class TopBar(Horizontal):
    """title | spacer | CPU/MEM | clock."""

    DEFAULT_CSS = """
    TopBar {
        dock: top;
        height: 1;
        background: $accent;
        color: $background;
    }
    TopBar Label {
        height: 1;
        background: $accent;
        color: $background;
        padding: 0 1;
    }
    TopBar #topbar_title { padding: 0 2; text-style: bold; }
    TopBar #topbar_spacer { width: 1fr; }
    TopBar #topbar_stats { width: auto; }
    TopBar #topbar_clock { width: auto; padding: 0 2; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Batch Simulation", id="topbar_title")
        yield Label("", id="topbar_spacer")
        yield Label("CPU --.- % | MEM --.- GB / --.- GB", id="topbar_stats")
        yield Label("--:--:--", id="topbar_clock")


def kill_proc_tree(proc):
    """Terminate a process and all its children. Uses taskkill /F /T on Windows
    so .bat / wrapper exes don't leave the real worker alive."""
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
    """One queued case: settings snapshotted at Add time plus mutable run state."""
    exe_path: str
    scene_path: str
    omp_threads: Optional[int]   # None = no OMP limit
    mpi_ranks: int               # 0 = MPI disabled
    zip_output: bool
    cleanup: str
    upload_output: bool
    status: str = STATUS_PENDING
    returncode: Optional[int] = None
    elapsed: Optional[int] = None
    warnings: int = 0
    errors: int = 0
    eta: Optional[str] = None
    log_buffer: list = field(default_factory=list)
    # Per-case private copy of the source exe, taken at Add time. Each entry
    # owns its own copy so re-compiling mid-batch only affects later adds.
    batch_exe_path: Optional[str] = None
    # Runtime-only (not part of the queued config). Per-entry so cases can run
    # concurrently: proc_holder holds the live Popen for force-stop, run_start
    # drives the live elapsed tick, force_stopped flags a user kill.
    proc_holder: list = field(default_factory=list, repr=False)
    run_start: Optional[float] = field(default=None, repr=False)
    # Anchor for the live ETA countdown: parsed seconds of the most recent
    # simulator ETA token, and the wall-clock time we captured it. The queue
    # cell shows eta_anchor_seconds - (now - eta_anchor_at) so a stale token
    # keeps ticking down between the simulator's (often sparse) ETA updates
    # instead of sitting frozen. seconds=None marks a token we couldn't parse.
    eta_anchor_seconds: Optional[int] = field(default=None, repr=False)
    eta_anchor_at: Optional[float] = field(default=None, repr=False)
    force_stopped: bool = field(default=False, repr=False)
    zip_done: bool = field(default=False, repr=False)
    upload_done: bool = field(default=False, repr=False)


def format_sim_type_text(simulator: Simulator, exe_path: str) -> str:
    profile = simulator.identify_profile(exe_path)
    if profile is None:
        base = "Type: unknown"
    else:
        name = profile_name(profile)
        if not profile_supports_mpi(profile):
            base = f"Type: {name} (single-process only - MPI not supported)"
        else:
            base = f"Type: {name}"
    if exe_path and not os.path.isfile(exe_path):
        base += "  [red](file not found)[/red]"
    return base


def format_drag_target_text(target_id: str) -> str:
    name = "Simulator" if target_id == "exe_input" else "Scene"
    return f"Drag target: {name}  (click a field to switch)"


def strip_quotes(s: str) -> str:
    """Strip outer quotes - Windows Terminal drag-drop inserts them."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


class IntStepInput(Input):
    """Integer Input that also steps by 1 with the Up / Down arrow keys.

    A blank field steps to min_value on the first press; values are clamped to
    [min_value, max_value] when those are set.
    """
    BINDINGS = [
        Binding("up", "step(1)", "Increment", show=False),
        Binding("down", "step(-1)", "Decrement", show=False),
    ]

    def __init__(self, *args, min_value: Optional[int] = None,
                 max_value: Optional[int] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._min = min_value
        self._max = max_value

    def action_step(self, delta: int) -> None:
        raw = self.value.strip()
        base = self._min if self._min is not None else 0
        if raw:
            try:
                base = int(raw) + delta
            except ValueError:
                pass
        if self._min is not None:
            base = max(self._min, base)
        if self._max is not None:
            base = min(self._max, base)
        self.value = str(base)
        self.cursor_position = len(self.value)


class StepButton(Static):
    """A height-1 clickable arrow that steps a target IntStepInput by delta."""

    def __init__(self, label: str, target_id: str, delta: int, **kwargs):
        super().__init__(label, **kwargs)
        self._target_id = target_id
        self._delta = delta

    def on_click(self, event) -> None:
        event.stop()
        target = self.app.query_one(f"#{self._target_id}", IntStepInput)
        if not target.disabled:   # ignore clicks while the field is locked
            target.action_step(self._delta)


class QuitConfirmScreen(ModalScreen[bool]):
    """Confirm quitting while a batch / zip / upload is still in flight.

    Dismisses True to quit (the caller then kills every child process so an
    orphaned rclone / 7z doesn't keep holding the launching console) or False
    to stay. Escape and the Cancel button both stay.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, summary: str):
        super().__init__()
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="quit_dialog"):
            yield Label("Work still in progress", id="quit_title")
            yield Label(self._summary, id="quit_summary")
            yield Label(
                "Quitting now kills all running simulator, 7-Zip and rclone "
                "processes; unfinished cases are left incomplete.",
                id="quit_detail",
            )
            with Horizontal(id="quit_buttons"):
                yield Button("Quit and kill all", variant="error", id="quit_yes")
                yield Button("Cancel", variant="primary", id="quit_no")

    def on_mount(self):
        # Default focus to Cancel so a stray Enter doesn't kill the batch.
        self.query_one("#quit_no", Button).focus()

    @on(Button.Pressed, "#quit_yes")
    def _confirm(self):
        self.dismiss(True)

    @on(Button.Pressed, "#quit_no")
    def _cancel(self):
        self.dismiss(False)

    def action_cancel(self):
        self.dismiss(False)


class BatchSimuApp(App):
    CSS = """
    Screen { layout: vertical; }
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; }

    /* Setup tab: flat layout - top widgets + scene_queue (1fr) + bottom widgets.
       scene_queue is the only fr-sized child, so it absorbs all remaining
       vertical space inside setup_panel. Left/right padding keeps text,
       buttons, and the progress bar off the tab edges. */
    #setup_panel { height: 1fr; padding: 0 1; }
    #scene_queue { height: 1fr; border: solid $accent; }

    .row { width: 100%; height: 3; align: left middle; }
    /* Breathing room between groups on the Queue tab. Sub-labels like
       sim_type_label / drag_target_label hug their row above; the
       queue table and the status line each get a break of their own.
       The very first row (Simulator input) sits flush against the top
       of the tab. */
    #setup_panel > .row { margin-top: 1; }
    #setup_panel > #simulator_row { margin-top: 0; }
    #setup_panel > #status_label { margin-top: 1; }
    Input { width: 1fr; }
    .narrow { width: 8; }
    /* OMP holds a 2-digit value (default 24); the baseline narrow width clips
       it once focused (cursor pushes it out of view), so give OMP one more cell. */
    #omp_input { width: 9; }
    /* Vertical mini-stepper: arrows docked to the top/bottom edges of the
       3-row field so they bracket the value symmetrically (a 2-row pair can't
       sit dead-centre in 3 rows, so frame it instead). */
    .stepper { width: 1; height: 3; }
    .stepper > StepButton { width: 1fr; height: 1; content-align: center middle; color: $text-muted; }
    .stepper > .step_up { dock: top; }
    .stepper > .step_down { dock: bottom; }
    .stepper > StepButton:hover { background: $accent; color: $background; }
    .stepper:disabled > StepButton { color: $text-disabled; }
    Button { margin-right: 1; }
    Label { margin: 0 1; }
    /* Pad "Simulator:" / "Scene:" labels to the same width so the two
       Input fields start at the same column, and emphasise them. */
    .field_label { width: 11; text-style: bold; }
    /* Breathing room between the progress bar and the bottom of the tab. */
    #progress { margin-bottom: 1; }

    /* Vertically center short widgets so they line up with Input/Button */
    .row > Label, .row > Switch {
        height: 3;
        content-align: left middle;
    }

    /* Push Reset to the right edge of its row */
    #bottom_filler { width: 1fr; }

    #sim_type_label, #status_label,
    #current_case_label, #current_step_label,
    #summary_label, #drag_target_label {
        width: 100%;
    }
    /* Inside a Horizontal with the Copy button - share the row. */
    #current_stats_label { width: 1fr; }

    /* Sub-labels for the Simulator / Scene rows: dimmed so they read as
       hints rather than primary content. */
    #sim_type_label, #drag_target_label { color: $text-muted; }

    #clear_exe_btn, #clear_scene_btn { width: 9; min-width: 9; }
    /* Compact button - 1 line, no border/padding so it lines up with the
       Static labels on either side inside a height:1 toolbar row. */
    .copy_log_btn {
        width: 9; min-width: 9;
        height: 1; min-height: 1;
        padding: 0;
        border: none;
    }

    #log_panel { height: 1fr; border: solid $accent; }
    .case_log_panel { height: 1fr; border: solid $accent; }
    #done_table { height: 1fr; }

    #current_case_label, #current_step_label, #current_stats_label {
        padding: 0 1;
    }
    /* Compact 1-line toolbar (stats + Copy button) so the three Running
       header lines stay equal height and visually aligned. */
    #running_toolbar { height: 1; }
    /* Case-tab header mirrors the Running tab: case label (accent bold) +
       exe label (plain) + stats row with Copy button, each on one line. */
    /* Case-tab row 1 hosts the Close button next to the case-name label. */
    .case_tab_case_label { width: 1fr; padding: 0 1; color: $accent; text-style: bold; }
    .case_tab_exe_label { width: 100%; padding: 0 1; }
    .case_tab_header { width: 1fr; padding: 0 1; }
    .case_tab_toolbar_row { height: 1; }
    /* Compact Close button - same style as the Copy button. */
    .close_log_btn {
        width: 9; min-width: 9;
        height: 1; min-height: 1;
        padding: 0;
        border: none;
    }
    #current_case_label { color: $accent; text-style: bold; }

    #summary_label { padding: 1; text-style: bold; }

    /* Quit-confirmation modal: centered dialog over the dimmed app. */
    QuitConfirmScreen { align: center middle; }
    #quit_dialog {
        width: 66; height: auto;
        padding: 1 2; border: thick $error;
        background: $surface;
    }
    #quit_title { width: 100%; text-style: bold; color: $error; }
    #quit_summary { width: 100%; margin-top: 1; }
    #quit_detail { width: 100%; margin-top: 1; color: $text-muted; }
    #quit_buttons { width: 100%; height: auto; align: center middle; margin-top: 1; }
    """

    BINDINGS = [
        Binding("ctrl+s", "start", "Start", priority=True),
        # ctrl+x and ctrl+w collide with Input's cut / delete-word. show=False
        # keeps the Footer key order stable when an Input is focused; ctrl+x
        # stays priority=True so Stop still fires from inside an Input, ctrl+w
        # stays non-priority so Input's delete-word still works there.
        Binding("ctrl+x", "stop", "Stop", priority=True, show=False),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "show_tab('setup')", "Queue"),
        Binding("f2", "show_tab('running')", "Running"),
        Binding("ctrl+w", "close_tab", "Close case tab", show=False),
        Binding("space", "toggle_select", "Select row"),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("escape", "clear_selection", "Clear selection", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.simulator = Simulator(self.config)
        self.scene_entries: list[SceneEntry] = []
        self.add_cleanup = self.simulator.default_cleanup   # current Add-row Cleanup choice
        self.stop_requested = False
        self.batch_running = False
        # Step pattern of the running profile; cleared under parallel runs where
        # cases may use different profiles. Drives the latest-step label only.
        self.current_step_re: Optional[re.Pattern] = None
        # Re-snap OMP/MPI defaults only when the matched profile actually
        # changes, so manual toggles survive further typing in the exe field.
        self._last_profile_name: str | None = None
        self._current_col_widths: Optional[list[int]] = None

        # Background zip / cleanup queue. Each finished case enqueues a task
        # (run by _zip_worker_loop) so the next case can start while the
        # previous one is being archived. Drained at batch end.
        self._zip_queue: queue.Queue = queue.Queue()
        self._zip_worker: Optional[threading.Thread] = None
        # Separate upload queue so transfers (network) overlap zipping the next
        # case (disk). Gated independently by upload.async. Drained after zips.
        self._upload_queue: queue.Queue = queue.Queue()
        self._upload_worker: Optional[threading.Thread] = None
        # Live 7-Zip / rclone Popen handles, keyed by id(holder), so a confirmed
        # quit can kill them instead of orphaning the processes (an orphaned
        # rclone keeps the launching console busy). Each zip / upload task
        # registers its own holder for the duration of its run.
        self._aux_holders: dict[int, list] = {}
        self._aux_lock = threading.Lock()

        # Multi-select for the queue table. Stores id(entry) so stale row
        # indices after reorder / remove don't shadow current selections.
        self._selected_ids: set[int] = set()

        # Drag-drop / paste target. Tracked from focus events instead of
        # App.focused or mouse_over because OLE drag-drop is modal on
        # Windows: switching to the file manager clears focus, and no
        # MouseMove events are delivered while the drag is in progress.
        self._paste_target_id: str = "add_file_input"

    def compose(self) -> ComposeResult:
        yield TopBar()
        with TabbedContent(initial="setup"):
            with TabPane("Queue", id="setup"):
                with Vertical(id="setup_panel"):
                    with Horizontal(id="simulator_row", classes="row"):
                        yield Label("Simulator:", classes="field_label")
                        yield Input(
                            value=self.simulator.default_exe,
                            id="exe_input",
                            placeholder="path to simulator exe (drag a file in or paste)",
                        )
                        yield Button("Clear", id="clear_exe_btn")
                    yield Static(format_sim_type_text(self.simulator, self.simulator.default_exe), id="sim_type_label")

                    with Horizontal(classes="row"):
                        yield Label("Scene:", classes="field_label")
                        yield Input(
                            id="add_file_input",
                            placeholder="drag scene file(s) in or paste; Enter adds them with the settings below",
                        )
                        yield Button("Clear", id="clear_scene_btn")
                    yield Static(format_drag_target_text(self._paste_target_id), id="drag_target_label")

                    with Horizontal(classes="row"):
                        yield Switch(value=False, id="omp_switch")
                        yield Label("OMP")
                        yield IntStepInput(
                            value=str(self.simulator.default_omp_threads),
                            id="omp_input", classes="narrow", type="integer", min_value=1,
                        )
                        yield Switch(value=False, id="mpi_switch")
                        yield Label("MPI")
                        yield IntStepInput(
                            value=str(self.simulator.default_mpi_ranks),
                            id="mpi_input", classes="narrow", type="integer", min_value=1,
                        )
                        yield Switch(value=self.simulator.default_zip, id="zip_switch")
                        yield Label("Zip")
                        yield Button(f"Clean: {CLEANUP_BTN_LABEL[self.add_cleanup]}", id="cleanup_btn")
                        yield Switch(value=self.simulator.upload_enabled, id="upload_switch")
                        yield Label("Upload")
                        yield Button("Add", id="add_btn", variant="primary")

                    with Horizontal(classes="row"):
                        yield Button("Up", id="up_btn")
                        yield Button("Down", id="down_btn")
                        yield Button("View log", id="view_log_btn")
                        yield Button("Remove selected", id="remove_btn")

                    yield DataTable(id="scene_queue", zebra_stripes=True, cell_padding=0)

                    with Horizontal(classes="row"):
                        yield Button("START", id="start_btn", variant="success")
                        yield Button("STOP", id="stop_btn", variant="error", disabled=True)
                        yield Button("FORCE STOP", id="force_stop_btn", variant="error", disabled=True)
                        yield Button("RESUME", id="resume_btn", variant="primary")
                        yield Label("  Parallel")
                        yield IntStepInput(
                            value=str(self.simulator.default_parallel_cases),
                            id="parallel_input", classes="narrow", type="integer", min_value=1,
                        )
                        with Vertical(classes="stepper", id="parallel_stepper"):
                            yield StepButton("▲", "parallel_input", 1, classes="step_up")
                            yield StepButton("▼", "parallel_input", -1, classes="step_down")
                        yield Static("", id="bottom_filler")
                        yield Button("Reset", id="reset_btn", variant="warning")
                    yield Static("Idle", id="status_label")
                    yield ProgressBar(id="progress", total=100, show_eta=False)

            with TabPane("Running", id="running"):
                with Vertical():
                    yield Static("No case running", id="current_case_label")
                    yield Static("Step: -", id="current_step_label")
                    with Horizontal(id="running_toolbar"):
                        yield Static("Elapsed: - | Warnings: 0 | Errors: 0", id="current_stats_label")
                        yield Button("Copy", classes="copy_log_btn")
                    yield RichLog(
                        id="log_panel",
                        highlight=False,
                        markup=True,
                        wrap=False,
                        max_lines=10000,
                    )

        yield Footer()

    _KIND_COLOR = {
        "error": "red",
        "warning": "yellow",
        "step": "cyan",
        "info": "blue",
    }

    @classmethod
    def _write_log_line(cls, widget: RichLog, line: str, kind: str):
        line = line.rstrip("\n")
        color = cls._KIND_COLOR.get(kind)
        if color:
            widget.write(f"[{color}]{line}[/{color}]")
        else:
            widget.write(line)

    def log_line(self, line: str, kind: str = "raw"):
        widget = self.query_one("#log_panel", RichLog)
        self._write_log_line(widget, line, kind)
        if kind == "step":
            self.query_one("#current_step_label", Static).update(
                f"Step: {self._format_step_text(line)}"
            )

    def _format_step_text(self, line: str) -> str:
        r = self.current_step_re
        if r is not None:
            m = r.search(line)
            if m:
                if m.lastindex:
                    return m.group(1).rstrip()
                return line[m.start():].rstrip()
        return line.rstrip()

    def _refresh_current_stats(self):
        if self.batch_running:
            now = _time.time()
            for e in self.scene_entries:
                if e.status == STATUS_RUNNING and e.run_start is not None:
                    e.elapsed = round(now - e.run_start)
            running = sum(1 for e in self.scene_entries if e.status == STATUS_RUNNING)
            done = sum(1 for e in self.scene_entries if e.status in FINISHED_STATUSES)
            total = len(self.scene_entries) or 1
            self.query_one("#current_stats_label", Static).update(
                f"Running: {running} | Done: {done}/{total} | "
                f"Warnings: {sum(e.warnings for e in self.scene_entries)} | "
                f"Errors: {sum(e.errors for e in self.scene_entries)}"
            )
            self.set_status(f"Running {running} | Done {done}/{total}")
            self.set_progress((done / total) * 100)
        # Repaint every tick so file-existence changes flip the `[!]` marker
        # even while idle.
        self.refresh_scene_queue()

    def set_status(self, text: str):
        self.query_one("#status_label", Static).update(text)

    def set_progress(self, percent: float):
        self.query_one("#progress", ProgressBar).update(progress=percent)

    @staticmethod
    def _short(path: str, width: int) -> str:
        """Basename without extension; head + ellipsis + tail when too wide."""
        if not path:
            return "(empty)"
        stem = os.path.splitext(os.path.basename(path))[0]
        if len(stem) <= width:
            return stem
        keep = max(width - 1, 1)
        head = keep // 2
        tail = keep - head
        return stem[:head] + "…" + stem[-tail:]

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
    def _fmt_stage(enabled: bool, done: bool) -> str:
        # '-' disabled, 'Y' enabled/pending, 'D' completed for this case
        if not enabled:
            return "-"
        return "D" if done else "Y"

    @staticmethod
    def _fmt_time(elapsed: Optional[int]) -> str:
        # H:MM:SS left-flush; column ljust pads the trailing space. Sidesteps
        # timedelta's "X days, ..." rollover so anything < 1000h fits the cell.
        if elapsed is None or elapsed < 0:
            return "-"
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"

    @staticmethod
    def _parse_eta_seconds(eta_token: Optional[str]) -> Optional[int]:
        # Total seconds for a simulator ETA token, or None if unrecognised.
        # Accepts '<1m', 'XhYm' / 'Xh Ym' (CAMMP), or 'Xm' (>=60 carries).
        # Anchors the per-second countdown in the queue (see _fmt_eta_cell).
        if not eta_token:
            return None
        eta_token = eta_token.strip()
        if eta_token == "<1m":
            return 0
        m = re.match(r"(\d+)h\s*(\d+)m\Z", eta_token)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60
        m = re.match(r"(\d+)m\Z", eta_token)
        if m:
            return int(m.group(1)) * 60
        return None

    @classmethod
    def _fmt_eta(cls, eta_token: Optional[str]) -> str:
        # Static H:MM:SS for a token (no countdown), matching _fmt_time's
        # left-flush format so Time and ETA line up. Unparseable tokens show
        # verbatim; _fmt_eta_cell falls back here for those.
        if not eta_token:
            return "-"
        secs = cls._parse_eta_seconds(eta_token)
        return cls._fmt_time(secs) if secs is not None else eta_token.strip()

    def _fmt_eta_cell(self, entry: SceneEntry, now: float) -> str:
        # Live ETA: count down from the last token's parsed seconds by the
        # wall-clock elapsed since we captured it, so the cell keeps ticking
        # between the simulator's sparse ETA updates. Floors at 0:00:00.
        if not entry.eta:
            return "-"
        secs, at = entry.eta_anchor_seconds, entry.eta_anchor_at
        if secs is None or at is None:
            return self._fmt_eta(entry.eta)
        return self._fmt_time(max(0, round(secs - (now - at))))

    def _format_case_tab_header(self, entry: SceneEntry) -> tuple[str, str, str]:
        case_line = f"Case: {self.simulator.case_name_from_path(entry.scene_path)}"
        exe_line = f"Simulator: {os.path.basename(entry.exe_path)}"
        stats_line = (
            f"Status: {status_display(entry)}"
            f"  |  Time: {self._fmt_time(entry.elapsed)}"
            f"  |  Warnings: {entry.warnings}"
            f"  |  Errors: {entry.errors}"
        )
        return case_line, exe_line, stats_line

    def _compute_col_widths(self) -> list[int]:
        """Distribute width beyond baseline to Simulator / Scene at 1:2,
        rolling the surplus to whichever column is still truncated once
        the other saturates. Other columns stay at baseline; narrower
        than baseline falls back to baseline + DataTable scrolling.

        Uses scrollable_content_region (excludes border, padding, and
        scrollbar gutter) and the DataTable has cell_padding=0, so the
        column-width sum can fill the region exactly.
        """
        base = [w for _, w in QUEUE_COLS]
        try:
            table = self.query_one("#scene_queue", DataTable)
            avail = table.scrollable_content_region.width
        except Exception:
            return base
        base_sim, base_scene = base[1], base[2]
        extra = avail - sum(base)
        if extra <= 0:
            return base

        # Saturation point per column: the smallest width that lets the
        # widest current entry render without _short truncating it. +1
        # matches the trailing-space reservation in refresh_scene_queue;
        # +4 on scene reserves the " [!]" marker for missing files.
        sat_sim = 0
        sat_scene = 0
        for e in self.scene_entries:
            if e.exe_path:
                stem = os.path.splitext(os.path.basename(e.exe_path))[0]
                sat_sim = max(sat_sim, len(stem) + 1)
            if e.scene_path:
                stem = os.path.splitext(os.path.basename(e.scene_path))[0]
                missing_pad = 4 if not os.path.exists(e.scene_path) else 0
                sat_scene = max(sat_scene, len(stem) + missing_pad + 1)
        sim_need = max(0, sat_sim - base_sim)
        scene_need = max(0, sat_scene - base_scene)
        total_need = sim_need + scene_need

        if extra > total_need:
            leftover = extra - total_need
            bonus_sim = sim_need + leftover // 3
            bonus_scene = scene_need + leftover - leftover // 3
        else:
            bonus_sim_12 = extra // 3
            bonus_scene_12 = extra - bonus_sim_12
            if bonus_sim_12 >= sim_need:
                bonus_sim = sim_need
                bonus_scene = extra - sim_need
            elif bonus_scene_12 >= scene_need:
                bonus_scene = scene_need
                bonus_sim = extra - scene_need
            else:
                bonus_sim = bonus_sim_12
                bonus_scene = bonus_scene_12

        widths = list(base)
        widths[1] += bonus_sim
        widths[2] += bonus_scene
        return widths

    def refresh_scene_queue(self):
        table = self.query_one("#scene_queue", DataTable)
        now = _time.time()   # single clock read for every row's live ETA tick
        prev = table.cursor_row if table.row_count else None
        # clear() below resets the scroll to the top, and restoring the cursor
        # re-scrolls it into view; capture the live scroll so a periodic refresh
        # of a long, scrolled queue doesn't snap back to the header.
        prev_scroll_x, prev_scroll_y = table.scroll_x, table.scroll_y
        widths = self._compute_col_widths()
        # Mutating an existing column's width doesn't reliably re-flow the
        # layout, so re-create columns whenever the widths change.
        if widths != self._current_col_widths:
            table.clear(columns=True)
            for (label, _), w in zip(QUEUE_COLS, widths):
                table.add_column(label, width=w)
            self._current_col_widths = widths
        else:
            table.clear()
        sim_w = widths[1]
        scene_w = widths[2]
        for i, e in enumerate(self.scene_entries):
            # -1 reserves a trailing space, -4 more reserves the " [!]" marker.
            missing = bool(e.scene_path) and not os.path.exists(e.scene_path)
            scene_budget = scene_w - 1 - (4 if missing else 0)
            scene_disp = self._short(e.scene_path, scene_budget)
            if missing:
                scene_disp += " [!]"
            sty = ROW_STYLE_BY_STATUS.get(e.status, "")
            marker = "*" if id(e) in self._selected_ids else " "
            values = [
                f"{marker} {i + 1}",
                self._short(e.exe_path, sim_w - 1),
                scene_disp,
                self._fmt_omp(e.omp_threads),
                self._fmt_mpi(e.mpi_ranks),
                self._fmt_stage(e.zip_output, e.zip_done),
                CLEANUP_COL.get(e.cleanup, e.cleanup),
                self._fmt_stage(e.upload_output, e.upload_done),
                status_display(e),
                self._fmt_time(e.elapsed),
                self._fmt_eta_cell(e, now),
                str(e.warnings),
                str(e.errors),
            ]
            table.add_row(*(styled_cell(v, w, sty) for v, w in zip(values, widths)))
        if prev is not None and 0 <= prev < table.row_count:
            # scroll=False so the cursor restore doesn't fight the scroll
            # restore below (which must win for a user-scrolled queue).
            table.move_cursor(row=prev, scroll=False)
        self._restore_queue_scroll(table, prev_scroll_x, prev_scroll_y)

    def _restore_queue_scroll(self, table: DataTable, x: float, y: float):
        # clear() zeroes the scroll and the cursor restore schedules a deferred
        # "scroll cursor into view"; both would drag a mouse-scrolled queue back
        # to the header. Re-apply the saved offset now (flicker-free first paint)
        # and again on the table's own call_after_refresh queue, which runs after
        # the cursor's deferred scroll so this pass wins.
        def _apply():
            table.scroll_to(x=x, y=y, animate=False)
        _apply()
        table.call_after_refresh(_apply)

    def reset_run_controls(self):
        self.query_one("#start_btn", Button).disabled = False
        self.query_one("#stop_btn", Button).disabled = True
        self.query_one("#force_stop_btn", Button).disabled = True
        self.query_one("#resume_btn", Button).disabled = False
        self.query_one("#reset_btn", Button).disabled = False
        # Parallel count is captured at launch; re-enable editing for the next run.
        self.query_one("#parallel_input", Input).disabled = False
        self.query_one("#parallel_stepper").disabled = False

    def apply_sim_type(self, exe_path: str):
        profile = self.simulator.identify_profile(exe_path)
        self.query_one("#sim_type_label", Static).update(format_sim_type_text(self.simulator, exe_path))
        supports = profile_supports_mpi(profile)
        mpi_switch = self.query_one("#mpi_switch", Switch)
        omp_switch = self.query_one("#omp_switch", Switch)

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
        self._sync_input_enabled_state()

    def _sync_input_enabled_state(self):
        """Gray out the OMP / MPI numeric Input whenever its switch is off
        (or the profile forbids MPI). The user can still read the cached
        value but the visual cue makes clear that it won't take effect."""
        omp_input = self.query_one("#omp_input", Input)
        omp_input.disabled = not self.query_one("#omp_switch", Switch).value
        mpi_input = self.query_one("#mpi_input", Input)
        profile = self.simulator.identify_profile(self.query_one("#exe_input", Input).value)
        mpi_input.disabled = (
            not profile_supports_mpi(profile)
            or not self.query_one("#mpi_switch", Switch).value
        )

    @on(Switch.Changed, "#omp_switch")
    @on(Switch.Changed, "#mpi_switch")
    def on_omp_mpi_switch_changed(self):
        self._sync_input_enabled_state()

    def switch_tab(self, tab_id: str):
        # show_tab() in Textual just unhides; tc.active = id is the real switch.
        # Drop focus first or a focused Button on the previous tab snaps the
        # active pane back to keep its widget visible.
        try:
            tc = self.query_one(TabbedContent)
            try:
                self.set_focus(None)
            except Exception:
                pass
            tc.active = tab_id
            self.refresh()
        except Exception:
            pass

    def on_mount(self):
        # First paint runs before layout, so scrollable_content_region is 0
        # and refresh lands on baseline widths. The deferred call re-runs
        # after layout to pick up the bonus distribution.
        self.refresh_scene_queue()
        self.call_after_refresh(self.refresh_scene_queue)
        self.apply_sim_type(self.query_one("#exe_input", Input).value)
        self.set_interval(1.0, self._refresh_current_stats)
        self._refresh_topbar()
        self.set_interval(1.0, self._refresh_topbar)
        # Daemon thread so it dies with the app; tasks queued during a batch
        # are drained before the worker declares idle.
        self._zip_worker = threading.Thread(
            target=self._zip_worker_loop, daemon=True, name="zip-worker",
        )
        self._zip_worker.start()
        self._upload_worker = threading.Thread(
            target=self._upload_worker_loop, daemon=True, name="upload-worker",
        )
        self._upload_worker.start()

    def on_resize(self, event):
        # Deferred: on_resize fires before the DataTable's content_size
        # catches up, so an immediate read returns a stale value.
        self.call_after_refresh(self.refresh_scene_queue)

    def _refresh_topbar(self):
        try:
            clock_text = datetime.datetime.now().strftime("%H:%M:%S")
            self.query_one("#topbar_clock", Label).update(clock_text)
            stats_label = self.query_one("#topbar_stats", Label)
            if HAS_PSUTIL:
                # interval=None = % since the previous call; first call after
                # import is always 0.
                cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                used_gb = vm.used / (1024 ** 3)
                total_gb = vm.total / (1024 ** 3)
                stats_label.update(
                    f"CPU {cpu:4.1f}% | MEM {used_gb:4.1f} GB / {total_gb:4.1f} GB"
                )
            else:
                stats_label.update("(install psutil for CPU/MEM)")
        except Exception:
            pass

    # ---------- event handlers ----------

    @on(Input.Changed, "#exe_input")
    def on_exe_changed(self, event: Input.Changed):
        self.apply_sim_type(event.value)

    @on(Button.Pressed, "#add_btn")
    def on_add(self):
        self._add_from_input()

    @on(Button.Pressed, "#cleanup_btn")
    def on_cycle_cleanup(self):
        # Cycle Keep -> Folder -> Both. 'Both' (delete the archive too) only
        # actually drops the archive when Upload is on and succeeds; the
        # batch worker keeps it otherwise, so selecting it here is always safe.
        idx = CLEANUP_CYCLE.index(self.add_cleanup)
        self.add_cleanup = CLEANUP_CYCLE[(idx + 1) % len(CLEANUP_CYCLE)]
        self.query_one("#cleanup_btn", Button).label = f"Clean: {CLEANUP_BTN_LABEL[self.add_cleanup]}"

    @on(Input.Submitted, "#add_file_input")
    def on_add_submit(self):
        self._add_from_input()

    @on(Button.Pressed, "#clear_exe_btn")
    def on_clear_exe(self):
        exe_input = self.query_one("#exe_input", Input)
        exe_input.value = ""
        exe_input.focus()

    @on(Button.Pressed, "#clear_scene_btn")
    def on_clear_scene(self):
        scene_input = self.query_one("#add_file_input", Input)
        scene_input.value = ""
        scene_input.focus()

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
            # posix=False so Windows-style backslashes survive (POSIX mode
            # treats them as escape chars and silently eats them, turning
            # C:\Users\foo into C:Usersfoo). strip_quotes below handles the
            # quote tokens that posix=False leaves in.
            paths = shlex.split(text, posix=False)
        except ValueError as e:
            self.log_line(f"Error parsing input: {e}", "error")
            return

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
        if not profile_supports_mpi(self.simulator.identify_profile(exe_path)):
            mpi = 0
        zip_out = self.query_one("#zip_switch", Switch).value
        cleanup = self.add_cleanup
        up_out = self.query_one("#upload_switch", Switch).value

        # All entries from one Add share a single exe snapshot. Snapshots only
        # diverge across separate Add calls, so a rebuild mid-batch only affects
        # cases added after it.
        if not os.path.isfile(exe_path):
            self.log_line(f"Simulator exe not found: {exe_path}", "error")
            return
        try:
            shared_batch_exe = self.simulator.prepare_exe(exe_path)
        except Exception as e:
            self.log_line(f"Failed to prepare exe: {e}", "error")
            return

        added = 0
        for p in paths:
            p = strip_quotes(p)
            entry = SceneEntry(
                exe_path=exe_path,
                scene_path=p,
                omp_threads=omp,
                mpi_ranks=mpi,
                zip_output=zip_out,
                cleanup=cleanup,
                upload_output=up_out,
                batch_exe_path=shared_batch_exe,
            )
            self.scene_entries.append(entry)
            added += 1
            if not os.path.exists(p):
                self.log_line(f"Warning: scene file not found: {p}", "warning")

        if added == 0:
            # Defensive: no entries committed, roll back the orphan exe copy.
            try:
                self.simulator.cleanup_exe(shared_batch_exe)
            except Exception:
                pass
        else:
            # Explain the per-case denominator jump on the Telegram side.
            if self.batch_running:
                self.simulator.info(
                    f"Queue extended mid-batch: +{added} case(s), "
                    f"now {len(self.scene_entries)} total",
                    tag="Batch",
                )
            self.refresh_scene_queue()
        inp.value = ""
        inp.focus()

    @on(Button.Pressed, "#view_log_btn")
    async def on_view_log(self):
        if self._selected_ids:
            targets = [e for e in self.scene_entries if id(e) in self._selected_ids]
            eligible = [
                e for e in targets if e.status not in (STATUS_PENDING, STATUS_RUNNING)
            ]
            skipped = len(targets) - len(eligible)
            if not eligible:
                self.log_line(
                    "Selection has no finished entries; nothing to open.",
                    "warning",
                )
                return
            for entry in eligible:
                await self._open_case_tab(entry)
            if skipped:
                self.log_line(
                    f"Opened {len(eligible)} log tab(s); skipped {skipped} pending / running entry(s).",
                    "info",
                )
            return

        table = self.query_one("#scene_queue", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self.scene_entries)):
            self.log_line("Select a row first to view its log.", "warning")
            return
        entry = self.scene_entries[idx]
        if entry.status == STATUS_PENDING:
            self.log_line("That case hasn't run yet - nothing to show.", "warning")
            return
        if entry.status == STATUS_RUNNING:
            self.log_line("That case is still running - watch the Running tab.", "warning")
            return
        await self._open_case_tab(entry)

    async def _open_case_tab(self, entry: SceneEntry):
        tc = self.query_one(TabbedContent)
        tab_id = f"case-{id(entry)}"
        try:
            tc.get_pane(tab_id)
            self.switch_tab(tab_id)
            return
        except Exception:
            pass

        case_name = self.simulator.case_name_from_path(entry.scene_path)
        log_widget = RichLog(
            highlight=False, markup=True, wrap=False, max_lines=20000,
            classes="case_log_panel",
        )
        case_line, exe_line, stats_line = self._format_case_tab_header(entry)
        header_case = Horizontal(
            Static(case_line, classes="case_tab_case_label"),
            Button("Close", classes="close_log_btn"),
            classes="case_tab_toolbar_row",
        )
        header_exe = Static(exe_line, classes="case_tab_exe_label")
        header_stats = Horizontal(
            Static(stats_line, classes="case_tab_header"),
            Button("Copy", classes="copy_log_btn"),
            classes="case_tab_toolbar_row",
        )
        pane = TabPane(case_name, header_case, header_exe, header_stats, log_widget, id=tab_id)
        await tc.add_pane(pane)
        for line, kind in entry.log_buffer:
            self._write_log_line(log_widget, line, kind)
        if not entry.log_buffer:
            log_widget.write("[dim](no output yet)[/dim]")
        # switch_tab's explicit refresh() flushes the tab-bar repaint that
        # Textual sometimes drops right after add_pane.
        self.switch_tab(tab_id)

    async def action_close_tab(self):
        tc = self.query_one(TabbedContent)
        active = tc.active
        if not active or active in ("setup", "running"):
            return
        try:
            await tc.remove_pane(active)
        except Exception:
            pass

    async def _close_all_case_tabs(self):
        tc = self.query_one(TabbedContent)
        for pane in list(tc.query(TabPane)):
            if pane.id and pane.id.startswith("case-"):
                try:
                    await tc.remove_pane(pane.id)
                except Exception:
                    pass

    @on(Button.Pressed, "#remove_btn")
    def on_remove(self):
        if self._selected_ids:
            targets = [
                e for e in self.scene_entries
                if id(e) in self._selected_ids and e.status in REMOVABLE_STATUSES
            ]
            if not targets:
                self.log_line(
                    "Selection has no removable entries (only pending / stopped can go).",
                    "warning",
                )
                return
            for entry in targets:
                self._cleanup_entry_exe(entry)
                self.scene_entries.remove(entry)
                self._selected_ids.discard(id(entry))
            self.refresh_scene_queue()
            self.log_line(f"Removed {len(targets)} selected entries.", "info")
            return

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
        self._cleanup_entry_exe(target)
        self.scene_entries.pop(idx)
        self.refresh_scene_queue()
        self.log_line(f"Removed: {target.scene_path}", "info")

    def _cleanup_entry_exe(self, entry: SceneEntry):
        """Drop the entry's exe-copy ref. Deletes the on-disk copy only when
        no other entry still references it (a batch-Add shares one copy)."""
        path = entry.batch_exe_path
        if not path:
            return
        entry.batch_exe_path = None
        if any(e.batch_exe_path == path for e in self.scene_entries):
            return
        try:
            self.simulator.cleanup_exe(path)
        except Exception:
            pass

    @on(Button.Pressed, "#up_btn")
    def on_move_up(self):
        self._move_selected(-1)

    @on(Button.Pressed, "#down_btn")
    def on_move_down(self):
        self._move_selected(+1)

    def _move_selected(self, delta: int):
        # Only pending entries can be reordered, and not past a non-pending row.
        # Multi-select moves every selected entry as a group; any that hit a
        # boundary or barrier stay where they are while the rest still shift.
        if self._selected_ids:
            self._move_selected_group(delta)
            return
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
        if 0 <= new_idx < table.row_count:
            table.move_cursor(row=new_idx)

    def _move_selected_group(self, delta: int):
        selected = [
            i for i, e in enumerate(self.scene_entries) if id(e) in self._selected_ids
        ]
        if not selected:
            return
        # Process the leading edge first so swaps don't trample each other:
        # top-to-bottom for Up, bottom-to-top for Down.
        order = sorted(selected) if delta < 0 else sorted(selected, reverse=True)
        moved = 0
        for idx in order:
            new_idx = idx + delta
            if not (0 <= new_idx < len(self.scene_entries)):
                continue
            if self.scene_entries[idx].status != STATUS_PENDING:
                continue
            target = self.scene_entries[new_idx]
            if target.status != STATUS_PENDING:
                continue
            # Skip swaps where the target is also selected - that's two members
            # of the group trading places, no net movement for the group.
            if id(target) in self._selected_ids:
                continue
            self.scene_entries[idx], self.scene_entries[new_idx] = (
                self.scene_entries[new_idx],
                self.scene_entries[idx],
            )
            moved += 1
        if moved == 0:
            self.log_line(
                "Nothing to move - selection is blocked by boundary or non-pending neighbours.",
                "warning",
            )
            return
        self.refresh_scene_queue()

    @on(Button.Pressed, "#reset_btn")
    async def on_reset(self):
        if self.batch_running:
            self.log_line("Cannot reset while a batch is running. Stop first.", "warning")
            return
        # Best-effort cleanup of every entry's private exe copy before
        # the queue is wiped.
        for entry in self.scene_entries:
            self._cleanup_entry_exe(entry)
        await self._close_all_case_tabs()
        self.scene_entries.clear()
        self._selected_ids.clear()
        self.refresh_scene_queue()
        self.set_status("Idle")
        self.set_progress(0)
        self.query_one("#log_panel", RichLog).clear()
        self.query_one("#exe_input", Input).value = self.simulator.default_exe
        self.query_one("#add_file_input", Input).value = ""
        self.current_step_re = None
        self._last_profile_name = None
        self.stop_requested = False
        self.apply_sim_type(self.simulator.default_exe)
        self.query_one("#current_case_label", Static).update("No case running")
        self.query_one("#current_step_label", Static).update("Step: -")
        self.query_one("#current_stats_label", Static).update("Idle | Warnings: 0 | Errors: 0")
        self.reset_run_controls()
        self.switch_tab("setup")

    def on_paste(self, event: events.Paste):
        # Drag-and-drop comes through as a bracketed paste. We route by the
        # last-focused input (see on_descendant_focus) since OLE drag-drop is
        # modal on Windows - no MouseMove events arrive during the drag, and
        # self.focused can clear via alt-tab.
        text = event.text or ""
        text = text.replace("\r", "").replace("\n", " ").strip()
        if not text:
            return

        if self._paste_target_id == "exe_input":
            self.query_one("#exe_input", Input).value = strip_quotes(text)
        else:
            # add_file_input keeps quotes; _add_from_input uses shlex.split.
            scene_input = self.query_one("#add_file_input", Input)
            scene_input.value = text
            scene_input.focus()
        event.stop()

    def on_descendant_focus(self, event: events.DescendantFocus):
        w = event.control
        if isinstance(w, Input) and w.id in ("exe_input", "add_file_input"):
            self._set_paste_target(w.id)

    def _set_paste_target(self, target_id: str):
        if target_id == self._paste_target_id:
            return
        self._paste_target_id = target_id
        try:
            self.query_one("#drag_target_label", Static).update(
                format_drag_target_text(target_id)
            )
        except Exception:
            pass

    def action_clear_log(self):
        self.query_one("#log_panel", RichLog).clear()

    def action_copy_log(self):
        # RichLog swallows Textual's click-drag selection (scrollable
        # container eats mouse-down), so the Copy button is the one-shot
        # 'copy everything in this log' path.
        log_widget = self._active_log_widget()
        if log_widget is None:
            self.notify("Switch to Running or a case tab first.", severity="warning")
            return
        lines = [strip.text for strip in log_widget.lines]
        if not lines:
            self.notify("Log is empty.", severity="information")
            return
        text = "\n".join(lines).rstrip() + "\n"
        self.copy_to_clipboard(text)
        self.notify(f"Copied {len(lines)} lines to clipboard.")

    def _active_log_widget(self) -> Optional[RichLog]:
        tc = self.query_one(TabbedContent)
        active = tc.active
        if not active or active == "setup":
            return None
        if active == "running":
            return self.query_one("#log_panel", RichLog)
        try:
            pane = tc.get_pane(active)
            return pane.query_one(RichLog)
        except Exception:
            return None

    @on(Button.Pressed, ".copy_log_btn")
    def on_copy_log(self):
        self.action_copy_log()

    @on(Button.Pressed, ".close_log_btn")
    async def on_close_log(self):
        await self.action_close_tab()

    def action_show_tab(self, tab_id: str):
        self.switch_tab(tab_id)

    def action_toggle_select(self):
        table = self.query_one("#scene_queue", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self.scene_entries)):
            return
        eid = id(self.scene_entries[idx])
        if eid in self._selected_ids:
            self._selected_ids.discard(eid)
        else:
            self._selected_ids.add(eid)
        self.refresh_scene_queue()

    def action_select_all(self):
        self._selected_ids = {id(e) for e in self.scene_entries}
        self.refresh_scene_queue()

    def action_clear_selection(self):
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self.refresh_scene_queue()

    @staticmethod
    def _reset_entry_run_state(entry: SceneEntry):
        entry.status = STATUS_PENDING
        entry.returncode = None
        entry.elapsed = None
        entry.warnings = 0
        entry.errors = 0
        entry.eta = None
        entry.eta_anchor_seconds = None
        entry.eta_anchor_at = None
        entry.proc_holder = []
        entry.run_start = None
        entry.force_stopped = False
        entry.zip_done = False
        entry.upload_done = False

    @on(Button.Pressed, "#start_btn")
    def on_start(self):
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
        if not self.batch_running:
            return
        self.stop_requested = True
        self.log_line("--- Stop requested: running case(s) will finish then batch exits ---", "warning")

    def action_stop(self):
        self.on_stop()

    @on(Button.Pressed, "#force_stop_btn")
    def on_force_stop(self):
        if not self.batch_running:
            return
        self.stop_requested = True
        # Kill every case currently running. Each runner flags its own entry
        # so it reports STOPPED (not FAILED) once its process dies.
        killed = 0
        for entry in self.scene_entries:
            if entry.status == STATUS_RUNNING:
                entry.force_stopped = True
                for proc in entry.proc_holder:
                    kill_proc_tree(proc)
                    killed += 1
        self.log_line(f"--- Force stop: terminating {killed} running process tree(s) ---", "warning")

    @on(Button.Pressed, "#resume_btn")
    def on_resume(self):
        if self.batch_running:
            return
        for entry in self.scene_entries:
            if entry.status == STATUS_STOPPED:
                self._reset_entry_run_state(entry)
        self.refresh_scene_queue()
        self._launch_batch()

    def _console_sink(self, msg, kind="info"):
        # simulator.info() reaches here from two thread contexts: the batch
        # worker thread (must marshal onto the app via call_from_thread) and,
        # for mid-batch Add, directly from a UI event handler already on the
        # app thread (where call_from_thread raises). Route per caller thread.
        if threading.get_ident() == self._thread_id:
            self.log_line(msg, kind)
        else:
            self.call_from_thread(self.log_line, msg, kind)

    def _warn_oversubscription(self, pending):
        # Soft hint only: parallel cases helps only when each case leaves cores
        # free. Flag uncapped OMP or an estimated thread count over the cores.
        cores = os.cpu_count() or 0
        k = self._parallel_k
        uncapped = [e for e in pending if e.omp_threads is None and e.mpi_ranks == 0]
        if uncapped:
            self.log_line(
                f"Parallel={k}: {len(uncapped)} case(s) have no OMP limit; "
                "running them concurrently will oversubscribe the CPU.",
                "warning",
            )
        elif cores:
            peak = max(((e.omp_threads or 1) * (e.mpi_ranks or 1)) for e in pending)
            if peak * k > cores:
                self.log_line(
                    f"Parallel={k}: up to ~{peak * k} threads vs {cores} cores; "
                    "cases may oversubscribe the CPU and run slower.",
                    "warning",
                )

    def _launch_batch(self):
        if not self.scene_entries:
            self.log_line("No scene entries in the queue.", "error")
            return
        pending = [e for e in self.scene_entries if e.status == STATUS_PENDING]
        if not pending:
            self.log_line("No pending entries to run.", "warning")
            return

        # The batch_exe copy is the source of truth at run time; the source
        # exe may have been moved or deleted since Add, but the copy can't be.
        missing = [e for e in pending if not e.batch_exe_path or not os.path.isfile(e.batch_exe_path)]
        if missing:
            for e in missing:
                self.log_line(
                    f"Prepared exe missing for '{e.scene_path}': {e.batch_exe_path}",
                    "error",
                )
            return

        self.simulator.write_console = self._console_sink

        self.batch_running = True
        self.stop_requested = False
        # Concurrency: K cases at once (1 = sequential). Read live from the
        # Parallel input so it can be tuned between runs.
        self._parallel_k = max(1, self._read_int(self.query_one("#parallel_input", Input).value, 1))
        self._batch_lock = threading.Lock()
        self._batch_results: list[dict] = []
        if self._parallel_k > 1:
            self._warn_oversubscription(pending)

        # Switch tab BEFORE disabling the focused button; otherwise Textual
        # auto-moves focus to the next focusable widget on Setup, which
        # competes with the tab switch and snaps the active back to Setup.
        self.switch_tab("running")
        self.query_one("#start_btn", Button).disabled = True
        self.query_one("#resume_btn", Button).disabled = True
        self.query_one("#stop_btn", Button).disabled = False
        self.query_one("#force_stop_btn", Button).disabled = False
        self.query_one("#reset_btn", Button).disabled = True
        # Lock the parallel count for the duration of the run; it's read once
        # at launch, so editing it mid-batch would be misleading.
        self.query_one("#parallel_input", Input).disabled = True
        self.query_one("#parallel_stepper").disabled = True
        self.set_progress(0)

        self._run_batch_worker()

    def _mark_status(self, entry: SceneEntry, status: str):
        # ETA is only meaningful while RUNNING; clear on any other transition.
        entry.status = status
        if status != STATUS_RUNNING:
            entry.eta = None
            entry.eta_anchor_seconds = None
            entry.eta_anchor_at = None
        self.refresh_scene_queue()

    def _zip_worker_loop(self):
        # Tasks are (case_name, output_folder, do_zip, cleanup, do_upload).
        # Serialised so parallel cases don't thrash disk with concurrent 7z
        # runs. Uploads are handed off to the separate upload worker.
        while True:
            try:
                task = self._zip_queue.get()
            except Exception:
                return
            try:
                if task is None:
                    return
                case_name, output_folder, do_zip, cleanup, do_upload, entry = task
                self._run_zip_task(case_name, output_folder, do_zip, cleanup, do_upload, entry)
            except Exception as e:
                try:
                    self.call_from_thread(
                        self.log_line, f"Zip task crashed: {e}", "error",
                    )
                except Exception:
                    pass
            finally:
                self._zip_queue.task_done()

    def _upload_worker_loop(self):
        # Serialises uploads (one rclone at a time) while running independently
        # of the zip worker. Task is (case_name, archive, cleanup).
        while True:
            try:
                task = self._upload_queue.get()
            except Exception:
                return
            try:
                if task is None:
                    return
                case_name, archive, cleanup, entry = task
                self._run_upload_task(case_name, archive, cleanup, entry)
            except Exception as e:
                try:
                    self.call_from_thread(
                        self.log_line, f"Upload task crashed: {e}", "error",
                    )
                except Exception:
                    pass
            finally:
                self._upload_queue.task_done()

    def _run_zip_task(self, case_name: str, output_folder: Optional[str],
                      do_zip: bool, cleanup: str, do_upload: bool, entry: SceneEntry):
        sim = self.simulator
        if not do_zip:
            return
        if not output_folder:
            sim.info(
                f"No output directory detected in log for '{case_name}'; skipping zip/upload/cleanup.",
                tag="Case",
            )
            return

        def zip_on_line(line, kind):
            self.call_from_thread(self.log_line, line, kind)

        zip_holder: list = []
        self._track_aux_proc(zip_holder)
        try:
            zipped = sim.zip_case_output(
                case_name, output_folder, on_line=zip_on_line, process_holder=zip_holder,
            )
        finally:
            self._untrack_aux_proc(zip_holder)
        if zipped:
            entry.zip_done = True   # Zip column: Y -> D
            self.call_from_thread(self.refresh_scene_queue)
        archive = f"{output_folder}{sim.zip_ext}"
        # Folder removal doesn't depend on the upload - settle it now.
        sim.cleanup_folder(case_name, output_folder, cleanup, zipped)
        if not (zipped and do_upload):
            # Nothing to upload; settle the archive policy ('both' keeps it).
            sim.cleanup_archive(case_name, archive, cleanup, zipped, uploaded=False)
            return
        # Hand the upload to its own worker (async) or run it inline (sync);
        # the archive's own cleanup happens once the transfer outcome is known.
        if sim.upload_async:
            self._upload_queue.put((case_name, archive, cleanup, entry))
        else:
            self._run_upload_task(case_name, archive, cleanup, entry)

    def _run_upload_task(self, case_name: str, archive: str, cleanup: str, entry: SceneEntry):
        sim = self.simulator
        # rclone emits a progress line every --stats interval; a multi-hour
        # upload would flood the log with them. Throttle to one line per
        # upload_eta_step percent. Non-progress lines (errors, "Copied (new)",
        # the final 100%) always pass through.
        step = self.simulator.upload_eta_step
        last_bucket = [-1]

        def on_line(line, kind):
            m = _UPLOAD_PROGRESS_RE.search(line)
            if m:
                pct = int(m.group(1))
                # Throttle to one line per band below 100%, but always let the
                # 100% heartbeat through: rclone keeps emitting --stats lines
                # while Drive finalises a big upload server-side, and dropping
                # them (they share the top band) makes a live upload look frozen.
                if pct < 100:
                    bucket = pct // step
                    if bucket <= last_bucket[0]:
                        return
                    last_bucket[0] = bucket
            self.call_from_thread(self.log_line, line, kind)

        upload_holder: list = []
        self._track_aux_proc(upload_holder)
        try:
            uploaded = sim.upload_case_output(
                case_name, archive, on_line=on_line, process_holder=upload_holder,
            )
        finally:
            self._untrack_aux_proc(upload_holder)
        if uploaded:
            entry.upload_done = True   # Upl column: Y -> D
            self.call_from_thread(self.refresh_scene_queue)
        sim.cleanup_archive(case_name, archive, cleanup, zipped=True, uploaded=uploaded)

    @work(thread=True, exclusive=True)
    def _run_batch_worker(self):
        # Coordinator: dispatch up to K cases concurrently (K=1 -> sequential),
        # each on its own runner thread, then wait for them all. The live
        # cursor still re-reads len(scene_entries) so a mid-batch Add extends
        # this run.
        sim = self.simulator
        k = self._parallel_k
        sem = threading.Semaphore(k)
        runners: list[threading.Thread] = []
        runnable = sum(1 for e in self.scene_entries if e.status == STATUS_PENDING)
        total_initial = len(self.scene_entries)

        try:
            sim.info("Start processing", tag="Batch")
            sim.tg.queue_message("#Batch Batch settings:")
            sim.tg.queue_message(f"Pending cases to run: {runnable} / {total_initial}")
            sim.tg.queue_message(f"Parallel cases: {k}")
            sim.tg.queue_message(f"Distinct simulators: {len({e.exe_path for e in self.scene_entries if e.status == STATUS_PENDING})}")
            sim.tg.send_telegram_message_batch()
            self.call_from_thread(self._begin_run_header, k)

            i = 0
            while True:
                if self.stop_requested:
                    self.call_from_thread(self.log_line, "--- Batch stopped by user ---", "warning")
                    break
                if i >= len(self.scene_entries):
                    # Reached the end of the queue as it stands - but a case
                    # may still be running, and a mid-batch Add appends new
                    # pending entries past the current end. Don't finish while
                    # any dispatched runner is alive: wait briefly, then re-read
                    # len(scene_entries) so a just-added case gets dispatched
                    # below instead of stranded. Only break once the tail is
                    # reached with nothing in flight.
                    alive = [t for t in runners if t.is_alive()]
                    if alive:
                        alive[0].join(timeout=0.2)
                        continue
                    break
                entry = self.scene_entries[i]
                if entry.status != STATUS_PENDING:
                    i += 1
                    continue
                # Blocks until a slot frees, so at most K cases run at once.
                sem.acquire()
                if self.stop_requested:
                    sem.release()
                    self.call_from_thread(self.log_line, "--- Batch stopped by user ---", "warning")
                    break
                t = threading.Thread(
                    target=self._run_one_case, args=(entry, i, sem), daemon=True
                )
                t.start()
                runners.append(t)
                i += 1

            # Stop dispatching; wait for everything in flight to finish.
            for t in runners:
                t.join()

            self.call_from_thread(self.set_progress, 100)
            # Drain in-flight tasks before the Telegram summary so it lands
            # after the archive log noise. Zips first (they enqueue uploads),
            # then uploads.
            pending = self._zip_queue.unfinished_tasks + self._upload_queue.unfinished_tasks
            if pending:
                self.call_from_thread(self.set_status, f"Finishing {pending} zip / upload task(s)...")
                self._zip_queue.join()
                self._upload_queue.join()

            with self._batch_lock:
                results = list(self._batch_results)
            case_names = [r["name"] for r in results]
            time_costs = [r["cost"] for r in results]
            total_failures = sum(1 for r in results if r["failed"])
            total_warnings = sum(r["warnings"] for r in results)
            total_errors = sum(r["errors"] for r in results)
            sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)
            sim.info("All done", tag="Batch")

        finally:
            # batch_exe copies persist so START / RESUME can re-run with the
            # original snapshot; cleanup happens on Remove / Reset / unmount.
            with self._batch_lock:
                tw = sum(r["warnings"] for r in self._batch_results)
                te = sum(r["errors"] for r in self._batch_results)
                tf = sum(1 for r in self._batch_results if r["failed"])
            self.batch_running = False
            self.call_from_thread(self.reset_run_controls)
            self.call_from_thread(self.switch_tab, "setup")
            self.call_from_thread(
                self.set_status,
                f"Idle | Warnings: {tw} | Errors: {te} | Failures: {tf}",
            )

    def _begin_run_header(self, k: int):
        # Single Running-tab header for the whole batch; per-case detail lives
        # in the queue table. current_step_re is cleared because concurrent
        # cases may use different profiles - show step lines verbatim.
        self.current_step_re = None
        self.query_one("#current_case_label", Static).update(
            "Running" if k == 1 else f"Running (up to {k} parallel)"
        )
        self.query_one("#current_step_label", Static).update("Step: -")

    def _run_one_case(self, entry: SceneEntry, idx: int, sem: "threading.Semaphore"):
        # One case on its own thread. It mutates only its own entry's fields;
        # every widget update marshals onto the app via call_from_thread.
        sim = self.simulator
        case_name = sim.case_name_from_path(entry.scene_path)
        rec = {"name": case_name, "cost": -1, "warnings": 0, "errors": 0, "failed": False}
        try:
            entry.run_start = _time.time()
            entry.force_stopped = False
            entry.proc_holder = []
            entry.log_buffer = []
            entry.eta = None
            entry.eta_anchor_seconds = None
            entry.eta_anchor_at = None
            entry.zip_done = False
            entry.upload_done = False
            self.call_from_thread(self._mark_status, entry, STATUS_RUNNING)

            if not os.path.exists(entry.scene_path):
                sim.info(f"File '{entry.scene_path}' not found.", tag="Case")
                self.call_from_thread(self._mark_status, entry, STATUS_MISSING)
                rec["failed"] = True
                return

            batch_exe = entry.batch_exe_path
            if not batch_exe or not os.path.isfile(batch_exe):
                self.call_from_thread(
                    self.log_line, f"Prepared exe missing for '{case_name}': {batch_exe}", "error",
                )
                self.call_from_thread(self._mark_status, entry, STATUS_ERROR)
                rec["failed"] = True
                return

            profile = sim.identify_profile(entry.exe_path)
            eta_pat_str = profile_eta_pattern(profile)
            eta_re = re.compile(eta_pat_str) if eta_pat_str else None
            env = sim.make_env(entry.omp_threads)

            def on_line(line, kind, _entry=entry, _eta_re=eta_re, _name=case_name):
                _entry.log_buffer.append((line, kind))
                if kind == "warning":
                    _entry.warnings += 1
                    self.call_from_thread(self.refresh_scene_queue)
                elif kind == "error":
                    _entry.errors += 1
                    self.call_from_thread(self.refresh_scene_queue)
                # Prefix the combined log so interleaved case streams stay legible.
                self.call_from_thread(
                    self.log_line, f"[{_name}] {line}" if line.strip() else line, kind,
                )
                if kind == "step" and _eta_re is not None:
                    m = _eta_re.search(line)
                    if m and m.group(1) != _entry.eta:
                        # Re-anchor only when the token text changes, so a
                        # repeated estimate keeps counting down smoothly rather
                        # than snapping back to its start each step line.
                        _entry.eta = m.group(1)
                        _entry.eta_anchor_seconds = self._parse_eta_seconds(m.group(1))
                        _entry.eta_anchor_at = _time.time()
                        self.call_from_thread(self.refresh_scene_queue)

            try:
                result = sim.run_case(
                    batch_exe, entry.scene_path, idx,
                    lambda: len(self.scene_entries),
                    entry.mpi_ranks,
                    on_line=on_line, process_holder=entry.proc_holder, env=env,
                )
            except Exception as e:
                self.call_from_thread(self.log_line, f"Exception in case '{case_name}': {e}", "error")
                self.call_from_thread(self._mark_status, entry, STATUS_ERROR)
                rec["failed"] = True
                return

            entry.returncode = result.returncode
            entry.warnings = result.warnings
            entry.errors = result.errors
            rec["warnings"] = result.warnings
            rec["errors"] = result.errors

            if entry.force_stopped:
                # Force-stop -> STOPPED (removable), not FAILED.
                entry.elapsed = -1
                self.call_from_thread(self._mark_status, entry, STATUS_STOPPED)
                return

            if result.returncode == 0:
                entry.elapsed = result.elapsed
                rec["cost"] = result.elapsed
                # Async: enqueue and return; the zip-worker drains serially.
                # Sync: run inline so this runner waits for the archive.
                if sim.zip_async:
                    self._zip_queue.put((
                        case_name, result.output_folder,
                        entry.zip_output, entry.cleanup, entry.upload_output, entry,
                    ))
                else:
                    self._run_zip_task(
                        case_name, result.output_folder,
                        entry.zip_output, entry.cleanup, entry.upload_output, entry,
                    )
                self.call_from_thread(self._mark_status, entry, STATUS_DONE)
            else:
                entry.elapsed = -1
                rec["failed"] = True
                self.call_from_thread(self._mark_status, entry, STATUS_FAILED)
        finally:
            entry.run_start = None
            with self._batch_lock:
                self._batch_results.append(rec)
            sem.release()

    def _track_aux_proc(self, holder: list):
        with self._aux_lock:
            self._aux_holders[id(holder)] = holder

    def _untrack_aux_proc(self, holder: list):
        with self._aux_lock:
            self._aux_holders.pop(id(holder), None)

    def _has_active_work(self) -> bool:
        """True while a batch is running or any zip / cleanup / upload task is
        still queued or in flight - i.e. quitting now would strand work."""
        if self.batch_running:
            return True
        if self._zip_queue.unfinished_tasks or self._upload_queue.unfinished_tasks:
            return True
        with self._aux_lock:
            return bool(self._aux_holders)

    def _active_work_summary(self) -> str:
        running = sum(1 for e in self.scene_entries if e.status == STATUS_RUNNING)
        zips = self._zip_queue.unfinished_tasks
        ups = self._upload_queue.unfinished_tasks
        parts = []
        if running:
            parts.append(f"{running} simulation(s) running")
        if zips:
            parts.append(f"{zips} zip/cleanup task(s) pending")
        if ups:
            parts.append(f"{ups} upload(s) pending")
        if not parts:
            parts.append("a background task in progress")
        return "In progress: " + ", ".join(parts) + "."

    def _kill_all_processes(self):
        """Terminate every live child process - the simulators plus the 7-Zip /
        rclone procs spawned by zip & upload tasks - so none are left orphaned.
        kill_proc_tree no-ops on already-dead handles, so stale entries are safe."""
        self.stop_requested = True   # stop the coordinator dispatching new cases
        for entry in self.scene_entries:
            for proc in list(entry.proc_holder):
                kill_proc_tree(proc)
        with self._aux_lock:
            holders = list(self._aux_holders.values())
        for holder in holders:
            for proc in list(holder):
                kill_proc_tree(proc)

    async def action_quit(self):
        # ctrl+q while the dialog is already up is a no-op (don't stack it).
        if isinstance(self.screen, QuitConfirmScreen):
            return
        if self._has_active_work():
            self.push_screen(
                QuitConfirmScreen(self._active_work_summary()), self._on_quit_confirm,
            )
            return
        self.exit()

    def _on_quit_confirm(self, do_quit: Optional[bool]):
        if do_quit:
            self._kill_all_processes()
            self.exit()

    def on_unmount(self):
        # Safety net for every teardown path - including ones that bypass the
        # quit dialog (Ctrl+C, an unhandled crash, the terminal closing): kill
        # all child procs first so nothing is orphaned, then drop the exe copies
        # (now deletable, since their simulator no longer holds the file open).
        # On a confirmed quit this is a harmless second pass (kill_proc_tree
        # no-ops on dead handles).
        self._kill_all_processes()
        for entry in self.scene_entries:
            self._cleanup_entry_exe(entry)


def main():
    BatchSimuApp().run()


if __name__ == "__main__":
    main()

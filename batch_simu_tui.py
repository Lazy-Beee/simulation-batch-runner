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
)


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
    ("Simulator", 22),
    ("Scene", 26),
    ("OMP", 5),
    ("MPI", 5),
    ("Zip", 5),
    ("Rmv", 5),
    ("Status", 10),
    ("Time", 10),
    ("ETA", 8),
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


class TopBar(Horizontal):
    """Replaces the default Header. Layout: title | spacer | CPU/MEM | clock."""

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
    # ETA token extracted from the most recent step line (e.g. '7h57m',
    # '<1m'). Profile must define eta_pattern to enable extraction.
    eta: Optional[str] = None
    # Captured (line, kind) pairs for this case. The Running tab shows the
    # live unified stream; this buffer feeds the per-case tab a user can
    # pop open from the Setup queue to look at one case in isolation.
    log_buffer: list = field(default_factory=list)
    # Per-case private copy of the simulator exe, taken at the moment the
    # entry was added to the queue (see _add_from_input). Each entry owns
    # its own copy so re-compiling the source exe mid-batch only affects
    # cases added after the rebuild. None means no copy was prepared yet.
    batch_exe_path: Optional[str] = None


def format_sim_type_text(simulator: Simulator, exe_path: str) -> str:
    profile = simulator.identify_profile(exe_path)
    if profile is None:
        return "Type: unknown"
    name = profile_name(profile)
    if not profile_supports_mpi(profile):
        return f"Type: {name} (single-process only - MPI not supported)"
    return f"Type: {name}"


def format_drag_target_text(target_id: str) -> str:
    name = "Simulator" if target_id == "exe_input" else "Scene"
    return f"Drag target: {name}  (click a field to switch)"


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
    """

    BINDINGS = [
        Binding("ctrl+s", "start", "Start", priority=True),
        # ctrl+x and ctrl+w collide with Input's built-in 'cut' / 'delete-word'
        # bindings. show=False hides them from the Footer so the Footer key
        # order stays stable when an Input is focused (otherwise these
        # bindings get inserted at the Input.BINDINGS slot positions, which
        # are interleaved among Input's own keys). Stop keeps priority=True
        # so ctrl+x still fires from inside an Input; close_tab is left
        # non-priority on purpose so ctrl+w inside an Input remains
        # delete-left-word.
        Binding("ctrl+x", "stop", "Stop", priority=True, show=False),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "show_tab('setup')", "Queue"),
        Binding("f2", "show_tab('running')", "Running"),
        Binding("ctrl+w", "close_tab", "Close case tab", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.simulator = Simulator(self.config)
        self.scene_entries: list[SceneEntry] = []
        self.process_holder: list = []
        self.stop_requested = False
        self.force_stopped_current = False   # set by FORCE STOP; consumed by worker
        self.current_entry: Optional[SceneEntry] = None
        self.batch_running = False
        self.current_case_start: float | None = None
        self.current_warnings = 0
        self.current_errors = 0
        # Compiled step_pattern regex for the current case; used by log_line
        # to format the Step label and by log_lines_batch (if present).
        self.current_step_re: Optional[re.Pattern] = None
        # Tracks the most recently applied profile so we only re-snap the
        # OMP/MPI switches when the matched profile actually transitions.
        self._last_profile_name: str | None = None

        # Which Input drag-and-drop / paste content lands in. Updated whenever
        # the user focuses one of the two inputs (mouse click or Tab key).
        # We track this explicitly instead of reading App.focused at paste
        # time because OLE drag-drop is modal: switching to the file manager
        # to grab a file clears App focus, and the bracketed-paste event
        # arrives without restoring it. mouse_over is also stale at that
        # point (no MouseMove events are delivered during the drag).
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
                        yield Button("View log", id="view_log_btn")
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

    # ---------- helpers ----------

    _KIND_COLOR = {
        "error": "red",
        "warning": "yellow",
        "step": "cyan",
        "info": "blue",
    }

    @classmethod
    def _write_log_line(cls, widget: RichLog, line: str, kind: str):
        """Style-and-write a single (line, kind) pair to any RichLog without
        touching app-wide state. Used by both the Running tab log writer and
        the per-case tab replay."""
        line = line.rstrip("\n")
        color = cls._KIND_COLOR.get(kind)
        if color:
            widget.write(f"[{color}]{line}[/{color}]")
        else:
            widget.write(line)

    def log_line(self, line: str, kind: str = "raw"):
        widget = self.query_one("#log_panel", RichLog)
        self._write_log_line(widget, line, kind)
        # Side-effects only relevant to the live Running tab
        if kind == "error":
            self.current_errors += 1
            self._refresh_current_stats()
        elif kind == "warning":
            self.current_warnings += 1
            self._refresh_current_stats()
        elif kind == "step":
            self.query_one("#current_step_label", Static).update(
                f"Step: {self._format_step_text(line)}"
            )

    def _format_step_text(self, line: str) -> str:
        """Strip a step line down to the user-facing portion. If the active
        profile's step_pattern regex has a capture group, that group wins;
        otherwise the text starts at the first match position."""
        r = self.current_step_re
        if r is not None:
            m = r.search(line)
            if m:
                if m.lastindex:
                    return m.group(1).rstrip()
                return line[m.start():].rstrip()
        return line.rstrip()

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

    def _format_case_tab_header(self, entry: SceneEntry) -> tuple[str, str, str]:
        """Three-line header for the case-log tab, mirroring the Running
        tab's case / step / stats stack: case name (accent bold), exe
        basename, then a stats row that pairs with a Copy button."""
        case_line = f"Case: {self.simulator.case_name_from_path(entry.scene_path)}"
        exe_line = f"Simulator: {os.path.basename(entry.exe_path)}"
        stats_line = (
            f"Status: {status_display(entry)}"
            f"  |  Time: {self._fmt_time(entry.elapsed)}"
            f"  |  Warnings: {entry.warnings}"
            f"  |  Errors: {entry.errors}"
        )
        return case_line, exe_line, stats_line

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
                e.eta or "-",
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
        """Activate a tab by id.

        Textual's TabbedContent.show_tab() is just an unhide helper - it does
        NOT switch the active pane. The actual switch is `tc.active = tab_id`,
        which fires the internal _watch_active watcher.

        We additionally drop focus before switching: if a Button on the
        previous tab still has focus when the active pane changes, Textual
        snaps back to keep the focused widget visible (causing the "screen
        flashes but tab stays put" symptom).
        """
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

    def start_current_case(self, case_name: str, idx: int, total: int):
        self.current_case_start = _time.time()
        self.current_warnings = 0
        self.current_errors = 0
        self.query_one("#current_case_label", Static).update(f"Case: {case_name} ({idx+1}/{total})")
        self.query_one("#current_step_label", Static).update("Step: -")
        self._refresh_current_stats()

    def finish_current_case(self):
        self.current_case_start = None

    # ---------- mount ----------

    def on_mount(self):
        queue = self.query_one("#scene_queue", DataTable)
        for label, width in QUEUE_COLS:
            queue.add_column(label, width=width)
        self.apply_sim_type(self.query_one("#exe_input", Input).value)
        self.set_interval(1.0, self._refresh_current_stats)
        # Topbar clock + CPU/MEM refresh
        self._refresh_topbar()
        self.set_interval(1.0, self._refresh_topbar)

    def _refresh_topbar(self):
        try:
            clock_text = datetime.datetime.now().strftime("%H:%M:%S")
            self.query_one("#topbar_clock", Label).update(clock_text)
            stats_label = self.query_one("#topbar_stats", Label)
            if HAS_PSUTIL:
                # interval=None -> percent since the previous call; first
                # call after import returns 0, subsequent calls real %.
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

        # All entries from this one Add call share a single snapshot of the
        # simulator exe - the user can't recompile in the middle of a single
        # synchronous Add. Snapshots only diverge across separate Add calls,
        # which is enough granularity to handle "rebuild mid-batch then add
        # more cases" correctly without bloating disk for batch adds.
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
                remove_output=rm_out,
                batch_exe_path=shared_batch_exe,
            )
            self.scene_entries.append(entry)
            added += 1
            if not os.path.exists(p):
                self.log_line(f"Warning: scene file not found: {p}", "warning")

        if added == 0:
            # No entries committed (defensive - shouldn't happen, paths
            # parsing above already returns early on empty input); roll
            # back the orphan copy so it doesn't leak.
            try:
                self.simulator.cleanup_exe(shared_batch_exe)
            except Exception:
                pass
        else:
            self.refresh_scene_queue()
        inp.value = ""
        inp.focus()

    @on(Button.Pressed, "#view_log_btn")
    async def on_view_log(self):
        """Open (or switch to) a per-case tab showing that case's captured log.

        Only finished cases get a log tab. For a running case the live log is
        already on the Running tab; pending cases have nothing to show yet.
        """
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
        # If a tab for this entry already exists, just switch to it.
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
        # Replay the buffered lines into the new widget
        for line, kind in entry.log_buffer:
            self._write_log_line(log_widget, line, kind)
        if not entry.log_buffer:
            log_widget.write("[dim](no output yet)[/dim]")
        # Use switch_tab so the explicit self.refresh() force-flushes the
        # tab-bar repaint (Textual sometimes drops it right after add_pane).
        self.switch_tab(tab_id)

    async def action_close_tab(self):
        """Close the active tab unless it's setup or running (those are pinned)."""
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
        """Drop this entry's reference to its batch_exe copy, and delete
        the copy on disk only if no other entry still references it.
        Idempotent; used by on_remove / on_reset / on_unmount."""
        path = entry.batch_exe_path
        if not path:
            return
        entry.batch_exe_path = None
        # A batch-Add of N scenes shares one copy; only the last drop deletes.
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
    async def on_reset(self):
        if self.batch_running:
            self.log_line("Cannot reset while a batch is running. Stop first.", "warning")
            return
        # Best-effort cleanup of every entry's private exe copy before
        # the queue is wiped.
        for entry in self.scene_entries:
            self._cleanup_entry_exe(entry)
        # Close any per-case tabs the user had opened
        await self._close_all_case_tabs()
        # Wipe queue
        self.scene_entries.clear()
        self.refresh_scene_queue()
        self.set_status("Idle")
        self.set_progress(0)
        self.query_one("#log_panel", RichLog).clear()
        # Restore exe input to the configured default + re-apply profile
        self.query_one("#exe_input", Input).value = self.simulator.default_exe
        self.query_one("#add_file_input", Input).value = ""
        self.current_step_re = None
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

        Route to whichever of the two inputs was most recently focused (see
        on_descendant_focus). We can't use mouse position - OLE drag-drop on
        Windows is modal, so no MouseMove events arrive between leaving the
        terminal to grab a file and the drop landing back - and we can't
        trust self.focused either, because alt-tabbing out and back can
        clear it. Mirroring the last clicked/Tab-selected input is the
        sturdiest mapping under those constraints."""
        text = event.text or ""
        # Normalize: terminals often append CR/LF and may wrap with quotes
        text = text.replace("\r", "").replace("\n", " ").strip()
        if not text:
            return

        if self._paste_target_id == "exe_input":
            self.query_one("#exe_input", Input).value = strip_quotes(text)
        else:  # add_file_input - keep quotes; _add_from_input uses shlex.split
            scene_input = self.query_one("#add_file_input", Input)
            scene_input.value = text
            scene_input.focus()
        event.stop()

    def on_descendant_focus(self, event: events.DescendantFocus):
        """Track which of our two routable inputs was last focused so the next
        paste / drag-drop lands there. Triggered by both mouse click and Tab
        key navigation. Other focusable widgets (switches, buttons, the
        OMP/MPI inputs) are ignored - they don't change the paste target."""
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
            # Label may not exist yet during early mount; the initial render
            # already shows the default target.
            pass

    def action_clear_log(self):
        self.query_one("#log_panel", RichLog).clear()

    def action_copy_log(self):
        """Copy the active tab's RichLog content to the system clipboard.

        Driven by the Copy button in Running and case tabs. Textual's
        click-drag selection works for Static/Label but not for RichLog
        (mouse-down is eaten by the scrollable container), so this gives
        the user a one-shot 'copy everything in this log' path. The
        terminal's own Shift+drag selection is still the finer-grained
        alternative."""
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
        """Return the RichLog inside the currently active tab, or None for
        tabs without one (Setup)."""
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

        # Validate each pending entry still has its private batch_exe copy.
        # The source exe may have been moved or deleted since Add - we don't
        # need it anymore (the copy is the source of truth at run time) but
        # the copy itself has to exist.
        missing = [e for e in pending if not e.batch_exe_path or not os.path.isfile(e.batch_exe_path)]
        if missing:
            for e in missing:
                self.log_line(
                    f"Prepared exe missing for '{e.scene_path}': {e.batch_exe_path}",
                    "error",
                )
            return

        self.simulator.write_console = lambda msg, kind="info": self.call_from_thread(self.log_line, msg, kind)

        self.batch_running = True
        self.stop_requested = False
        self.force_stopped_current = False
        self.process_holder = []

        # Switch BEFORE disabling start_btn / resume_btn. Disabling a focused
        # button causes Textual to auto-move focus to the next focusable
        # widget (still on the Setup tab) - which then competes with our
        # tab switch and snaps the active back to Setup.
        self.switch_tab("running")
        self.query_one("#start_btn", Button).disabled = True
        self.query_one("#resume_btn", Button).disabled = True
        self.query_one("#stop_btn", Button).disabled = False
        self.query_one("#force_stop_btn", Button).disabled = False
        self.query_one("#reset_btn", Button).disabled = True
        self.set_progress(0)

        # Worker reads self.scene_entries live so cases Added mid-batch are
        # picked up too.
        self._run_batch_worker()

    def _mark_status(self, entry: SceneEntry, status: str):
        """Update the entry's status field and repaint the queue table.

        Called from the worker thread via call_from_thread. ETA is only
        meaningful while a case is actively running, so any non-RUNNING
        transition also clears the cached ETA.
        """
        entry.status = status
        if status != STATUS_RUNNING:
            entry.eta = None
        self.refresh_scene_queue()

    @work(thread=True, exclusive=True)
    def _run_batch_worker(self):
        sim = self.simulator
        case_names: list[str] = []
        time_costs: list[int] = []
        total_warnings = 0
        total_errors = 0
        total_failures = 0
        # Snapshot count for the start-of-batch Telegram digest only.
        # The actual run loop reads self.scene_entries live so mid-batch
        # Adds get processed too.
        runnable = sum(1 for e in self.scene_entries if e.status == STATUS_PENDING)
        total_initial = len(self.scene_entries)

        try:
            sim.info("Start processing", tag="Batch")
            sim.tg.queue_message("#Batch Batch settings:")
            sim.tg.queue_message(f"Pending cases to run: {runnable} / {total_initial}")
            sim.tg.queue_message(f"Distinct simulators: {len({e.exe_path for e in self.scene_entries if e.status == STATUS_PENDING})}")
            sim.tg.send_telegram_message_batch()

            # Live cursor: each iteration re-reads len(self.scene_entries),
            # so entries Added mid-batch extend this run instead of being
            # deferred to the next START / RESUME. `i` stays 0-indexed to
            # match the rest of the case-processing block below.
            i = 0
            while True:
                if self.stop_requested:
                    self.call_from_thread(self.log_line, "--- Batch stopped by user ---", "warning")
                    break
                if i >= len(self.scene_entries):
                    break
                entry = self.scene_entries[i]
                # Resume support: skip anything that's already been processed.
                if entry.status != STATUS_PENDING:
                    i += 1
                    continue
                total = len(self.scene_entries)

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
                    i += 1
                    continue

                # Per-case OMP env (None unsets)
                sim.set_omp_env(entry.omp_threads)

                # Each entry owns a private copy prepared at Add time. If
                # the copy was somehow deleted (e.g. user wiped the folder
                # between adds and runs), skip the case rather than fall
                # back to the source exe.
                batch_exe = entry.batch_exe_path
                if not batch_exe or not os.path.isfile(batch_exe):
                    self.call_from_thread(
                        self.log_line,
                        f"Prepared exe missing for '{case_name}': {batch_exe}",
                        "error",
                    )
                    entry.elapsed = -1
                    total_failures += 1
                    self.call_from_thread(self._mark_status, entry, STATUS_ERROR)
                    time_costs.append(-1)
                    self.current_entry = None
                    i += 1
                    continue

                # Compile the step / ETA regexes for this case's profile.
                profile = sim.identify_profile(entry.exe_path)
                step_pattern_str = profile_step_pattern(profile)
                self.current_step_re = re.compile(step_pattern_str) if step_pattern_str else None
                eta_pat_str = profile_eta_pattern(profile)
                eta_re = re.compile(eta_pat_str) if eta_pat_str else None

                self.process_holder.clear()

                # Reset the per-case log buffer + ETA so a re-run via START
                # or RESUME doesn't carry over the previous run's state.
                entry.log_buffer = []
                entry.eta = None

                def on_line(line, kind, _entry=entry, _eta_re=eta_re):
                    _entry.log_buffer.append((line, kind))
                    self.call_from_thread(self.log_line, line, kind)
                    if kind == "step" and _eta_re is not None:
                        m = _eta_re.search(line)
                        if m:
                            new_eta = m.group(1)
                            if new_eta != _entry.eta:
                                _entry.eta = new_eta
                                self.call_from_thread(self.refresh_scene_queue)

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
                    i += 1
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
                    i += 1
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
                i += 1

            self.call_from_thread(self.set_progress, 100)
            sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)
            sim.info("All done", tag="Batch")

        finally:
            # Per-case batch_exe copies persist past batch end so START /
            # RESUME can re-run the same entries with their original exe
            # snapshot. They get cleaned up when the entry is Removed,
            # on Reset, or on app unmount.
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
        for entry in self.scene_entries:
            self._cleanup_entry_exe(entry)


def main():
    BatchSimuApp().run()


if __name__ == "__main__":
    main()

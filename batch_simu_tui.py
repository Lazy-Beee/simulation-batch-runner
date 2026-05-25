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


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"   # scene file didn't exist when we tried to run it
STATUS_ERROR = "error"       # exception during run
STATUS_STOPPED = "stopped"   # process was force-stopped mid-run

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
    ("ETA", 10),
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
    remove_output: bool
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
        self.current_step_re: Optional[re.Pattern] = None
        # Re-snap OMP/MPI defaults only when the matched profile actually
        # changes, so manual toggles survive further typing in the exe field.
        self._last_profile_name: str | None = None
        self._current_col_widths: Optional[list[int]] = None

        # Background zip / remove queue. Each finished case enqueues a task
        # (run by _zip_worker_loop) so the next case can start while the
        # previous one is being archived. Drained at batch end.
        self._zip_queue: queue.Queue = queue.Queue()
        self._zip_worker: Optional[threading.Thread] = None

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
        r = self.current_step_re
        if r is not None:
            m = r.search(line)
            if m:
                if m.lastindex:
                    return m.group(1).rstrip()
                return line[m.start():].rstrip()
        return line.rstrip()

    def _refresh_current_stats(self):
        if self.current_case_start is not None:
            elapsed = round(_time.time() - self.current_case_start)
            self.query_one("#current_stats_label", Static).update(
                f"Elapsed: {datetime.timedelta(seconds=elapsed)} | "
                f"Warnings: {self.current_warnings} | Errors: {self.current_errors}"
            )
            entry = self.current_entry
            if entry is not None and entry.status == STATUS_RUNNING:
                entry.elapsed = elapsed
                # Match by identity, not dataclass equality - two entries with
                # identical fields would otherwise shadow each other.
                idx = next(
                    (j for j, e in enumerate(self.scene_entries) if e is entry),
                    -1,
                )
                if idx >= 0:
                    total = len(self.scene_entries)
                    case_name = self.simulator.case_name_from_path(entry.scene_path)
                    self.set_status(f"Case {idx+1}/{total}: {case_name}")
                    self.query_one("#current_case_label", Static).update(
                        f"Case: {case_name} ({idx+1}/{total})"
                    )
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
    def _fmt_time(elapsed: Optional[int]) -> str:
        # H:MM:SS left-flush; column ljust pads the trailing space. Sidesteps
        # timedelta's "X days, ..." rollover so anything < 1000h fits the cell.
        if elapsed is None or elapsed < 0:
            return "-"
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_eta(eta_token: Optional[str]) -> str:
        # Match _fmt_time's left-flush H:MM:SS format so Time and ETA line up.
        # Accepts '<1m', 'XhYm', 'Xh Ym' (CAMMP), or 'Xm' (>=60 carries).
        if not eta_token:
            return "-"
        eta_token = eta_token.strip()
        if eta_token == "<1m":
            h, mm = 0, 0
        else:
            m = re.match(r"(\d+)h\s*(\d+)m\Z", eta_token)
            if m:
                h, mm = int(m.group(1)), int(m.group(2))
            else:
                m = re.match(r"(\d+)m\Z", eta_token)
                if m:
                    h, mm = divmod(int(m.group(1)), 60)
                else:
                    return eta_token
        return f"{h}:{mm:02d}:00"

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
        prev = table.cursor_row if table.row_count else None
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
            values = [
                str(i + 1),
                self._short(e.exe_path, sim_w - 1),
                scene_disp,
                self._fmt_omp(e.omp_threads),
                self._fmt_mpi(e.mpi_ranks),
                self._fmt_bool(e.zip_output),
                self._fmt_bool(e.remove_output),
                status_display(e),
                self._fmt_time(e.elapsed),
                self._fmt_eta(e.eta),
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

    def start_current_case(self, case_name: str, idx: int, total: int):
        self.current_case_start = _time.time()
        self.current_warnings = 0
        self.current_errors = 0
        self.query_one("#current_case_label", Static).update(f"Case: {case_name} ({idx+1}/{total})")
        self.query_one("#current_step_label", Static).update("Step: -")
        self._refresh_current_stats()

    def finish_current_case(self):
        self.current_case_start = None

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
        rm_out = self.query_one("#remove_switch", Switch).value

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
                remove_output=rm_out,
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
        self.refresh_scene_queue()
        self.set_status("Idle")
        self.set_progress(0)
        self.query_one("#log_panel", RichLog).clear()
        self.query_one("#exe_input", Input).value = self.simulator.default_exe
        self.query_one("#add_file_input", Input).value = ""
        self.current_step_re = None
        self._last_profile_name = None
        self.stop_requested = False
        self.force_stopped_current = False
        self.current_entry = None
        self.apply_sim_type(self.simulator.default_exe)
        self.current_warnings = 0
        self.current_errors = 0
        self.finish_current_case()
        self.query_one("#current_case_label", Static).update("No case running")
        self.query_one("#current_step_label", Static).update("Step: -")
        self.query_one("#current_stats_label", Static).update("Elapsed: - | Warnings: 0 | Errors: 0")
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

    @staticmethod
    def _reset_entry_run_state(entry: SceneEntry):
        entry.status = STATUS_PENDING
        entry.returncode = None
        entry.elapsed = None
        entry.warnings = 0
        entry.errors = 0

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
        self.log_line("--- Stop requested: current case will finish then batch exits ---", "warning")

    def action_stop(self):
        self.on_stop()

    @on(Button.Pressed, "#force_stop_btn")
    def on_force_stop(self):
        if not self.batch_running:
            return
        self.stop_requested = True
        self.force_stopped_current = True
        for proc in self.process_holder:
            kill_proc_tree(proc)
        self.log_line("--- Force stop: terminating current case (process tree) ---", "warning")

    @on(Button.Pressed, "#resume_btn")
    def on_resume(self):
        if self.batch_running:
            return
        for entry in self.scene_entries:
            if entry.status == STATUS_STOPPED:
                self._reset_entry_run_state(entry)
        self.refresh_scene_queue()
        self._launch_batch()

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

        self.simulator.write_console = lambda msg, kind="info": self.call_from_thread(self.log_line, msg, kind)

        self.batch_running = True
        self.stop_requested = False
        self.force_stopped_current = False
        self.process_holder = []

        # Switch tab BEFORE disabling the focused button; otherwise Textual
        # auto-moves focus to the next focusable widget on Setup, which
        # competes with the tab switch and snaps the active back to Setup.
        self.switch_tab("running")
        self.query_one("#start_btn", Button).disabled = True
        self.query_one("#resume_btn", Button).disabled = True
        self.query_one("#stop_btn", Button).disabled = False
        self.query_one("#force_stop_btn", Button).disabled = False
        self.query_one("#reset_btn", Button).disabled = True
        self.set_progress(0)

        self._run_batch_worker()

    def _mark_status(self, entry: SceneEntry, status: str):
        # ETA is only meaningful while RUNNING; clear on any other transition.
        entry.status = status
        if status != STATUS_RUNNING:
            entry.eta = None
        self.refresh_scene_queue()

    def _zip_worker_loop(self):
        # Tasks are (case_name, output_folder, do_zip, do_remove). Worker
        # serialises them so we don't thrash disk with parallel 7z runs.
        while True:
            try:
                task = self._zip_queue.get()
            except Exception:
                return
            try:
                if task is None:
                    return
                case_name, output_folder, do_zip, do_remove = task
                self._run_zip_task(case_name, output_folder, do_zip, do_remove)
            except Exception as e:
                try:
                    self.call_from_thread(
                        self.log_line, f"Zip task crashed: {e}", "error",
                    )
                except Exception:
                    pass
            finally:
                self._zip_queue.task_done()

    def _run_zip_task(self, case_name: str, output_folder: Optional[str],
                      do_zip: bool, do_remove: bool):
        sim = self.simulator
        if not do_zip:
            return
        if not output_folder:
            sim.info(
                f"No output directory detected in log for '{case_name}'; skipping zip/remove.",
                tag="Case",
            )
            return

        def zip_on_line(line, kind):
            self.call_from_thread(self.log_line, line, kind)

        zipped = sim.zip_case_output(case_name, output_folder, on_line=zip_on_line)
        if do_remove:
            if zipped:
                sim.remove_case_output(case_name, output_folder)
            else:
                sim.info(f"Output removal cancelled for case '{case_name}'", tag="Case")

    @work(thread=True, exclusive=True)
    def _run_batch_worker(self):
        sim = self.simulator
        case_names: list[str] = []
        time_costs: list[int] = []
        total_warnings = 0
        total_errors = 0
        total_failures = 0
        # Start-of-batch digest only; the run loop reads scene_entries live.
        runnable = sum(1 for e in self.scene_entries if e.status == STATUS_PENDING)
        total_initial = len(self.scene_entries)

        try:
            sim.info("Start processing", tag="Batch")
            sim.tg.queue_message("#Batch Batch settings:")
            sim.tg.queue_message(f"Pending cases to run: {runnable} / {total_initial}")
            sim.tg.queue_message(f"Distinct simulators: {len({e.exe_path for e in self.scene_entries if e.status == STATUS_PENDING})}")
            sim.tg.send_telegram_message_batch()

            # Live cursor: each iteration re-reads len(self.scene_entries) so
            # entries Added mid-batch extend this run.
            i = 0
            while True:
                if self.stop_requested:
                    self.call_from_thread(self.log_line, "--- Batch stopped by user ---", "warning")
                    break
                if i >= len(self.scene_entries):
                    break
                entry = self.scene_entries[i]
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

                profile = sim.identify_profile(entry.exe_path)
                step_pattern_str = profile_step_pattern(profile)
                self.current_step_re = re.compile(step_pattern_str) if step_pattern_str else None
                eta_pat_str = profile_eta_pattern(profile)
                eta_re = re.compile(eta_pat_str) if eta_pat_str else None

                self.process_holder.clear()

                entry.log_buffer = []
                entry.eta = None

                def on_line(line, kind, _entry=entry, _eta_re=eta_re):
                    _entry.log_buffer.append((line, kind))
                    if kind == "warning":
                        _entry.warnings += 1
                        self.call_from_thread(self.refresh_scene_queue)
                    elif kind == "error":
                        _entry.errors += 1
                        self.call_from_thread(self.refresh_scene_queue)
                    self.call_from_thread(self.log_line, line, kind)
                    if kind == "step" and _eta_re is not None:
                        m = _eta_re.search(line)
                        if m:
                            new_eta = m.group(1)
                            if new_eta != _entry.eta:
                                _entry.eta = new_eta
                                self.call_from_thread(self.refresh_scene_queue)

                try:
                    # Live total so mid-case Adds bump the per-step denominator.
                    result = sim.run_case(
                        batch_exe, entry.scene_path, i,
                        lambda: len(self.scene_entries),
                        entry.mpi_ranks,
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
                    # Force-stop -> STOPPED (removable), not FAILED.
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
                    # Enqueue zip + remove so the next case can start while
                    # this one is being archived. Drained at batch end.
                    self._zip_queue.put((
                        case_name, result.output_folder,
                        entry.zip_output, entry.remove_output,
                    ))
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
                    f"Done {i+1}/{len(self.scene_entries)} | "
                    f"Warnings: {total_warnings} | Errors: {total_errors} | Failures: {total_failures}",
                )
                self.current_entry = None
                i += 1

            self.call_from_thread(self.set_progress, 100)
            # Drain any in-flight zip / remove tasks before reporting totals
            # so the Telegram batch summary lands after the archive log
            # noise rather than in the middle of it.
            pending = self._zip_queue.unfinished_tasks
            if pending:
                self.call_from_thread(
                    self.set_status,
                    f"Finishing {pending} zip / remove task(s)...",
                )
                self._zip_queue.join()
            sim.send_batch_report(case_names, time_costs, total_failures, total_errors, total_warnings)
            sim.info("All done", tag="Batch")

        finally:
            # batch_exe copies persist so START / RESUME can re-run with the
            # original snapshot; cleanup happens on Remove / Reset / unmount.
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

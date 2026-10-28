# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "textual>=8.2,<9",
#   "psutil>=5.9",
#   "certifi",
# ]
# ///
"""CLUD Installer: pick a drive, get a portable Claude Code (and/or omp) stick.

Run it through CLUD-Installer.bat (Windows) or CLUD-Installer.sh (macOS/Linux).
Without arguments it opens the TUI. For scripting:
    --list                 print the drives and exit
    --target PATH --yes    install to PATH without the TUI
    --no-rename            don't rename the drive to CLUD
    --platforms LIST       comma-separated: windows, linux-arm64, linux-x64, or linux for
                           both Linux builds (default: all)
    --agents LIST          comma-separated: claude, omp (default: claude)
    --all                  include non-removable drives
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

import clud_core as core

BANNER = """\
 ██████╗██╗     ██╗   ██╗██████╗
██╔════╝██║     ██║   ██║██╔══██╗
██║     ██║     ██║   ██║██║  ██║
██║     ██║     ██║   ██║██║  ██║
╚██████╗███████╗╚██████╔╝██████╔╝
 ╚═════╝╚══════╝ ╚═════╝ ╚═════╝ """

ACCENT = "#D97757"


def files_text(done: int, total: int) -> str:
    return f"{done:,} / {total:,} files copied, {total - done:,} left"


# The install screen: a monitor showing the log, with the stick plugged into its side.
# Data pulses run down the cable while files go to the stick.
STICK = (
    "╭──────────────╮",
    "┤   CLUD   @   │",
    "╰──────────────╯",
)
CABLE = 6
SIMPLE_GLYPHS = str.maketrans("╭╮╰╯┌┐└┘├┤─│■•", "++++++++++-|#>")


def draw_rig(lines: list[str], width: int, height: int, writing: bool, tick: int,
             label: str = "", simple: bool = False, ready: bool = False):
    """The monitor-and-stick picture as Rich Text, sized to width x height cells."""
    from rich.text import Text

    stick_w = len(STICK[0])
    mon_w = max(20, min(width - CABLE - stick_w, 80))
    inner = mon_w - 8
    rows = max(3, height - 4)
    shown = [line[:inner - 1] + "…" if len(line) > inner else line for line in lines[-rows:]]
    shown = [""] * (rows - len(shown)) + shown
    mid = 2 + rows // 2  # the row the cable leaves the monitor at
    stick_top = mid - 1
    frame, dim, accent = "grey50", "grey35", ACCENT

    out = Text()
    for y in range(rows + 4):
        row = Text()
        if y == 0:
            row.append("╭" + "─" * (mon_w - 2) + "╮", frame)
        elif y == 1:
            row.append("│ ┌" + "─" * (mon_w - 6) + "┐ │", frame)
        elif y < rows + 2:
            i = y - 2
            row.append("│ │ ", frame)
            row.append(shown[i].ljust(inner), "bold" if i == rows - 1 else "")
            row.append(" │ " + ("├" if y == mid else "│"), frame)
        elif y == rows + 2:
            row.append("│ └" + "─" * (mon_w - 6) + "┘ │", frame)
        else:
            row.append("╰" + "─" * (mon_w - 2) + "╯", frame)
        row.pad_right(mon_w - row.cell_len)

        if y == mid:
            for x in range(CABLE):
                if writing and (x - tick) % 3 == 0:
                    row.append("•", accent)
                else:
                    row.append("─", frame)
        elif y == stick_top + len(STICK):
            row.append(" " * CABLE + label[:stick_w].center(stick_w), "dim")
        else:
            row.append(" " * CABLE)
        k = y - stick_top
        if 0 <= k < len(STICK):
            part = STICK[k]
            if "@" in part:
                left, right = part.split("@")
                name = left.index("CLUD")
                row.append(left[:name], frame)
                row.append("CLUD", f"bold {accent}")
                row.append(left[name + 4:], frame)
                # The stick's LED: blinks orange while writing, steady green once done
                if ready:
                    led = "green"
                else:
                    led = accent if writing and tick % 2 else dim
                row.append("■", led)
                row.append(right, frame)
            else:
                row.append(part, frame)
        if simple:
            row.plain = row.plain.translate(SIMPLE_GLYPHS)
        out.append(row)
        if y < rows + 3:
            out.append("\n")
    return out


# --- CLI mode ---------------------------------------------------------------------------

class PrintReporter(core.Reporter):
    def __init__(self) -> None:
        self._last = 0.0
        self._files = ""

    def step(self, index: int, total: int, title: str) -> None:
        self._files = ""  # install() logs the step title itself; just drop the last file count

    def progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        if total and (now - self._last > 1 or done >= total):
            self._last = now
            print(f"    {done * 100 // total:3d}%  {core.human(done)} / {core.human(total)}{self._files}",
                  flush=True)

    def files(self, done: int, total: int) -> None:
        self._files = f", {files_text(done, total)}"

    def log(self, message: str) -> None:
        print(message, flush=True)


def print_drives(show_all: bool) -> None:
    drives = core.list_drives(show_all)
    if not drives:
        print("No removable drives found. Use --all to list every drive.")
    for d in drives:
        kind = "system" if d.system else ("removable" if d.removable else "fixed")
        clud = (f"  CLUD {d.clud_version or '?'} ({', '.join(d.clud_platforms)}; {', '.join(d.clud_agents)})"
                if d.has_clud else "")
        print(f"{str(d.mount):<24} {d.label:<16} {d.fstype:<6} {core.human(d.total):>9} total  "
              f"{core.human(d.free):>9} free  {kind}{clud}")


def run_cli(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if not target.is_dir():
        print(f"{target} is not a folder or mounted drive")
        return 1
    if not args.yes:
        print("Add --yes to confirm installing to", target)
        return 1
    platforms = []
    for name in args.platforms.split(","):
        name = name.strip().lower()
        keys = [k for k in core.PLATFORM_KEYS if k.startswith("linux")] if name == "linux" else [name]
        if not all(k in core.PLATFORM_KEYS for k in keys):
            print(f"Unknown platform {name!r}. Choose from: {', '.join(core.PLATFORM_KEYS)}, linux")
            return 1
        platforms += keys
    agents = [name.strip().lower() for name in args.agents.split(",")]
    for name in agents:
        if name not in core.AGENT_KEYS:
            print(f"Unknown agent {name!r}. Choose from: {', '.join(core.AGENT_KEYS)}")
            return 1
    print("Looking up the latest versions...")
    src = core.resolve_sources()
    omp = f"omp {src.omp_version}" if src.omp_version else f"omp unavailable ({src.omp_error})"
    print(f"Claude Code {src.claude_version}, {omp}, Python {src.python_version}")
    print("Platforms:", ", ".join(k for k in core.PLATFORM_KEYS if k in platforms))
    print("Agents:", ", ".join(k for k in core.AGENT_KEYS if k in agents))
    notes = core.install(target, src, rename=not args.no_rename, report=PrintReporter(), platforms=platforms,
                         agents=agents)
    for note in notes:
        print("Note:", note)
    return 0


# --- TUI --------------------------------------------------------------------------------

def legacy_console() -> bool:
    """True in the classic Windows console window. Its usual fonts (Lucida Console, raster)
    lack the block and line characters Textual draws borders, bars and scrollbars with,
    which then show up as boxes. CLUD_SIMPLE_GLYPHS=1/0 forces it either way."""
    forced = os.environ.get("CLUD_SIMPLE_GLYPHS")
    if forced is not None:
        return forced == "1"
    if os.name != "nt":
        return False
    modern = ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI", "WEZTERM_PANE", "ALACRITTY_WINDOW_ID")
    return not any(name in os.environ for name in modern)


# Drawn instead of Textual's default look when legacy_console(): no eighth-block borders
SIMPLE_CSS = """
Button, Button:focus, Button:hover, Button.-active, Button:disabled {
    border: none !important; height: 1; min-width: 12; margin-right: 2;
}
Checkbox, Checkbox:focus, Checkbox:hover { border: none !important; padding: 0 1; }
Checkbox:focus { text-style: reverse; }
FooterKey.-command-palette { border-left: outer $foreground 20%; }
#bar { margin-bottom: 0; }
#quit { margin-top: 2; }
"""


def run_tui(show_all: bool) -> int:
    from textual import work
    from textual.app import App, ComposeResult
    from rich.text import Text
    from textual.binding import Binding
    from textual.content import Content
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widget import Widget
    from textual.widgets import Button, Checkbox, DataTable, Footer, Header, ProgressBar, Static

    simple = legacy_console()
    if simple:
        from textual.renderables.bar import Bar as BarRenderable
        from textual.scrollbar import ScrollBarRender

        BarRenderable.BAR = BarRenderable.HALF_BAR_LEFT = BarRenderable.HALF_BAR_RIGHT = "─"
        ScrollBarRender.VERTICAL_BARS = [" "] * 8
        ScrollBarRender.HORIZONTAL_BARS = [" "] * 8
    header_icon = "C" if simple else None

    class DriveScreen(Screen):
        BINDINGS = [
            Binding("enter", "choose", "Install here", show=True, priority=True, key_display="Enter"),
            Binding("a", "toggle_all", "Show all drives"),
            Binding("r", "refresh", "Refresh"),
            Binding("q", "app.quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.show_all = show_all
            self.drives: dict[str, core.Drive] = {}

        def compose(self) -> ComposeResult:
            yield Header(icon=header_icon)
            yield Static(f"[{ACCENT}]{BANNER}[/]", id="banner")
            yield Static("Portable Claude Code for Windows PCs and Linux, Raspberry Pi included. "
                         "Pick the drive to install it on.",
                         id="tagline")
            yield DataTable(id="drives", cursor_type="row", zebra_stripes=True)
            yield Static("", id="hint")
            yield Footer()

        def on_mount(self) -> None:
            self.action_refresh()

        def action_refresh(self) -> None:
            table = self.query_one(DataTable)
            # Rebuild the columns too, so their widths fit the new rows
            table.clear(columns=True)
            table.add_columns("Drive", "Name", "Format", "Size", "Free", "Type", "CLUD")
            self.drives = {}
            for d in core.list_drives(self.show_all):
                key = str(d.mount)
                self.drives[key] = d
                kind = "[red]system[/]" if d.system else ("USB / card" if d.removable else "fixed")
                if d.has_clud:
                    clud = f"[{ACCENT}]v{d.clud_version}[/]" if d.clud_version else f"[{ACCENT}]yes[/]"
                else:
                    clud = "[dim]-[/]"
                free = core.human(d.free)
                if d.free < core.NEEDED_BYTES:
                    free = f"[red]{free}[/]"
                table.add_row(Text(key), Text(d.label) if d.label else Text("(no name)", style="dim"), d.fstype, core.human(d.total), free, kind,
                              clud, key=key)
            hint = self.query_one("#hint", Static)
            if not self.drives:
                hint.update("No removable drives found. Plug one in and press [b]r[/], "
                            "or press [b]a[/] to show every drive.")
            else:
                hint.update(f"Needs about {core.human(core.NEEDED_BYTES)} free for every platform. "
                            "Drives that already have CLUD get updated; your login and settings are kept.")
            table.focus()

        def action_toggle_all(self) -> None:
            self.show_all = not self.show_all
            self.action_refresh()

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self.choose(event.row_key.value)

        def action_choose(self) -> None:
            table = self.query_one(DataTable)
            if table.row_count:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                self.choose(key)

        def choose(self, key: str) -> None:
            d = self.drives.get(key)
            if d is None:
                return
            if d.system:
                self.notify("That's the drive your system runs from. Pick a USB drive or card.",
                            severity="error")
                return
            smallest = min(core.needed_bytes([p.key], [a]) for p in core.PLATFORMS for a in core.AGENT_KEYS)
            if d.free < smallest:
                self.notify(f"Not enough space: needs at least {core.human(smallest)} free.",
                            severity="error")
                return
            self.app.push_screen(ConfirmScreen(d))

    class ConfirmScreen(Screen):
        BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

        def __init__(self, drive: core.Drive) -> None:
            super().__init__()
            self.drive = drive
            self.sources: core.Sources | None = None

        def compose(self) -> ComposeResult:
            d = self.drive
            action = "Update CLUD on" if d.has_clud else "Install CLUD on"
            yield Header(icon=header_icon)
            with VerticalScroll(id="confirm-box"):
                yield Static(Content.from_markup("[b]$action $mount[/b]  $label", action=action, mount=str(d.mount),
                                                 label=d.label), id="title")
                yield Static(f"{d.fstype} · {core.human(d.total)} · {core.human(d.free)} free", classes="muted")
                yield Static("Looking up the latest versions…", id="versions")
                yield Static(
                    "• Other files on the drive are left alone.\n"
                    "• Your CLUD login, settings, history and work\\ folder are kept.\n"
                    "• CLUD runs with [b]--dangerously-skip-permissions[/b] (omp: [b]--auto-approve[/b]): "
                    "on whatever PC it's plugged into, it acts without asking.",
                    id="notes",
                )
                yield Static("Platforms to put on the stick:", classes="muted")
                for p in core.PLATFORMS:
                    yield Checkbox(p.title, value=True, id=f"plat-{p.key}", classes="platform")
                yield Static("Agents (with both, CLUD asks which to start):", classes="muted pick")
                # Updates keep the stick's agents unless changed here
                agents = d.clud_agents if d.has_clud else core.DEFAULT_AGENTS
                for a in core.AGENTS:
                    yield Checkbox(a.title, value=a.key in agents, id=f"agent-{a.key}", classes="platform")
                yield Static("", id="space")
                rename_ok = sys.platform != "linux"
                yield Checkbox(f"Rename the drive to {core.LABEL}", value=rename_ok and d.label != core.LABEL,
                               disabled=not rename_ok, id="rename")
                with Horizontal(id="buttons"):
                    yield Button("Install", variant="primary", id="go", disabled=True)
                    yield Button("Back", id="back")
            yield Footer()

        def on_mount(self) -> None:
            self.update_space()
            self.lookup()

        def platforms(self) -> list[str]:
            return [p.key for p in core.PLATFORMS if self.query_one(f"#plat-{p.key}", Checkbox).value]

        def agents(self) -> list[str]:
            return [a.key for a in core.AGENTS if self.query_one(f"#agent-{a.key}", Checkbox).value]

        def update_space(self) -> None:
            need = core.needed_bytes(self.platforms(), self.agents())
            # An update frees the old version's space too, but only after building the new one
            ok = self.drive.free >= need
            text = f"Needs about {core.human(need)} free; the drive has {core.human(self.drive.free)}."
            self.query_one("#space", Static).update(text if ok else f"[red]{text}[/]")
            self.query_one("#go", Button).disabled = not (ok and self.sources and need and self.agents())

        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            if event.checkbox.has_class("platform"):
                self.update_space()

        @work(thread=True, exclusive=True)
        def lookup(self) -> None:
            try:
                src = core.resolve_sources()
            except Exception as e:  # network down, GitHub rate limit, ...
                self.app.call_from_thread(self.lookup_failed, str(e))
                return
            self.app.call_from_thread(self.lookup_done, src)

        def lookup_done(self, src: core.Sources) -> None:
            self.sources = src
            versions = Content.from_markup(
                "Claude Code [b]$claude[/b] · omp [b]$omp[/b] · Python [b]$python[/b] + computer use",
                claude=src.claude_version, omp=src.omp_version or "-", python=src.python_version)
            if not src.omp_urls:
                # omp can't be installed this time; keep the rest going
                box = self.query_one("#agent-omp", Checkbox)
                box.value, box.disabled = False, True
                versions += Content.from_markup("\n[yellow]Couldn't look up omp:[/] $error", error=src.omp_error)
            self.query_one("#versions", Static).update(versions)
            self.update_space()
            button = self.query_one("#go", Button)
            if not button.disabled:
                button.focus()

        def lookup_failed(self, error: str) -> None:
            self.query_one("#versions", Static).update(Content.from_markup(
                "[red]Couldn't look up versions:[/] $error\nCheck your internet connection and go back to retry.",
                error=error))

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "back":
                self.app.pop_screen()
            elif event.button.id == "go" and self.sources:
                rename = self.query_one("#rename", Checkbox).value
                self.app.switch_screen(InstallScreen(self.drive, self.sources, rename, self.platforms(),
                                                     self.agents()))

    class TuiReporter(core.Reporter):
        def __init__(self, screen: "InstallScreen") -> None:
            self.screen = screen
            self._last = 0.0

        def step(self, index: int, total: int, title: str) -> None:
            self.screen.app.call_from_thread(self.screen.set_step, index, total, title)

        def progress(self, done: int, total: int) -> None:
            now = time.monotonic()
            if now - self._last > 0.1 or (total and done >= total):
                self._last = now
                self.screen.app.call_from_thread(self.screen.set_progress, done, total)

        def files(self, done: int, total: int) -> None:
            self.screen.app.call_from_thread(self.screen.set_files, done, total)

        def log(self, message: str) -> None:
            self.screen.app.call_from_thread(self.screen.write_log, message)

        def writing(self, on_stick: bool) -> None:
            self.screen.app.call_from_thread(self.screen.set_writing, on_stick)

    class Rig(Widget):
        """The monitor showing the log, with the stick plugged into it."""

        def __init__(self, label: str, **kwargs) -> None:
            super().__init__(**kwargs)
            self.label = label
            self.lines: list[str] = []
            self.writing = False
            self.ready = False
            self.tick = 0

        def on_mount(self) -> None:
            self.set_interval(0.15, self.animate)

        def animate(self) -> None:
            if self.writing:
                self.tick -= 1
                self.refresh()

        def write(self, message: str) -> None:
            self.lines += str(message).splitlines() or [""]
            del self.lines[:-200]
            self.refresh()

        def render(self):
            return draw_rig(self.lines, self.size.width, self.size.height, self.writing, self.tick,
                            self.label, simple, self.ready)

    class InstallScreen(Screen):
        BINDINGS = [Binding("q", "quit_if_done", "Quit")]

        def __init__(self, drive: core.Drive, sources: core.Sources, rename: bool, platforms: list[str],
                     agents: list[str]) -> None:
            super().__init__()
            self.drive, self.sources, self.rename = drive, sources, rename
            self.platforms, self.agents = platforms, agents
            self.finished = False

        def compose(self) -> ComposeResult:
            yield Header(icon=header_icon)
            with Vertical(id="install-box"):
                yield Rig(str(self.drive.mount), id="rig")
                with Horizontal(id="progress-row"):
                    with Vertical(id="progress"):
                        yield Static("", id="result")
                        yield Static("Starting…", id="step")
                        yield Static("", id="detail", classes="muted")
                        yield ProgressBar(id="bar", show_eta=False)
                    yield Button("Quit", id="quit", disabled=True)
            yield Footer()

        def on_mount(self) -> None:
            self.detail = ""
            self.index, self.count = 0, len(core.steps(self.platforms, self.agents))
            self.query_one("#bar", ProgressBar).update(total=self.count * 1000, progress=0)
            self.run_install()

        def show_detail(self, text: str) -> None:
            self.detail = text
            self.query_one("#detail", Static).update(text)

        def set_step(self, index: int, total: int, title: str) -> None:
            self.index, self.count = index, total
            self.query_one("#bar", ProgressBar).update(total=total * 1000, progress=index * 1000)
            self.query_one("#step", Static).update(
                Content.from_markup("[b]$step[/b]", step=f"{title} ({index + 1}/{total})"))
            self.show_detail("")

        def set_files(self, done: int, total: int) -> None:
            self.show_detail(files_text(done, total))

        def set_progress(self, done: int, total: int) -> None:
            part = min(done / total, 1) if total else 0
            self.query_one("#bar", ProgressBar).update(progress=(self.index + part) * 1000)
            if total > 1 and not self.detail.endswith("left"):
                self.show_detail(f"{core.human(done)} / {core.human(total)}")

        def set_writing(self, on_stick: bool) -> None:
            self.query_one(Rig).writing = on_stick
            self.query_one(Rig).refresh()

        def write_log(self, message: str) -> None:
            self.query_one(Rig).write(message)

        @work(thread=True, exclusive=True)
        def run_install(self) -> None:
            try:
                notes = core.install(self.drive.mount, self.sources, self.rename, TuiReporter(self),
                                     platforms=self.platforms, agents=self.agents)
            except Exception as e:
                self.app.call_from_thread(self.done, False, [str(e)], traceback.format_exc())
                return
            self.app.call_from_thread(self.done, True, notes, "")

        def done(self, ok: bool, notes: list[str], trace: str) -> None:
            self.finished = True
            self.set_writing(False)
            result = self.query_one("#result", Static)
            step = self.query_one("#step", Static)
            self.show_detail("")
            if ok:
                self.query_one("#bar", ProgressBar).update(progress=self.count * 1000)
                step.update(Content.from_markup(f"[b {ACCENT}]CLUD is ready.[/]"))
                self.query_one(Rig).ready = True
                self.query_one(Rig).refresh()
                if notes:
                    result.display = True
                    result.update(Content("\n").join(
                        Content.from_markup("[yellow]•[/] $note", note=note) for note in notes))
            else:
                self.write_log(trace)
                step.update(Content.from_markup("[red b]Install failed[/]"))
                result.display = True
                result.update(Content.from_markup(
                    "$error\nRun the installer again to retry; downloads are cached so it picks up quickly.",
                    error=notes[0]))
            button = self.query_one("#quit", Button)
            button.disabled = False
            button.focus()

        def action_quit_if_done(self) -> None:
            if self.finished:
                self.app.exit()
            else:
                self.notify("Still installing. Quitting now would leave the drive half-done.",
                            severity="warning")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "quit":
                self.app.exit()

    class CludInstaller(App):
        TITLE = "CLUD Installer"
        SUB_TITLE = "Claude + Chud = CLUD"
        CSS = f"""
        Screen {{ align-horizontal: center; }}
        #banner {{ color: {ACCENT}; text-style: bold; width: auto; margin: 1 0 0 0; }}
        #tagline {{ width: auto; margin: 0 0 1 0; color: $text-muted; }}
        #drives {{ height: auto; max-height: 14; width: 100%; max-width: 110; }}
        #hint {{ margin: 1 0; color: $text-muted; width: 100%; max-width: 110; }}
        #confirm-box, #install-box {{ width: 100%; max-width: 100; padding: 1 2; height: 1fr; }}
        #title {{ margin-bottom: 1; }}
        .muted {{ color: $text-muted; }}
        #versions {{ margin: 1 0; }}
        #space {{ margin: 0 0 1 0; }}
        .pick {{ margin-top: 1; }}
        Checkbox.platform {{ border: none; height: 1; padding: 0 1; background: transparent; }}
        Checkbox.platform:focus {{ text-style: reverse; }}
        #notes {{ margin: 0 0 1 0; }}
        #buttons {{ height: auto; margin-top: 1; }}
        #buttons Button {{ margin-right: 2; }}
        #rig {{ height: 1fr; max-height: 22; min-height: 10; }}
        #bar {{ width: 100%; }}
        #bar Bar {{ width: 1fr; }}
        #progress-row {{ dock: bottom; height: auto; }}
        #quit {{ margin-top: 1; }}
        #progress {{ width: 1fr; height: auto; margin-right: 2; }}
        #result {{ height: auto; margin-bottom: 1; display: none; }}
        #step, #detail {{ height: 1; text-wrap: nowrap; text-overflow: ellipsis; }}
        #bar {{ margin-bottom: 1; }}
        ProgressBar Bar > .bar--bar, ProgressBar Bar > .bar--complete {{ color: {ACCENT}; }}
        """ + (SIMPLE_CSS if simple else "")

        def on_mount(self) -> None:
            self.push_screen(DriveScreen())

    CludInstaller().run()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="CLUD Installer", description="Install CLUD (portable Claude Code) on a drive.")
    p.add_argument("--list", action="store_true", help="print the drives and exit")
    p.add_argument("--target", help="install to this drive or folder without the TUI")
    p.add_argument("--yes", action="store_true", help="confirm --target")
    p.add_argument("--no-rename", action="store_true", help=f"don't rename the drive to {core.LABEL}")
    p.add_argument("--all", action="store_true", help="include non-removable drives")
    p.add_argument("--platforms", default=",".join(core.PLATFORM_KEYS),
                   help="comma-separated: windows, linux-arm64, linux-x64, or linux (default: all)")
    p.add_argument("--agents", default=",".join(core.DEFAULT_AGENTS),
                   help="comma-separated: claude, omp (default: claude)")
    args = p.parse_args()
    if args.list:
        print_drives(args.all)
        return 0
    if args.target:
        return run_cli(args)
    return run_tui(args.all)


if __name__ == "__main__":
    sys.exit(main())

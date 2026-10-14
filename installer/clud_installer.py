# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "textual>=8.2,<9",
#   "psutil>=5.9",
#   "certifi",
# ]
# ///
"""CLUD Installer: pick a drive, get a portable Claude Code stick.

Run it through CLUD-Installer.bat (Windows) or CLUD-Installer.sh (macOS/Linux).
Without arguments it opens the TUI. For scripting:
    --list                 print the drives and exit
    --target PATH --yes    install to PATH without the TUI
    --no-rename            don't rename the drive to CLUD
    --all                  include non-removable drives
"""

from __future__ import annotations

import argparse
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


# --- CLI mode ---------------------------------------------------------------------------

class PrintReporter(core.Reporter):
    def __init__(self) -> None:
        self._last = 0.0

    def step(self, index: int, total: int, title: str) -> None:
        pass  # install() logs the step title itself

    def progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        if total and (now - self._last > 1 or done >= total):
            self._last = now
            print(f"    {done * 100 // total:3d}%  {core.human(done)} / {core.human(total)}", flush=True)

    def log(self, message: str) -> None:
        print(message, flush=True)


def print_drives(show_all: bool) -> None:
    drives = core.list_drives(show_all)
    if not drives:
        print("No removable drives found. Use --all to list every drive.")
    for d in drives:
        kind = "system" if d.system else ("removable" if d.removable else "fixed")
        clud = f"  CLUD {d.clud_version or '?'}" if d.has_clud else ""
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
    print("Looking up the latest versions...")
    src = core.resolve_sources()
    print(f"Claude Code {src.claude_version}, Git {src.git_version}, Python {src.python_version}")
    notes = core.install(target, src, rename=not args.no_rename, report=PrintReporter())
    for note in notes:
        print("Note:", note)
    return 0


# --- TUI --------------------------------------------------------------------------------

def run_tui(show_all: bool) -> int:
    from textual import work
    from textual.app import App, ComposeResult
    from rich.text import Text
    from textual.binding import Binding
    from textual.content import Content
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Button, Checkbox, DataTable, Footer, Header, ProgressBar, RichLog, Static

    class DriveScreen(Screen):
        BINDINGS = [
            Binding("enter", "choose", "Install here", show=True, priority=True),
            Binding("a", "toggle_all", "Show all drives"),
            Binding("r", "refresh", "Refresh"),
            Binding("q", "app.quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.show_all = show_all
            self.drives: dict[str, core.Drive] = {}

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static(f"[{ACCENT}]{BANNER}[/]", id="banner")
            yield Static("Portable Claude Code for any Windows PC. Pick the drive to install it on.",
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
                hint.update(f"Needs about {core.human(core.NEEDED_BYTES)} free. "
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
            if d.free < core.NEEDED_BYTES:
                self.notify(f"Not enough space: needs about {core.human(core.NEEDED_BYTES)} free.",
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
            yield Header()
            with Vertical(id="confirm-box"):
                yield Static(Content.from_markup("[b]$action $mount[/b]  $label", action=action, mount=str(d.mount),
                                                 label=d.label), id="title")
                yield Static(f"{d.fstype} · {core.human(d.total)} · {core.human(d.free)} free", classes="muted")
                yield Static("Looking up the latest versions…", id="versions")
                yield Static(
                    "• Other files on the drive are left alone.\n"
                    "• Your CLUD login, settings, history and work\\ folder are kept.\n"
                    "• CLUD runs with [b]--dangerously-skip-permissions[/b]: on whatever PC it's plugged into, "
                    "it acts without asking.",
                    id="notes",
                )
                rename_ok = sys.platform != "linux"
                yield Checkbox(f"Rename the drive to {core.LABEL}", value=rename_ok and d.label != core.LABEL,
                               disabled=not rename_ok, id="rename")
                with Horizontal(id="buttons"):
                    yield Button("Install", variant="primary", id="go", disabled=True)
                    yield Button("Back", id="back")
            yield Footer()

        def on_mount(self) -> None:
            self.lookup()

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
            self.query_one("#versions", Static).update(
                f"Claude Code [b]{src.claude_version}[/b] · Git for Windows [b]{src.git_version}[/b] · "
                f"Python [b]{src.python_version}[/b] + Windows-MCP"
            )
            button = self.query_one("#go", Button)
            button.disabled = False
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
                self.app.switch_screen(InstallScreen(self.drive, self.sources, rename))

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

        def log(self, message: str) -> None:
            self.screen.app.call_from_thread(self.screen.write_log, message)

    class InstallScreen(Screen):
        BINDINGS = [Binding("q", "quit_if_done", "Quit")]

        def __init__(self, drive: core.Drive, sources: core.Sources, rename: bool) -> None:
            super().__init__()
            self.drive, self.sources, self.rename = drive, sources, rename
            self.finished = False

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(id="install-box"):
                yield Static(Content.from_markup("Installing CLUD on [b]$mount[/b]", mount=str(self.drive.mount)),
                             id="title")
                yield Static("Overall", classes="muted")
                yield ProgressBar(id="overall", show_eta=False)
                yield Static("Starting…", id="step")
                yield ProgressBar(id="current", show_eta=False)
                yield RichLog(id="log", wrap=True, markup=False)
                yield Static("", id="result")
                with Horizontal(id="buttons"):
                    yield Button("Quit", id="quit", disabled=True)
            yield Footer()

        def on_mount(self) -> None:
            self.run_install()

        def set_step(self, index: int, total: int, title: str) -> None:
            self.query_one("#overall", ProgressBar).update(total=total, progress=index)
            self.query_one("#step", Static).update(f"[b]{title}[/b]")
            self.query_one("#current", ProgressBar).update(total=None, progress=0)

        def write_log(self, message: str) -> None:
            self.query_one(RichLog).write(message)

        def set_progress(self, done: int, total: int) -> None:
            bar = self.query_one("#current", ProgressBar)
            bar.update(total=total or None, progress=done)

        @work(thread=True, exclusive=True)
        def run_install(self) -> None:
            try:
                notes = core.install(self.drive.mount, self.sources, self.rename, TuiReporter(self))
            except Exception as e:
                self.app.call_from_thread(self.done, False, [str(e)], traceback.format_exc())
                return
            self.app.call_from_thread(self.done, True, notes, "")

        def done(self, ok: bool, notes: list[str], trace: str) -> None:
            self.finished = True
            log = self.query_one(RichLog)
            result = self.query_one("#result", Static)
            if ok:
                self.query_one("#overall", ProgressBar).update(progress=len(core.STEPS))
                text = (f"[b {ACCENT}]CLUD is ready.[/]  Eject the drive safely, plug it into any Windows PC "
                        "and double-click [b]CLUD.exe[/b].")
                content = Content.from_markup(text)
                for note in notes:
                    content += Content.from_markup("\n[yellow]•[/] $note", note=note)
                result.update(content)
            else:
                log.write(Text(trace, style="red"))
                result.update(Content.from_markup(
                    "[red b]Install failed:[/] $error\nRun the installer again to retry; "
                    "downloads are cached so it picks up quickly.", error=notes[0]))
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
        SUB_TITLE = "portable Claude Code"
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
        #notes {{ margin: 0 0 1 0; }}
        #buttons {{ height: auto; margin-top: 1; }}
        #buttons Button {{ margin-right: 2; }}
        #overall, #current {{ margin-bottom: 1; }}
        #log {{ height: 1fr; border: round $panel; }}
        #result {{ margin-top: 1; }}
        ProgressBar Bar > .bar--bar, ProgressBar Bar > .bar--complete {{ color: {ACCENT}; }}
        """

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
    args = p.parse_args()
    if args.list:
        print_drives(args.all)
        return 0
    if args.target:
        return run_cli(args)
    return run_tui(args.all)


if __name__ == "__main__":
    sys.exit(main())

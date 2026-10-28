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
#log { border: solid $panel; }
FooterKey.-command-palette { border-left: outer $foreground 20%; }
"""


def run_tui(show_all: bool) -> int:
    from textual import work
    from textual.app import App, ComposeResult
    from rich.text import Text
    from textual.binding import Binding
    from textual.content import Content
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Button, Checkbox, DataTable, Footer, Header, ProgressBar, RichLog, Static

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
                yield Static(Content.from_markup("Installing CLUD on [b]$mount[/b]", mount=str(self.drive.mount)),
                             id="title")
                yield Static("Overall", classes="muted")
                yield ProgressBar(id="overall", show_eta=False)
                yield Static("Starting…", id="step")
                yield ProgressBar(id="current", show_eta=False)
                yield Static("", id="files", classes="muted")
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
            self.query_one("#files", Static).update("")

        def set_files(self, done: int, total: int) -> None:
            self.query_one("#files", Static).update(files_text(done, total))

        def write_log(self, message: str) -> None:
            self.query_one(RichLog).write(message)

        def set_progress(self, done: int, total: int) -> None:
            bar = self.query_one("#current", ProgressBar)
            bar.update(total=total or None, progress=done)

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
            log = self.query_one(RichLog)
            result = self.query_one("#result", Static)
            if ok:
                self.query_one("#overall", ProgressBar).update(
                    progress=len(core.steps(self.platforms, self.agents)))
                how = []
                if "windows" in self.platforms:
                    how.append("on Windows, double-click [b]CLUD.exe[/b]")
                if any(p.startswith("linux") for p in self.platforms):
                    how.append("on Linux or a Raspberry Pi, open [b]CLUD.desktop[/b] (or run [b]sh CLUD.sh[/b])")
                text = (f"[b {ACCENT}]CLUD is ready.[/]  Eject the drive safely and plug it in: "
                        + "; ".join(how) + ".")
                if len(self.agents) > 1:
                    text += " It asks whether to start Claude Code or omp."
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
        #overall, #files {{ margin-bottom: 1; }}
        #log {{ height: 1fr; border: round $panel; }}
        #result {{ margin-top: 1; }}
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

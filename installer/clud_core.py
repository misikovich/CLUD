"""CLUD install logic: finding drives and building a CLUD stick on one. No UI in here.

Everything that lands on the stick is Windows software, but it is fetched as plain
archives so the stick can be built from Windows, macOS or Linux alike:
  - Claude Code: the native win32-x64 binary straight from the npm registry
  - Git for Windows: the full .tar.bz2 build (provides git + the bash.exe Claude needs)
  - Python: python-build-standalone 3.13 for Windows, relocatable, no venv
  - Windows-MCP: pip-installed into that Python with uv, targeting the Windows platform
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import certifi
import psutil

REPO = Path(__file__).resolve().parent.parent
STICK_FILES = REPO / "stick"
CACHE = REPO / ".cache"
LABEL = "CLUD"

# Rough size of an install, used to refuse drives that are too small. Updates need it
# too: the new version is built next to the old one before swapping in.
NEEDED_BYTES = 900 * 1024**2

# Folders the installer owns and replaces wholesale on update. "node" and ".npm-cache"
# are from the old npm-based layout and are removed when found.
MANAGED_DIRS = ("app", "git", "py")
LEGACY_DIRS = ("node", ".npm-cache")
STAGING = ".clud-new"
RETIRED = ".clud-old"
HIDDEN = ("app", "git", "py", "mcp", "CLUD.cmd", "autorun.inf", "clud.ico", "clud-version.json")

# Parts of Git for Windows that Claude never uses: docs, Vim's runtime, timezone data and
# Tcl/Tk for the gitk / git gui GUIs. Every file written to a stick costs ~20-40 ms (more
# on macOS), so these ~6,500 small files are minutes of install time. Newer builds use
# ucrt64\ where older ones had mingw64\.
GIT_SKIP = ("usr/share/doc/", "usr/share/man/", "usr/share/info/", "usr/share/gtk-doc/",
            "usr/share/vim/") + tuple(
    f"{prefix}/{sub}" for prefix in ("mingw64", "ucrt64")
    for sub in ("share/doc/", "share/gtk-doc/", "doc/", "share/zoneinfo/", "lib/tcl8.6/",
                "lib/tk8.6/", "share/git-gui/"))

# Same for Python: Tcl/Tk for tkinter, IDLE, demos, and C headers (~1,650 files)
PY_SKIP = ("tcl/", "include/", "Lib/idlelib/", "Lib/turtledemo/")

_SSL = ssl.create_default_context(cafile=certifi.where())


class Reporter:
    """Progress sink. The TUI and the plain CLI each provide their own."""

    def step(self, index: int, total: int, title: str) -> None: ...
    def progress(self, done: int, total: int) -> None: ...
    def log(self, message: str) -> None: ...


# --- Drives ---------------------------------------------------------------------------

@dataclass
class Drive:
    mount: Path
    label: str
    fstype: str
    total: int
    free: int
    removable: bool
    system: bool
    clud_version: str | None  # None: no CLUD on it; "" when the version is unknown

    @property
    def has_clud(self) -> bool:
        return self.clud_version is not None


def _windows_label(mount: str) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(mount, buf, 261, None, None, None, None, 0)
    return buf.value if ok else ""


def _linux_removable(device: str) -> bool:
    # /dev/sdb1 -> /sys/block/sdb/removable ; /dev/mmcblk0p1 -> mmcblk0
    name = os.path.basename(device)
    for base in (re.sub(r"p?\d+$", "", name), name):
        flag = Path("/sys/block") / base / "removable"
        if flag.exists():
            return flag.read_text().strip() == "1"
    return False


def _mac_external(device: str) -> bool:
    # Anything under /Volumes counts on a Mac, including the internal Recovery volume and
    # mounted .dmg files, so ask diskutil whether it's really a separate physical drive
    try:
        r = subprocess.run(["diskutil", "info", "-plist", device], capture_output=True, timeout=10)
        info = plistlib.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return not info.get("Internal", True) and info.get("BusProtocol") != "Disk Image"


def _clud_version(mount: Path) -> str | None:
    if not (mount / "CLUD.cmd").exists():
        return None
    try:
        return json.loads((mount / "clud-version.json").read_text()).get("claude", "")
    except (OSError, ValueError):
        return ""


def list_drives(show_all: bool = False) -> list[Drive]:
    """Mounted drives, removable ones only unless show_all. System drives are flagged."""
    drives: list[Drive] = []
    system_root = os.environ.get("SystemDrive", "C:").upper() + "\\" if os.name == "nt" else "/"
    for part in psutil.disk_partitions(all=False):
        opts = part.opts.split(",")
        mount = part.mountpoint
        if os.name == "nt":
            if "cdrom" in opts or not part.fstype:
                continue
            removable = "removable" in opts
            system = mount.upper() == system_root
            label = _windows_label(mount)
        elif sys.platform == "darwin":
            if mount.startswith("/System/Volumes/") or mount.startswith("/private/"):
                continue
            system = os.path.realpath(mount) == "/"
            removable = mount.startswith("/Volumes/") and not system and _mac_external(part.device)
            label = os.path.basename(mount) or mount
        else:
            if part.fstype in ("squashfs", "overlay", "tmpfs") or mount.startswith(("/snap/", "/boot")):
                continue
            system = mount in ("/", "/home", "/usr", "/var")
            removable = not system and (
                mount.startswith(("/media/", "/run/media/", "/mnt/")) or _linux_removable(part.device)
            )
            label = os.path.basename(mount) or mount
        if not (removable or show_all):
            continue
        try:
            usage = psutil.disk_usage(mount)
        except OSError:
            continue  # card reader with no card, permission denied, ...
        path = Path(mount)
        drives.append(Drive(path, label, part.fstype, usage.total, usage.free, removable, system,
                            _clud_version(path)))
    return drives


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --- Sources --------------------------------------------------------------------------

@dataclass
class Sources:
    claude_version: str
    claude_url: str
    git_version: str
    git_url: str
    python_version: str
    python_url: str


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "clud-installer", "Accept": "application/json"})
    with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
        return json.load(r)


def _github_asset(repo: str, pattern: str) -> tuple[str, str]:
    rel = _get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    for asset in rel["assets"]:
        if re.fullmatch(pattern, asset["name"]):
            return asset["name"], asset["browser_download_url"]
    raise RuntimeError(f"No asset matching {pattern} in the latest {repo} release")


def resolve_sources() -> Sources:
    """Look up the latest version of each component (3 small web requests)."""
    claude = _get_json("https://registry.npmjs.org/@anthropic-ai/claude-code/latest")["version"]
    win = _get_json(f"https://registry.npmjs.org/@anthropic-ai/claude-code-win32-x64/{claude}")
    git_name, git_url = _github_asset("git-for-windows/git", r"Git-[\d.]+-64-bit\.tar\.bz2")
    py_name, py_url = _github_asset(
        "astral-sh/python-build-standalone",
        r"cpython-3\.13\.\d+\+\d+-x86_64-pc-windows-msvc-install_only_stripped\.tar\.gz",
    )
    return Sources(
        claude_version=claude,
        claude_url=win["dist"]["tarball"],
        git_version=re.search(r"Git-([\d.]+)-64-bit", git_name).group(1),
        git_url=git_url,
        python_version=re.search(r"cpython-([\d.]+)\+", py_name).group(1),
        python_url=py_url,
    )


# --- Helpers --------------------------------------------------------------------------

def _download(url: str, report: Reporter) -> Path:
    """Download into the cache (once) and return the local path."""
    CACHE.mkdir(exist_ok=True)
    dest = CACHE / url.rsplit("/", 1)[-1].replace("%2B", "+")
    if dest.exists():
        report.log(f"Using cached {dest.name}")
        return dest
    report.log(f"Downloading {dest.name}")
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "clud-installer"})
    with urllib.request.urlopen(req, context=_SSL, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            report.progress(done, total)
    part.replace(dest)
    return dest


class _CountingReader:
    """File wrapper that reports how far into a compressed archive we've read."""

    def __init__(self, path: Path, report: Reporter):
        self._f = open(path, "rb")
        self._total = path.stat().st_size
        self._report = report
        self._last = 0.0

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        now = time.monotonic()
        if now - self._last > 0.1 or not data:
            self._last = now
            self._report.progress(self._f.tell(), self._total)
        return data

    def close(self) -> None:
        self._f.close()


def _extract_tar(archive: Path, dest: Path, report: Reporter, strip: int = 0,
                 skip: tuple[str, ...] = (), only: str | None = None) -> None:
    """Stream-extract a tar archive onto any filesystem, FAT included.

    Links are materialised as copies afterwards (FAT can't hold symlinks), and
    entries or links that would escape dest are dropped.
    """
    links: list[tuple[Path, str, bool]] = []
    reader = _CountingReader(archive, report)
    try:
        with tarfile.open(fileobj=reader, mode="r|*") as tf:
            for m in tf:
                parts = PurePosixPath(m.name).parts[strip:]
                if not parts or ".." in parts or parts[0].startswith("/"):
                    continue
                rel = "/".join(parts)
                if only is not None and rel != only:
                    continue
                if any(rel.startswith(s) or rel + "/" == s for s in skip):
                    continue
                out = dest.joinpath(*parts)
                if m.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                elif m.isfile():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(m) as src, open(out, "wb") as f:
                        shutil.copyfileobj(src, f, 1 << 20)
                elif m.issym() or m.islnk():
                    links.append((Path(*parts), m.linkname, m.issym()))
    finally:
        reader.close()
    for rel, target, symbolic in links:
        if symbolic:
            src = (dest / rel).parent / target
        else:
            src = dest.joinpath(*PurePosixPath(target).parts[strip:])
        out = dest / rel
        # Git for Windows has absolute links like dev/fd -> /proc/self/fd that MSYS emulates
        # itself; followed here they'd copy from the host's own /proc on Linux
        if PurePosixPath(target).is_absolute() or not src.resolve().is_relative_to(dest.resolve()):
            continue
        if src.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out)
        elif src.is_dir():
            shutil.copytree(src, out, dirs_exist_ok=True)


def _unhide(path: Path) -> None:
    # Windows refuses to overwrite a hidden file opened for writing
    if os.name == "nt" and path.exists():
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and attrs & 0x2:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs & ~0x2)


def _hide(path: Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | 0x2)
    elif sys.platform == "darwin":
        # On FAT/exFAT, macOS maps this flag to the DOS "hidden" attribute
        try:
            os.chflags(path, os.stat(path).st_flags | stat.UF_HIDDEN)
        except OSError:
            pass


def _rmtree(path: Path) -> None:
    def force(func, p, exc):
        # Read-only files, and files briefly locked by antivirus scanning fresh .exe/.pyd
        for _ in range(10):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
                return
            except PermissionError:
                time.sleep(0.3)
        raise exc

    if path.exists():
        shutil.rmtree(path, onexc=force)


def running_from(target: Path) -> list[str]:
    """Names of processes whose executable lives on target (CLUD, its MCP server, ...)."""
    root = os.path.normcase(os.path.realpath(target)).rstrip("\\/") + os.sep
    drive = os.path.splitdrive(root)[0]
    names = set()
    for p in psutil.process_iter(["exe", "name"]):
        exe = p.info.get("exe")
        if not exe or os.path.splitdrive(os.path.normcase(exe))[0] != drive:
            continue
        # Windows reports paths with any mix of 8.3 short and long names; realpath
        # settles on the long one
        if os.path.normcase(os.path.realpath(exe)).startswith(root):
            names.add(p.info.get("name") or os.path.basename(exe))
    return sorted(names)


def _remove_appledouble(target: Path) -> None:
    """Delete the ._ files macOS left next to everything CLUD wrote on FAT/exFAT.

    Tens of thousands of them, each taking a whole cluster. Only CLUD's own files are
    touched, not the rest of the drive. Hidden flags are stored natively and survive.
    """
    def remove(dot: Path) -> None:
        if dot.with_name(dot.name[2:]).exists():
            try:
                dot.unlink()
            except OSError:
                pass

    owned = {p.name for p in STICK_FILES.iterdir()}
    owned.update(MANAGED_DIRS, ("work", "clud-version.json"))
    for name in owned:
        remove(target / f"._{name}")
        if name != "work" and (target / name).is_dir():
            for root, dirs, files in os.walk(target / name):
                for f in files:
                    if f.startswith("._"):
                        remove(Path(root) / f)


def _copy_file(src: Path, dst: Path) -> None:
    _unhide(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _uv() -> str:
    uv = os.environ.get("CLUD_UV") or shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found. Start the installer with CLUD-Installer.bat / CLUD-Installer.sh")
    return uv


def _rename(mount: Path, report: Reporter) -> str | None:
    """Set the volume label. Returns a note for the user when it couldn't be done."""
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "label", str(mount).rstrip("\\"), LABEL], capture_output=True, text=True)
        return None if r.returncode == 0 else f"Couldn't rename the drive: {r.stderr.strip() or r.stdout.strip()}"
    if sys.platform == "darwin":
        r = subprocess.run(["diskutil", "rename", str(mount), LABEL], capture_output=True, text=True)
        return None if r.returncode == 0 else f"Couldn't rename the drive: {r.stderr.strip() or r.stdout.strip()}"
    return f"Rename the drive to {LABEL} yourself (needs root on Linux, e.g. fatlabel / exfatlabel)."


# --- Install --------------------------------------------------------------------------

STEPS = (
    "Downloading Claude Code",
    "Downloading Git for Windows",
    "Downloading Python",
    "Installing Claude Code",
    "Installing Git for Windows",
    "Installing Python",
    "Installing Windows-MCP (computer use)",
    "Switching to the new version",
    "Writing launcher and settings",
    "Finishing up",
)


def install(target: Path, src: Sources, rename: bool, report: Reporter) -> list[str]:
    """Build or update a CLUD stick at target. Returns notes worth showing the user.

    Your login, settings, history and anything in work\\ are left alone.
    """
    notes: list[str] = []
    busy = running_from(target)
    if busy:
        raise RuntimeError(f"CLUD is running from this drive ({', '.join(busy)}). "
                           "Close it, then try again.")
    n = len(STEPS)
    step = iter(range(n))

    def next_step() -> None:
        i = next(step)
        report.step(i, n, STEPS[i])
        report.log(f"[{i + 1}/{n}] {STEPS[i]}")

    next_step()
    claude_tgz = _download(src.claude_url, report)
    next_step()
    git_tar = _download(src.git_url, report)
    next_step()
    py_tar = _download(src.python_url, report)

    # Build the new app/git/py side by side in a staging folder on the drive and only
    # swap them in once all three are complete, so a failure (or a yanked stick) mid-way
    # leaves the previous CLUD working.
    stage = target / STAGING
    _rmtree(stage)
    stage.mkdir()

    next_step()
    _extract_tar(claude_tgz, stage / "app", report, strip=1, only="claude.exe")
    if not (stage / "app" / "claude.exe").exists():
        raise RuntimeError("claude.exe was not in the Claude Code package")

    next_step()
    _extract_tar(git_tar, stage / "git", report, skip=GIT_SKIP)
    if not (stage / "git" / "bin" / "bash.exe").exists():
        raise RuntimeError("bash.exe missing after extracting Git for Windows")

    next_step()
    _extract_tar(py_tar, stage / "py", report, strip=1, skip=PY_SKIP)

    next_step()
    report.progress(0, 0)
    site = stage / "py" / "Lib" / "site-packages"
    # Install into a local folder, then copy over: on FAT/exFAT macOS litters the target
    # with ._ AppleDouble files mid-install, which uv then rejects as not in the wheel.
    # --link-mode copy: uv would otherwise hardlink from its cache when the target is on the
    # same disk, sharing files (like psutil's .pyd) with this very process and making them
    # undeletable on the next update
    with tempfile.TemporaryDirectory(prefix="clud-mcp-") as tmp:
        cmd = [_uv(), "pip", "install", "--quiet", "--link-mode", "copy", "--target", tmp,
               "--python-platform", "x86_64-pc-windows-msvc", "--python-version", "3.13", "windows-mcp"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Installing Windows-MCP failed:\n{r.stderr.strip()}")
        # Test suites and type stubs are ~1,200 files that are never loaded at runtime
        shutil.copytree(tmp, site, copy_function=shutil.copyfile, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("tests", "*.pyi"))
    report.log("Windows-MCP installed")

    next_step()
    # Move the old folders aside first: unlike deleting, a move fails cleanly (before
    # touching anything) when a file inside is in use, and can then be undone.
    for leftover in target.glob(RETIRED + "*"):
        try:
            _rmtree(leftover)  # from an earlier update whose cleanup was blocked
        except OSError:
            pass
    old = target / f"{RETIRED}-{time.strftime('%Y%m%d-%H%M%S')}"
    old.mkdir()
    moved: list[str] = []
    try:
        for d in MANAGED_DIRS:
            if (target / d).exists():
                _unhide(target / d)
                (target / d).rename(old / d)
                moved.append(d)
    except OSError as e:
        for d in moved:
            (old / d).rename(target / d)
            _hide(target / d)
        raise RuntimeError("Something on the drive is in use, probably CLUD itself. Close it and try "
                           "again. Nothing was changed.") from e
    for d in MANAGED_DIRS:
        (stage / d).rename(target / d)
    _rmtree(stage)
    try:
        _rmtree(old)
    except OSError:
        notes.append(f"Couldn't delete the previous version in {old}. It's safe to delete by hand.")
    for d in LEGACY_DIRS:
        if (target / d).exists() and (target / "CLUD.cmd").exists():
            report.log(f"Removing old {d}\\ (no longer used)")
            _rmtree(target / d)

    next_step()
    for path in sorted(STICK_FILES.rglob("*")):
        rel = path.relative_to(STICK_FILES)
        dst = target / rel
        if path.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif rel == Path("config/CLAUDE.md") and dst.exists() and dst.read_bytes() != path.read_bytes():
            # Keep a copy of a CLAUDE.md the user may have edited
            backup = dst.with_name("CLAUDE.md.bak")
            _copy_file(dst, backup)
            _copy_file(path, dst)
            notes.append(f"CLAUDE.md was updated; your previous one is in {backup}")
        else:
            _copy_file(path, dst)
    (target / "work").mkdir(exist_ok=True)
    _register_mcp(target / "config" / ".claude.json")
    version_file = target / "clud-version.json"
    _unhide(version_file)
    version_file.write_text(json.dumps({
        "claude": src.claude_version, "git": src.git_version, "python": src.python_version,
        "installed": time.strftime("%Y-%m-%d"),
    }, indent=2))

    next_step()
    for name in HIDDEN:
        _hide(target / name)
    if sys.platform == "darwin":
        _remove_appledouble(target)
    if rename:
        note = _rename(target, report)
        if note:
            notes.append(note)
    report.progress(1, 1)
    report.log("Done")
    return notes


def _register_mcp(cfg: Path) -> None:
    """Add the windows-mcp server to Claude Code's user config, keeping everything else."""
    server = {"type": "stdio", "command": "cmd", "args": ["/c", "windows-mcp.cmd"], "env": {}}
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    if data.get("mcpServers", {}).get("windows-mcp") == server:
        return
    data.setdefault("mcpServers", {})["windows-mcp"] = server
    _unhide(cfg)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")

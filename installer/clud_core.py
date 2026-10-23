"""CLUD install logic: finding drives and building a CLUD stick on one. No UI in here.

The stick carries a build for each platform picked (see PLATFORMS). Everything is fetched
as plain archives, so the stick can be built from Windows, macOS or Linux alike:
  - Claude Code: the native binary for each platform straight from the npm registry
  - Windows: Git for Windows, the full .tar.bz2 build (git + the bash.exe Claude needs)
  - Python: python-build-standalone 3.13 per platform, relocatable, no venv
  - Computer use: Windows-MCP on Windows; on Linux CLUD's own mcp/clud_desktop.py, whose
    dependencies (Pillow, python-xlib) go into that platform's Python. Installed with uv,
    targeting the stick's platform rather than the one the installer runs on.

Linux binaries are named *.exe on purpose: Linux mounts FAT32 sticks with "showexec",
which only lets files ending in .exe/.com/.bat run.
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
import threading
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import certifi
import psutil

REPO = Path(__file__).resolve().parent.parent
STICK_FILES = REPO / "stick"
CACHE = REPO / ".cache"
LABEL = "CLUD"


@dataclass(frozen=True)
class Platform:
    key: str
    title: str
    dirs: tuple[str, ...]  # top-level folders on the stick it owns
    size: int  # rough install size, used to refuse drives that are too small


PLATFORMS = (
    Platform("windows", "Windows (64-bit PC)", ("app", "git", "py"), 900 * 1024**2),
    Platform("linux-arm64", "Linux ARM64 (Raspberry Pi 3/4/5 with 64-bit Pi OS)", ("linux-arm64",),
             340 * 1024**2),
    Platform("linux-x64", "Linux x64 (64-bit PC: Ubuntu, Debian, Mint, Fedora, ...)", ("linux-x64",),
             350 * 1024**2),
)
PLATFORM_KEYS = tuple(p.key for p in PLATFORMS)


def needed_bytes(platforms) -> int:
    """Free space an install needs. Updates need it too: the new version is built next to
    the old one before swapping in."""
    return sum(p.size for p in PLATFORMS if p.key in platforms)


# Everything, the default
NEEDED_BYTES = needed_bytes(PLATFORM_KEYS)

# Folders the installer owns and replaces wholesale on update. "node" and ".npm-cache"
# are from the old npm-based layout and are removed when found.
MANAGED_DIRS = tuple(d for p in PLATFORMS for d in p.dirs)
LEGACY_DIRS = ("node", ".npm-cache")
STAGING = ".clud-new"
RETIRED = ".clud-old"
# Hidden on Windows and macOS (Linux file managers ignore the FAT hidden flag)
HIDDEN = MANAGED_DIRS + ("mcp", "CLUD.cmd", "CLUD.sh", "autorun.inf", "clud.ico", "clud-version.json")

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
# Linux Python: also terminfo (~2,900 files), pip, and every bin/ entry but the interpreter
# itself (the rest are symlinks, which FAT would turn into 22 MB copies)
PY_LINUX_SKIP = ("include/", "share/", "lib/tcl", "lib/tk", "lib/itcl", "lib/thread", "lib/libtcl",
                 "lib/libpython", "lib/pkgconfig/", "lib/python3.13/idlelib/", "lib/python3.13/turtledemo/",
                 "lib/python3.13/tkinter/", "lib/python3.13/ensurepip/", "lib/python3.13/site-packages/pip",
                 "lib/python3.13/config-3.13", "bin/python3-", "bin/python3.13-", "bin/pip", "bin/idle",
                 "bin/pydoc", "bin/python3", "bin/python")
LINUX_PY_BIN = "bin/python3.13"

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
    clud_platforms: tuple[str, ...] = ()

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


def _clud_info(mount: Path) -> tuple[str | None, tuple[str, ...]]:
    """(Claude Code version, platforms) of the CLUD on a drive; version None if there's none."""
    if not (mount / "CLUD.cmd").exists():
        return None, ()
    try:
        info = json.loads((mount / "clud-version.json").read_text())
    except (OSError, ValueError):
        info = {}
    # Sticks from before Linux support only had Windows
    return info.get("claude", ""), tuple(info.get("platforms", ["windows"]))


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
                            *_clud_info(path)))
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
    claude_urls: dict[str, str]  # platform key -> npm tarball
    git_version: str
    git_url: str
    python_version: str
    python_urls: dict[str, str]  # platform key -> python-build-standalone archive


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


# npm package suffix and python-build-standalone target per platform
NPM_PLATFORM = {"windows": "win32-x64", "linux-arm64": "linux-arm64", "linux-x64": "linux-x64"}
PY_TARGET = {"windows": "x86_64-pc-windows-msvc", "linux-arm64": "aarch64-unknown-linux-gnu",
             "linux-x64": "x86_64-unknown-linux-gnu"}


def resolve_sources() -> Sources:
    """Look up the latest version of each component (a handful of small web requests)."""
    claude = _get_json("https://registry.npmjs.org/@anthropic-ai/claude-code/latest")["version"]
    claude_urls = {
        key: _get_json(f"https://registry.npmjs.org/@anthropic-ai/claude-code-{npm}/{claude}")["dist"]["tarball"]
        for key, npm in NPM_PLATFORM.items()
    }
    git_name, git_url = _github_asset("git-for-windows/git", r"Git-[\d.]+-64-bit\.tar\.bz2")
    rel = _get_json("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest")
    python_urls, py_version = {}, ""
    for key, target in PY_TARGET.items():
        pattern = rf"cpython-(3\.13\.\d+)\+\d+-{re.escape(target)}-install_only_stripped\.tar\.gz"
        for asset in rel["assets"]:
            m = re.fullmatch(pattern, asset["name"])
            if m:
                python_urls[key], py_version = asset["browser_download_url"], m.group(1)
                break
        else:
            raise RuntimeError(f"No Python 3.13 for {target} in the latest python-build-standalone release")
    return Sources(
        claude_version=claude,
        claude_urls=claude_urls,
        git_version=re.search(r"Git-([\d.]+)-64-bit", git_name).group(1),
        git_url=git_url,
        python_version=py_version,
        python_urls=python_urls,
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


class _ParallelWriter:
    """Writes files from a pool of threads.

    Linux mounts FAT sticks with "flush", so closing a file waits for it to reach the
    stick: one writer manages ~9 small files/s, 32 in parallel ~250. Big files are written
    inline and the bytes held in memory are capped.
    """

    INLINE = 8 << 20
    MAX_PENDING = 64 << 20

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(32)
        self._pending = 0
        self._cond = threading.Condition()
        self._futures: list[Future] = []

    def write(self, out: Path, data: bytes) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._pending + len(data) <= self.MAX_PENDING)
            self._pending += len(data)
        self._futures.append(self._pool.submit(self._write, out, data))

    def _write(self, out: Path, data: bytes) -> None:
        try:
            with open(out, "wb") as f:
                f.write(data)
        finally:
            with self._cond:
                self._pending -= len(data)
                self._cond.notify_all()

    def copy(self, src: str, dst: str) -> None:
        if os.path.getsize(src) > self.INLINE:
            shutil.copyfile(src, dst)
        else:
            self.write(Path(dst), Path(src).read_bytes())

    def wait(self) -> None:
        """Block until everything is written; re-raises the first failure."""
        self._pool.shutdown(wait=True)
        for f in self._futures:
            f.result()

    def __enter__(self) -> "_ParallelWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.wait()


def _extract_tar(archive: Path, dest: Path, report: Reporter, strip: int = 0,
                 skip: tuple[str, ...] = (), only: str | None = None, keep: tuple[str, ...] = ()) -> None:
    """Stream-extract a tar archive onto any filesystem, FAT included.

    Links are materialised as copies afterwards (FAT can't hold symlinks), and
    entries or links that would escape dest are dropped.
    """
    links: list[tuple[Path, str, bool]] = []
    reader = _CountingReader(archive, report)
    try:
        with _ParallelWriter() as writer, tarfile.open(fileobj=reader, mode="r|*") as tf:
            for m in tf:
                parts = PurePosixPath(m.name).parts[strip:]
                if not parts or ".." in parts or parts[0].startswith("/"):
                    continue
                rel = "/".join(parts)
                if only is not None and rel != only:
                    continue
                if rel not in keep and any(rel.startswith(s) or rel + "/" == s for s in skip):
                    continue
                out = dest.joinpath(*parts)
                if m.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                elif m.isfile():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(m) as src:
                        if m.size > writer.INLINE:
                            with open(out, "wb") as f:
                                shutil.copyfileobj(src, f, 1 << 20)
                        else:
                            writer.write(out, src.read())
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


def _make_executable(path: Path) -> None:
    # Only matters when installing to a folder on a Linux/macOS disk; FAT and exFAT
    # sticks get their permissions from how they're mounted
    if os.name != "nt":
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


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

def steps(platforms) -> list[str]:
    """Step titles of an install of these platforms, in order."""
    out = []
    for p in PLATFORMS:
        if p.key in platforms:
            out.append(f"Downloading for {p.title.split(' (')[0]}")
    for p in PLATFORMS:
        if p.key == "windows" and p.key in platforms:
            out += ["Installing Claude Code for Windows", "Installing Git for Windows",
                    "Installing Python for Windows", "Installing Windows-MCP (computer use)"]
        elif p.key in platforms:
            name = p.title.split(" (")[0]
            out += [f"Installing Claude Code for {name}", f"Installing Python + computer use for {name}"]
    return out + ["Switching to the new version", "Writing launchers and settings", "Finishing up"]


def _pip_install(packages: list[str], target: Path, py_target: str, what: str) -> None:
    """pip-install packages for the stick's platform (not this computer's) into target."""
    # Install into a local folder, then copy over: on FAT/exFAT macOS litters the target
    # with ._ AppleDouble files mid-install, which uv then rejects as not in the wheel.
    # --link-mode copy: uv would otherwise hardlink from its cache when the target is on the
    # same disk, sharing files (like psutil's .pyd) with this very process and making them
    # undeletable on the next update
    with tempfile.TemporaryDirectory(prefix="clud-pip-") as tmp:
        cmd = [_uv(), "pip", "install", "--quiet", "--link-mode", "copy", "--target", tmp,
               "--python-platform", py_target, "--python-version", "3.13", *packages]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Installing {what} failed:\n{r.stderr.strip()}")
        # Test suites and type stubs are ~1,200 files that are never loaded at runtime
        with _ParallelWriter() as writer:
            shutil.copytree(tmp, target, copy_function=writer.copy, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("tests", "*.pyi", "__pycache__"))


def install(target: Path, src: Sources, rename: bool, report: Reporter,
            platforms=PLATFORM_KEYS) -> list[str]:
    """Build or update a CLUD stick at target. Returns notes worth showing the user.

    Your login, settings, history and anything in work\\ are left alone. Platforms not
    picked are removed from the stick.
    """
    platforms = [p.key for p in PLATFORMS if p.key in platforms]
    if not platforms:
        raise RuntimeError("Pick at least one platform")
    notes: list[str] = []
    busy = running_from(target)
    if busy:
        raise RuntimeError(f"CLUD is running from this drive ({', '.join(busy)}). "
                           "Close it, then try again.")
    titles = steps(platforms)
    n = len(titles)
    step = iter(range(n))

    def next_step() -> None:
        i = next(step)
        report.step(i, n, titles[i])
        report.log(f"[{i + 1}/{n}] {titles[i]}")

    archives: dict[str, dict[str, Path]] = {}
    for key in platforms:
        next_step()
        archives[key] = {"claude": _download(src.claude_urls[key], report),
                         "python": _download(src.python_urls[key], report)}
        if key == "windows":
            archives[key]["git"] = _download(src.git_url, report)

    # Build the new folders side by side in a staging folder on the drive and only swap
    # them in once all are complete, so a failure (or a yanked stick) mid-way leaves the
    # previous CLUD working.
    stage = target / STAGING
    _rmtree(stage)
    stage.mkdir()

    for key in platforms:
        a = archives[key]
        if key == "windows":
            next_step()
            _extract_tar(a["claude"], stage / "app", report, strip=1, only="claude.exe")
            if not (stage / "app" / "claude.exe").exists():
                raise RuntimeError("claude.exe was not in the Claude Code package")

            next_step()
            _extract_tar(a["git"], stage / "git", report, skip=GIT_SKIP)
            if not (stage / "git" / "bin" / "bash.exe").exists():
                raise RuntimeError("bash.exe missing after extracting Git for Windows")

            next_step()
            _extract_tar(a["python"], stage / "py", report, strip=1, skip=PY_SKIP)

            next_step()
            report.progress(0, 0)
            _pip_install(["windows-mcp"], stage / "py" / "Lib" / "site-packages", PY_TARGET[key],
                         "Windows-MCP")
            report.log("Windows-MCP installed")
        else:
            out = stage / key
            next_step()
            _extract_tar(a["claude"], out, report, strip=1, only="claude")
            if not (out / "claude").exists():
                raise RuntimeError(f"claude was not in the Claude Code package for {key}")
            (out / "claude").rename(out / "claude.exe")
            _make_executable(out / "claude.exe")

            next_step()
            _extract_tar(a["python"], out / "py", report, strip=1,
                         skip=PY_LINUX_SKIP, keep=(LINUX_PY_BIN,))
            (out / "py" / LINUX_PY_BIN).rename(out / "py" / "bin" / "python3.exe")
            _make_executable(out / "py" / "bin" / "python3.exe")
            report.progress(0, 0)
            _pip_install(["pillow", "python-xlib"], out / "py" / "lib" / "python3.13" / "site-packages",
                         PY_TARGET[key], "computer use for Linux")
            report.log(f"Python + computer use for {key} installed")

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
        if (stage / d).exists():
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
        if "__pycache__" in rel.parts:
            continue
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
    _unregister_old_mcp(target / "config" / ".claude.json")
    version_file = target / "clud-version.json"
    _unhide(version_file)
    version_file.write_text(json.dumps({
        "claude": src.claude_version, "git": src.git_version if "windows" in platforms else None,
        "python": src.python_version, "platforms": platforms, "installed": time.strftime("%Y-%m-%d"),
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


# Older sticks registered Windows-MCP in config\.claude.json. The launchers now pass their
# platform's computer-use server with --mcp-config instead, so Linux doesn't try to start
# the Windows one.
OLD_WINDOWS_MCP = {"type": "stdio", "command": "cmd", "args": ["/c", "windows-mcp.cmd"], "env": {}}


def _unregister_old_mcp(cfg: Path) -> None:
    """Remove the old windows-mcp entry from Claude Code's user config, keeping everything else."""
    if not cfg.exists():
        return
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except ValueError:
        return
    servers = data.get("mcpServers") or {}
    if servers.get("windows-mcp") != OLD_WINDOWS_MCP:
        return
    del servers["windows-mcp"]
    _unhide(cfg)
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")

"""CLUD install logic: finding drives and building a CLUD stick on one. No UI in here.

The stick carries a build for each platform picked (see PLATFORMS), with the agents picked
(see AGENTS): Claude Code, omp, or both. Everything is fetched as plain archives, so the
stick can be built from Windows, macOS or Linux alike:
  - Claude Code: the native binary for each platform straight from the npm registry
  - omp (oh-my-pi): its standalone binary for each platform from its GitHub releases. It
    unpacks a native library (~60-110 MB) next to itself on first start; the launchers
    point it at the stick (PI_NATIVES_DIR) so that happens once, not on every host.
  - Python: python-build-standalone 3.13 per platform, relocatable, no venv. Its standard
    library and the pure-Python packages are packed into zips it imports from (see
    _pack_python), as a stick takes far longer to write thousands of small files than a
    few big ones.
  - Computer use: Windows-MCP on Windows; on Linux CLUD's own mcp/clud_desktop.py, whose
    dependencies (Pillow, python-xlib) go into that platform's Python. Installed with uv,
    targeting the stick's platform rather than the one the installer runs on.
No Git for Windows: without it Claude Code uses its PowerShell tool for commands.

Linux binaries are named *.exe on purpose: Linux mounts FAT32 sticks with "showexec",
which only lets files ending in .exe/.com/.bat run.
"""

from __future__ import annotations

import json
import os
import plistlib
import py_compile
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
import zipfile
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
    # Rough install sizes, used to refuse drives that are too small: Python and computer
    # use, then each agent (omp's includes the native library it unpacks on first start)
    size: int
    agent_size: dict[str, int]


MB = 1024**2
PLATFORMS = (
    Platform("windows", "Windows (64-bit PC)", ("app", "py"), 200 * MB,
             {"claude": 350 * MB, "omp": 330 * MB}),
    Platform("linux-arm64", "Linux ARM64 (Raspberry Pi 3/4/5 with 64-bit Pi OS)", ("linux-arm64",),
             100 * MB, {"claude": 240 * MB, "omp": 360 * MB}),
    Platform("linux-x64", "Linux x64 (64-bit PC: Ubuntu, Debian, Mint, Fedora, ...)", ("linux-x64",),
             100 * MB, {"claude": 250 * MB, "omp": 400 * MB}),
)
PLATFORM_KEYS = tuple(p.key for p in PLATFORMS)


@dataclass(frozen=True)
class Agent:
    key: str  # also the program's name on the stick: app\<key>.exe, linux-*/<key>.exe
    title: str


# The launchers start the one on the stick, or ask which when there are both
AGENTS = (
    Agent("claude", "Claude Code"),
    Agent("omp", "omp (oh-my-pi): Claude, GPT, Gemini, local models and 60+ more providers"),
)
AGENT_KEYS = tuple(a.key for a in AGENTS)
DEFAULT_AGENTS = ("claude",)


def needed_bytes(platforms, agents=DEFAULT_AGENTS) -> int:
    """Free space an install needs. Updates need it too: the new version is built next to
    the old one before swapping in."""
    return sum(p.size + sum(p.agent_size[a] for a in agents) for p in PLATFORMS if p.key in platforms)


# Every platform, the default agents
NEEDED_BYTES = needed_bytes(PLATFORM_KEYS)

# Folders the installer owns and replaces wholesale on update. "node" and ".npm-cache"
# are from the old npm-based layout, "git" from when sticks carried Git for Windows; they're
# removed when found.
MANAGED_DIRS = tuple(d for p in PLATFORMS for d in p.dirs)
LEGACY_DIRS = ("node", ".npm-cache", "git")
STAGING = ".clud-new"
RETIRED = ".clud-old"
# Hidden on Windows and macOS (Linux file managers ignore the FAT hidden flag)
HIDDEN = MANAGED_DIRS + ("mcp", "CLUD.cmd", "CLUD.sh", "omp.yml", "autorun.inf", "clud.ico",
                         "clud-version.json")

# Parts of Python CLUD never uses: Tcl/Tk for tkinter, IDLE, demos, C headers and import
# libraries, pip, and precompiled caches (_pack_python compiles its own)
PY_SKIP = ("tcl/", "include/", "libs/", "Lib/idlelib/", "Lib/turtledemo/", "Lib/tkinter/",
           "Lib/ensurepip/", "Lib/site-packages/", "Lib/__pycache__/")
# Linux Python: also terminfo (~2,900 files), and every bin/ entry but the interpreter
# itself (the rest are symlinks, which FAT would turn into 22 MB copies)
PY_LINUX_SKIP = ("include/", "share/", "lib/tcl", "lib/tk", "lib/itcl", "lib/thread", "lib/libtcl",
                 "lib/libpython", "lib/pkgconfig/", "lib/python3.13/idlelib/", "lib/python3.13/turtledemo/",
                 "lib/python3.13/tkinter/", "lib/python3.13/ensurepip/", "lib/python3.13/site-packages/",
                 "lib/python3.13/config-3.13", "lib/python3.13/__pycache__/", "bin/python3-",
                 "bin/python3.13-", "bin/pip", "bin/idle", "bin/pydoc", "bin/python3", "bin/python")
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
    clud_agents: tuple[str, ...] = ()

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


def _clud_info(mount: Path) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """(version, platforms, agents) of the CLUD on a drive; version None if there's none.

    The version is Claude Code's, or omp's on a stick without Claude Code.
    """
    if not (mount / "CLUD.cmd").exists():
        return None, (), ()
    try:
        info = json.loads((mount / "clud-version.json").read_text())
    except (OSError, ValueError):
        info = {}
    # Sticks from before Linux support only had Windows, from before omp only Claude Code
    agents = tuple(info.get("agents", ["claude"]))
    version = info.get("claude", "") if "claude" in agents else info.get("omp", "")
    return version, tuple(info.get("platforms", ["windows"])), agents


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
    python_version: str
    python_urls: dict[str, str]  # platform key -> python-build-standalone archive
    omp_version: str = ""  # "" when the lookup failed: then omp_error says why
    omp_urls: dict[str, str] | None = None  # platform key -> release binary
    omp_error: str = ""


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "clud-installer", "Accept": "application/json"})
    with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
        return json.load(r)


# npm package suffix and python-build-standalone target per platform
NPM_PLATFORM = {"windows": "win32-x64", "linux-arm64": "linux-arm64", "linux-x64": "linux-x64"}
PY_TARGET = {"windows": "x86_64-pc-windows-msvc", "linux-arm64": "aarch64-unknown-linux-gnu",
             "linux-x64": "x86_64-unknown-linux-gnu"}
# omp release binary per platform (the glibc Linux builds, not the musl ones)
OMP_ASSET = {"windows": "omp-windows-x64.exe", "linux-arm64": "omp-linux-arm64", "linux-x64": "omp-linux-x64"}
OMP_REPO = "can1357/oh-my-pi"


def _resolve_omp() -> tuple[str, dict[str, str]]:
    rel = _get_json(f"https://api.github.com/repos/{OMP_REPO}/releases/latest")
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    missing = [name for name in OMP_ASSET.values() if name not in assets]
    if missing:
        raise RuntimeError(f"omp {rel['tag_name']} has no {', '.join(missing)}")
    return rel["tag_name"].lstrip("v"), {key: assets[name] for key, name in OMP_ASSET.items()}


def resolve_sources() -> Sources:
    """Look up the latest version of each component (a handful of small web requests)."""
    claude = _get_json("https://registry.npmjs.org/@anthropic-ai/claude-code/latest")["version"]
    claude_urls = {
        key: _get_json(f"https://registry.npmjs.org/@anthropic-ai/claude-code-{npm}/{claude}")["dist"]["tarball"]
        for key, npm in NPM_PLATFORM.items()
    }
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
    # omp is optional: a failed lookup only rules it out, not the whole install
    omp_version, omp_urls, omp_error = "", None, ""
    try:
        omp_version, omp_urls = _resolve_omp()
    except Exception as e:  # network, GitHub rate limit, a release without some binary
        omp_error = str(e)
    return Sources(
        claude_version=claude,
        claude_urls=claude_urls,
        python_version=py_version,
        python_urls=python_urls,
        omp_version=omp_version,
        omp_urls=omp_urls,
        omp_error=omp_error,
    )


# --- Helpers --------------------------------------------------------------------------

def _download(url: str, report: Reporter, name: str | None = None) -> Path:
    """Download into the cache (once) and return the local path. name: what to cache it
    as, for URLs whose file name doesn't carry the version."""
    CACHE.mkdir(exist_ok=True)
    dest = CACHE / (name or url.rsplit("/", 1)[-1].replace("%2B", "+"))
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


def _flush_deletes() -> None:
    """Write pending deletions out to the drive before anything new reuses their space.

    Linux's exFAT driver can otherwise write a deleted folder's stale data over the next
    file put in its place: on Linux 7.3, CLUD.cmd came out as all zeros after an install.
    """
    if hasattr(os, "sync"):
        os.sync()


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


def _copy_big(src: Path, dst: Path, report: Reporter) -> None:
    """Copy one large file, reporting progress."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    total, done = src.stat().st_size, 0
    with open(src, "rb") as f, open(dst, "wb") as out:
        while chunk := f.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            report.progress(done, total)


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

def steps(platforms, agents=DEFAULT_AGENTS) -> list[str]:
    """Step titles of an install of these platforms and agents, in order."""
    out = []
    for p in PLATFORMS:
        if p.key in platforms:
            out.append(f"Downloading for {p.title.split(' (')[0]}")
    for p in PLATFORMS:
        if p.key not in platforms:
            continue
        name = p.title.split(" (")[0]
        if "claude" in agents:
            out.append(f"Installing Claude Code for {name}")
        if "omp" in agents:
            out.append(f"Installing omp for {name}")
        if p.key == "windows":
            out.append("Installing Python + Windows-MCP (computer use)")
        else:
            out.append(f"Installing Python + computer use for {name}")
    return out + ["Switching to the new version", "Writing launchers and settings", "Finishing up"]


def _pip_install(packages: list[str], target: Path, py_target: str, what: str) -> None:
    """pip-install packages for the stick's platform (not this computer's) into target, a
    folder on this computer: on FAT/exFAT macOS would litter it with ._ AppleDouble files
    mid-install, which uv then rejects as not in the wheel."""
    # --link-mode copy: uv would otherwise hardlink from its cache when the target is on the
    # same disk, sharing files (like psutil's .pyd) with this very process and making them
    # undeletable on the next update
    cmd = [_uv(), "pip", "install", "--quiet", "--link-mode", "copy", "--target", str(target),
           "--python-platform", py_target, "--python-version", "3.13", *packages]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Installing {what} failed:\n{r.stderr.strip()}")
    # Test suites, demos and type stubs are ~1,700 files that are never loaded at runtime
    for root, dirs, files in os.walk(target):
        for d in [d for d in dirs if d in PIP_DROP]:
            _rmtree(Path(root) / d)
            dirs.remove(d)
        for f in files:
            if f.endswith(".pyi"):
                os.unlink(Path(root) / f)


# Python imports straight from zip files, so the stick's Python keeps its standard library
# in the zip it looks for by default (python313.zip next to python.exe on Windows,
# lib/python313.zip on Linux) and pure-Python packages in another, put on sys.path by a .pth
# file. ~5,000 small files become two big ones. Each .py gets an unchecked-hash .pyc next to
# it in the zip, as Python can't cache bytecode for zipped code itself.
STDLIB_ZIP = {"Lib": "python313.zip", "lib/python3.13": "lib/python313.zip"}
SITE_ZIP = "clud-packages"
# Stays loose: os.py marks where the standard library is (Python looks for it to find its own
# home), lib-dynload holds native modules
STDLIB_LOOSE = ("os.py", "site-packages", "lib-dynload")
# Packages that need real files: comtypes and win32com generate code into their own folder
# (Windows-MCP's UI Automation wrappers), certifi hands its CA file to OpenSSL by path
SITE_LOOSE = {"comtypes", "win32com", "certifi"}
# Package files that are fine inside a zip (data among them read with importlib.resources,
# which reads zips too); anything else, mostly native code, keeps its package loose
ZIPPABLE = (".py", ".typed", ".md", ".txt", ".rst", ".json", ".yaml", ".js")
SITE_DROP = {"bin", "Scripts", "__pycache__", "PyWin32.chm"}
PIP_DROP = {"tests", "test", "Demos", "demos", "benchmarks"}


def _pack_python(py: Path, stdlib: str) -> None:
    """Move the standard library and pure-Python packages of the Python in py into zips."""
    lib = py / stdlib
    site = lib / "site-packages"
    _zip_files(py / STDLIB_ZIP[stdlib], lib,
               [f for f in _files(lib) if f.relative_to(lib).parts[0] not in STDLIB_LOOSE],
               keep={lib / "os.py"})

    for name in SITE_DROP:
        if (site / name).is_dir():
            _rmtree(site / name)
        else:
            (site / name).unlink(missing_ok=True)
    _rmtree(py / "Scripts")  # empty: pip isn't installed
    # Modules that .pth files import run before every other .pth is read
    pth_imports = {line.split()[1].rstrip(";") for pth in site.glob("*.pth")
                   for line in pth.read_text(encoding="utf-8", errors="replace").splitlines()
                   if line.startswith("import ") and len(line.split()) > 1}
    zipped: list[Path] = []
    for entry in site.iterdir():
        if entry.is_file():
            if entry.suffix == ".py" and entry.stem not in pth_imports:
                zipped.append(entry)
        elif entry.name.endswith(".dist-info"):
            zipped += _files(entry)
        elif entry.name not in SITE_LOOSE:
            files = _files(entry)
            if all(f.suffix in ZIPPABLE or f.name.startswith(("LICENSE", "COPYING")) for f in files):
                zipped += files
    _zip_files(site / f"{SITE_ZIP}.zip", site, zipped)
    (site / f"{SITE_ZIP}.pth").write_text(f"{SITE_ZIP}.zip\n", encoding="utf-8")
    # comtypes creates its comtypes.gen package on first use, then fails to import it
    # (Python's import cache misses the new folder), so a fresh stick's first Windows-MCP
    # start crashed. Ship the package ready-made.
    if (site / "comtypes").is_dir():
        (site / "comtypes" / "gen").mkdir(exist_ok=True)
        (site / "comtypes" / "gen" / "__init__.py").write_text(
            "# comtypes.gen package, directory for generated files.\n", encoding="utf-8")


def _files(folder: Path) -> list[Path]:
    return [Path(root) / f for root, dirs, files in os.walk(folder)
            if "__pycache__" not in Path(root).parts for f in files]


def _zip_files(archive: Path, base: Path, files: list[Path], keep=frozenset()) -> None:
    """Move files (paths under base) into a new zip, compiling .py files on the way."""
    compile_pyc = sys.version_info[:2] == (3, 13)  # the bytecode has to match the stick's Python
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        # Folder entries too: zipimport only finds namespace packages (no __init__.py, like
        # opentelemetry) by them
        for d in sorted({p for f in files for p in f.relative_to(base).parents if p.name}):
            zf.mkdir(d.as_posix())
        for f in sorted(files):
            arcname = f.relative_to(base).as_posix()
            zf.write(f, arcname)
            if compile_pyc and f.suffix == ".py":
                pyc = f.with_suffix(".pyc.tmp")
                try:
                    py_compile.compile(str(f), cfile=str(pyc), dfile=arcname, doraise=True,
                                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
                    zf.write(pyc, arcname[:-3] + ".pyc")
                except py_compile.PyCompileError:
                    pass  # not valid Python 3.13 (stray templates and the like); never imported
                finally:
                    pyc.unlink(missing_ok=True)
            if f not in keep:
                f.unlink()
    for root, dirs, _ in os.walk(base, topdown=False):
        for d in dirs:
            try:
                (Path(root) / d).rmdir()
            except OSError:
                pass  # not empty


def _install_python(archive: Path, dest: Path, stdlib: str, skip: tuple[str, ...], keep: tuple[str, ...],
                    packages: list[str], py_target: str, what: str, report: Reporter) -> None:
    """Put a python-build-standalone Python with packages at dest, packed (see _pack_python).

    It's put together on this computer first, then copied over in one go.
    """
    with tempfile.TemporaryDirectory(prefix="clud-py-") as tmp:
        py = Path(tmp) / "py"
        _extract_tar(archive, py, report, strip=1, skip=skip, keep=keep)
        if LINUX_PY_BIN in keep:
            (py / LINUX_PY_BIN).rename(py / "bin" / "python3.exe")
        report.progress(0, 0)
        _pip_install(packages, py / stdlib / "site-packages", py_target, what)
        _pack_python(py, stdlib)
        files = _files(py)
        total = sum(f.stat().st_size for f in files)
        done, last = 0, 0.0
        with _ParallelWriter() as writer:
            for f in files:
                out = dest / f.relative_to(py)
                out.parent.mkdir(parents=True, exist_ok=True)
                writer.copy(str(f), str(out))
                done += f.stat().st_size
                if time.monotonic() - last > 0.1:
                    last = time.monotonic()
                    report.progress(done, total)
        report.progress(total, total)


def install(target: Path, src: Sources, rename: bool, report: Reporter,
            platforms=PLATFORM_KEYS, agents=DEFAULT_AGENTS) -> list[str]:
    """Build or update a CLUD stick at target. Returns notes worth showing the user.

    Your login, settings, history and anything in work\\ are left alone. Platforms and
    agents not picked are removed from the stick (their logins and settings stay).
    """
    platforms = [p.key for p in PLATFORMS if p.key in platforms]
    agents = [a.key for a in AGENTS if a.key in agents]
    if not platforms:
        raise RuntimeError("Pick at least one platform")
    if not agents:
        raise RuntimeError("Pick at least one agent")
    if "omp" in agents and not src.omp_urls:
        raise RuntimeError(f"Couldn't look up omp: {src.omp_error or 'no release found'}")
    notes: list[str] = []
    busy = running_from(target)
    if busy:
        raise RuntimeError(f"CLUD is running from this drive ({', '.join(busy)}). "
                           "Close it, then try again.")
    titles = steps(platforms, agents)
    n = len(titles)
    step = iter(range(n))

    def next_step() -> None:
        i = next(step)
        report.step(i, n, titles[i])
        report.log(f"[{i + 1}/{n}] {titles[i]}")

    archives: dict[str, dict[str, Path]] = {}
    for key in platforms:
        next_step()
        archives[key] = {"python": _download(src.python_urls[key], report)}
        if "claude" in agents:
            archives[key]["claude"] = _download(src.claude_urls[key], report)
        if "omp" in agents:
            # The release's file names carry no version: cache them under one that does
            archives[key]["omp"] = _download(src.omp_urls[key], report,
                                             name=f"omp-{src.omp_version}-{OMP_ASSET[key]}")

    # Build the new folders side by side in a staging folder on the drive and only swap
    # them in once all are complete, so a failure (or a yanked stick) mid-way leaves the
    # previous CLUD working.
    stage = target / STAGING
    _rmtree(stage)
    _flush_deletes()
    stage.mkdir()

    for key in platforms:
        a = archives[key]
        bin_dir = stage / ("app" if key == "windows" else key)
        if "claude" in agents:
            next_step()
            name = "claude.exe" if key == "windows" else "claude"
            _extract_tar(a["claude"], bin_dir, report, strip=1, only=name)
            if not (bin_dir / name).exists():
                raise RuntimeError(f"{name} was not in the Claude Code package for {key}")
            if key != "windows":
                (bin_dir / "claude").rename(bin_dir / "claude.exe")
                _make_executable(bin_dir / "claude.exe")
        if "omp" in agents:
            next_step()
            _copy_big(a["omp"], bin_dir / "omp.exe", report)
            _make_executable(bin_dir / "omp.exe")
            report.log(f"omp {src.omp_version} installed")

        if key == "windows":
            next_step()
            _install_python(a["python"], stage / "py", "Lib", PY_SKIP, (), ["windows-mcp"],
                            PY_TARGET[key], "Windows-MCP", report)
            report.log("Python + Windows-MCP installed")
        else:
            next_step()
            _install_python(a["python"], bin_dir / "py", "lib/python3.13", PY_LINUX_SKIP, (LINUX_PY_BIN,),
                            ["pillow", "python-xlib"], PY_TARGET[key], "computer use for Linux", report)
            _make_executable(bin_dir / "py" / "bin" / "python3.exe")
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
    _flush_deletes()

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
    info = {"agents": agents}
    if "claude" in agents:
        info["claude"] = src.claude_version
    if "omp" in agents:
        info["omp"] = src.omp_version
    info.update(python=src.python_version, platforms=platforms, installed=time.strftime("%Y-%m-%d"))
    version_file.write_text(json.dumps(info, indent=2))

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

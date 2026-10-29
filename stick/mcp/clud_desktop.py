"""CLUD computer use for Linux and macOS: an MCP server (stdio) that sees and drives the desktop.

Started by CLUD.sh with the stick's own Python. On Linux, two backends, picked at startup:
  - Wayland compositors built on wlroots (labwc, the Raspberry Pi OS desktop; wayfire,
    sway, Hyprland, ...): talks the Wayland protocol directly, screenshots via
    wlr-screencopy, mouse and keyboard via the virtual-pointer / virtual-keyboard protocols.
  - X11 (any desktop on Xorg, including Raspberry Pi OS set to X11): XTest via python-xlib.
GNOME and KDE on Wayland expose none of those protocols to ordinary programs, so there
only screenshots work, through their own screenshot tools, if installed.
On macOS: screenshots with the built-in screencapture, mouse and keyboard as CoreGraphics
events (through ctypes). macOS only allows either once the user has given the terminal app
CLUD runs in the Screen Recording and Accessibility permissions; the tools say so until then.

The MCP protocol itself (JSON-RPC over stdin/stdout) is handled here too, to keep the
dependencies to Pillow and python-xlib.
"""

from __future__ import annotations

import array
import base64
import ctypes
import io
import json
import mmap
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback

from PIL import Image

# Screenshots are scaled down to fit in this box; coordinates the model sends back are in
# screenshot pixels and get scaled up again. Keeps images cheap and legible.
MAX_W, MAX_H = 1366, 768


def log(*args) -> None:
    print("clud-desktop:", *args, file=sys.stderr, flush=True)


# --- Keys ---------------------------------------------------------------------------------

# Friendly names -> X keysym names (also what XKB keymaps use)
KEY_ALIASES = {
    "enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "space": "space", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert", "home": "Home", "end": "End",
    "pageup": "Prior", "pgup": "Prior", "pagedown": "Next", "pgdn": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "printscreen": "Print", "print": "Print", "menu": "Menu", "capslock": "Caps_Lock",
    "minus": "minus", "plus": "plus", "equal": "equal", "comma": "comma", "period": "period",
    "slash": "slash", "backslash": "backslash", "semicolon": "semicolon",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute",
}
# Modifier name -> (keysym, X/XKB modifier mask bit)
MODIFIERS = {
    "shift": ("Shift_L", 1), "ctrl": ("Control_L", 4), "control": ("Control_L", 4),
    "alt": ("Alt_L", 8), "super": ("Super_L", 64), "win": ("Super_L", 64),
    "meta": ("Super_L", 64), "cmd": ("Super_L", 64), "command": ("Super_L", 64),
    "option": ("Alt_L", 8), "opt": ("Alt_L", 8),
}
CHAR_KEYSYMS = {"\n": "Return", "\t": "Tab", " ": "space"}


def char_keysym(ch: str) -> str:
    """XKB keysym name for a character. U+XXXX names cover all of Unicode."""
    return CHAR_KEYSYMS.get(ch) or f"U{ord(ch):04X}"


def parse_combo(combo: str) -> tuple[list[str], int, str]:
    """'ctrl+shift+t' -> (['Control_L', 'Shift_L'], mask, 't' keysym name)."""
    parts = [p.strip() for p in combo.replace(" ", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key")
    mods, mask = [], 0
    for p in parts[:-1]:
        m = MODIFIERS.get(p.lower())
        if not m:
            raise ValueError(f"unknown modifier {p!r}")
        mods.append(m[0])
        mask |= m[1]
    key = parts[-1]
    low = key.lower()
    if low in MODIFIERS:  # a bare modifier, e.g. "super" to open the menu
        return mods, mask, MODIFIERS[low][0]
    if low in KEY_ALIASES:
        return mods, mask, KEY_ALIASES[low]
    if len(key) == 1:
        return mods, mask, char_keysym(key.lower() if mask else key)
    if low[0] == "f" and low[1:].isdigit():
        return mods, mask, f"F{low[1:]}"
    return mods, mask, key  # trust it as a raw keysym name


# --- Wayland (wlroots) ----------------------------------------------------------------------

class WaylandError(Exception):
    pass


class Wayland:
    """A minimal Wayland client: just the wire protocol and the few interfaces we need."""

    def __init__(self) -> None:
        name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        path = name if name.startswith("/") else os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), name)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id = 2  # 1 is wl_display
        self.handlers: dict[int, object] = {}
        self.buf = b""
        self.fds: list[int] = []
        self.globals: dict[str, tuple[int, int]] = {}
        self.outputs: dict[int, dict] = {}  # global name -> info
        self.registry = self.new_id(self._on_registry)
        self.send(1, 1, ("u", self.registry))  # wl_display.get_registry
        self.roundtrip()
        missing = [i for i in ("wl_shm", "wl_seat") if i not in self.globals]
        if missing:
            raise WaylandError(f"compositor lacks {', '.join(missing)}")
        self.shm = self.bind("wl_shm", 1)
        self.seat = self.bind("wl_seat", 1)
        # Bind outputs and learn their logical layout
        xdg = self.bind("zxdg_output_manager_v1", 2) if "zxdg_output_manager_v1" in self.globals else None
        for gname, info in self.outputs.items():
            info["id"] = self.bind_name(gname, "wl_output", min(info["version"], 3), self._output_handler(info))
            if xdg:
                xid = self.new_id(self._xdg_output_handler(info))
                self.send(xdg, 1, ("u", xid), ("u", info["id"]))  # get_xdg_output
        self.roundtrip()
        self.roundtrip()
        self.pointer = self.keyboard = None
        self.keymap_syms: dict[str, int] = {}

    # wire format
    def new_id(self, handler=None) -> int:
        i = self.next_id
        self.next_id += 1
        self.handlers[i] = handler
        return i

    def send(self, obj: int, opcode: int, *args, fds: list[int] | None = None) -> None:
        body = b""
        for kind, value in args:
            if kind in ("u", "o"):
                body += struct.pack("<I", value)
            elif kind == "i":
                body += struct.pack("<i", value)
            elif kind == "f":  # 24.8 fixed point
                body += struct.pack("<i", int(round(value * 256)))
            elif kind == "s":
                raw = value.encode() + b"\0"
                body += struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)
        msg = struct.pack("<II", obj, ((8 + len(body)) << 16) | opcode) + body
        if fds:
            self.sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
        else:
            self.sock.sendall(msg)

    def dispatch(self, timeout: float | None = None) -> None:
        """Read from the socket once and handle every complete event in it."""
        self.sock.settimeout(timeout)
        try:
            data, anc, _, _ = self.sock.recvmsg(65536, socket.CMSG_SPACE(16 * 4))
        except socket.timeout:
            return
        finally:
            self.sock.settimeout(None)
        if not data:
            raise WaylandError("compositor closed the connection")
        for level, typ, cdata in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(cdata[: len(cdata) - len(cdata) % 4])
                self.fds.extend(fds)
        self.buf += data
        while len(self.buf) >= 8:
            obj, word = struct.unpack_from("<II", self.buf)
            size, opcode = word >> 16, word & 0xFFFF
            if len(self.buf) < size:
                break
            payload, self.buf = self.buf[8:size], self.buf[size:]
            if obj == 1:
                self._on_display(opcode, payload)
            else:
                handler = self.handlers.get(obj)
                if handler:
                    handler(opcode, payload)

    def roundtrip(self) -> None:
        done = []
        cb = self.new_id(lambda op, p: done.append(1))
        self.send(1, 0, ("u", cb))  # wl_display.sync
        while not done:
            self.dispatch()

    def _on_display(self, opcode: int, p: bytes) -> None:
        if opcode == 0:  # error
            obj, code = struct.unpack_from("<II", p)
            raise WaylandError(f"protocol error on object {obj} (code {code}): {_string(p, 8)[0]}")
        if opcode == 1:  # delete_id
            self.handlers.pop(struct.unpack_from("<I", p)[0], None)

    def _on_registry(self, opcode: int, p: bytes) -> None:
        if opcode == 0:
            name = struct.unpack_from("<I", p)[0]
            iface, off = _string(p, 4)
            version = struct.unpack_from("<I", p, off)[0]
            self.globals[iface] = (name, version)
            if iface == "wl_output":
                self.outputs[name] = {"version": version, "x": 0, "y": 0, "w": 0, "h": 0,
                                      "lx": None, "ly": None, "lw": None, "lh": None, "transform": 0}
        elif opcode == 1:
            self.outputs.pop(struct.unpack_from("<I", p)[0], None)

    def bind(self, iface: str, version: int, handler=None) -> int:
        name, have = self.globals[iface]
        return self.bind_name(name, iface, min(version, have), handler)

    def bind_name(self, name: int, iface: str, version: int, handler=None) -> int:
        new = self.new_id(handler)
        # wl_registry.bind(name, new_id with interface string + version)
        self.send(self.registry, 0, ("u", name), ("s", iface), ("u", version), ("u", new))
        return new

    def _output_handler(self, info: dict):
        def on(opcode: int, p: bytes) -> None:
            if opcode == 0:  # geometry
                info["x"], info["y"] = struct.unpack_from("<ii", p)
                info["transform"] = struct.unpack_from("<i", p, len(p) - 4)[0]
            elif opcode == 1:  # mode
                flags, w, h, _ = struct.unpack_from("<Iiii", p)
                if flags & 1:  # current
                    info["w"], info["h"] = w, h
        return on

    def _xdg_output_handler(self, info: dict):
        def on(opcode: int, p: bytes) -> None:
            if opcode == 0:
                info["lx"], info["ly"] = struct.unpack_from("<ii", p)
            elif opcode == 1:
                info["lw"], info["lh"] = struct.unpack_from("<ii", p)
        return on

    # layout
    def layout(self) -> list[dict]:
        outs = []
        for info in self.outputs.values():
            if not info.get("id"):
                continue
            w, h = info["w"], info["h"]
            if info["transform"] in (1, 3, 5, 7):
                w, h = h, w
            lx = info["lx"] if info["lx"] is not None else info["x"]
            ly = info["ly"] if info["ly"] is not None else info["y"]
            outs.append({**info, "LX": lx, "LY": ly, "LW": info["lw"] or w, "LH": info["lh"] or h})
        if not outs:
            raise WaylandError("no outputs (monitors) found")
        return outs

    # screencopy
    def capture(self, out: dict) -> Image.Image:
        if "zwlr_screencopy_manager_v1" not in self.globals:
            raise WaylandError("compositor has no wlr-screencopy (not a wlroots desktop?)")
        mgr = getattr(self, "_screencopy", None) or self.bind("zwlr_screencopy_manager_v1", 3)
        self._screencopy = mgr
        version = min(self.globals["zwlr_screencopy_manager_v1"][1], 3)
        st: dict = {"fmt": None, "done": False, "ready": False, "failed": False, "flags": 0}

        def on(opcode: int, p: bytes) -> None:
            if opcode == 0 and st["fmt"] is None:  # buffer: format, w, h, stride
                st["fmt"] = struct.unpack_from("<IIII", p)
            elif opcode == 1:
                st["flags"] = struct.unpack_from("<I", p)[0]
            elif opcode == 2:
                st["ready"] = True
            elif opcode == 3:
                st["failed"] = True
            elif opcode == 6:
                st["done"] = True

        frame = self.new_id(on)
        self.send(mgr, 0, ("u", frame), ("i", 0), ("o", out["id"]))  # capture_output, no cursor
        while not st["failed"] and (st["fmt"] is None or (version >= 3 and not st["done"])):
            self.dispatch()
        if st["failed"]:
            raise WaylandError("screencopy failed")
        fmt, w, h, stride = st["fmt"]
        size = stride * h
        fd = os.memfd_create("clud-shot")
        try:
            os.ftruncate(fd, size)
            pool = self.new_id()
            self.send(self.shm, 0, ("u", pool), ("i", size), fds=[fd])  # create_pool(id, fd, size)
            buf = self.new_id()
            self.send(pool, 0, ("u", buf), ("i", 0), ("i", w), ("i", h), ("i", stride), ("u", fmt))
            self.send(frame, 0, ("o", buf))  # copy
            while not (st["ready"] or st["failed"]):
                self.dispatch()
            if st["failed"]:
                raise WaylandError("screencopy failed")
            with mmap.mmap(fd, size) as m:
                data = bytes(m)
            self.send(frame, 1)  # destroy
            self.send(buf, 0)
            self.send(pool, 1)
        finally:
            os.close(fd)
        # ARGB8888 / XRGB8888 are B,G,R,A in memory; ABGR8888 / XBGR8888 are R,G,B,A
        raw = {0: "BGRX", 1: "BGRX", 0x34324241: "RGBX", 0x34324258: "RGBX"}.get(fmt)
        if not raw:
            raise WaylandError(f"unsupported screencopy pixel format {fmt:#x}")
        img = Image.frombuffer("RGB", (w, h), data, "raw", raw, stride, 1)
        if st["flags"] & 1:
            img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        transpose = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}
        if out["transform"] in transpose:
            img = img.transpose(transpose[out["transform"]])
        return img

    # input
    def _ensure_pointer(self) -> int:
        if self.pointer is None:
            if "zwlr_virtual_pointer_manager_v1" not in self.globals:
                raise WaylandError("compositor has no virtual-pointer protocol: can't move the mouse")
            mgr = self.bind("zwlr_virtual_pointer_manager_v1", 1)
            self.pointer = self.new_id()
            self.send(mgr, 0, ("o", self.seat), ("u", self.pointer))
        return self.pointer

    def move(self, x: float, y: float, extent: tuple[int, int, int, int]) -> None:
        ex, ey, ew, eh = extent
        p = self._ensure_pointer()
        # Absolute positions are relative to the bounding box of all outputs
        self.send(p, 1, ("u", _ms()), ("u", int(x - ex)), ("u", int(y - ey)), ("u", ew), ("u", eh))
        self.send(p, 4)
        self.roundtrip()

    def button(self, code: int, pressed: bool) -> None:
        p = self._ensure_pointer()
        self.send(p, 2, ("u", _ms()), ("u", code), ("u", 1 if pressed else 0))
        self.send(p, 4)
        self.roundtrip()

    def scroll(self, dx: int, dy: int) -> None:
        p = self._ensure_pointer()
        for axis, steps in ((0, dy), (1, dx)):
            if steps:
                self.send(p, 5, ("u", 0))  # axis_source wheel
                self.send(p, 7, ("u", _ms()), ("u", axis), ("f", 15.0 * steps), ("i", steps))
                self.send(p, 4)
        self.roundtrip()

    def _ensure_keyboard(self, syms: list[str]) -> None:
        if "zwp_virtual_keyboard_manager_v1" not in self.globals:
            raise WaylandError("compositor has no virtual-keyboard protocol: can't type")
        if self.keyboard is None:
            mgr = self.bind("zwp_virtual_keyboard_manager_v1", 1)
            self.keyboard = self.new_id()
            self.send(mgr, 0, ("o", self.seat), ("u", self.keyboard))
        new = [s for s in dict.fromkeys(syms) if s not in self.keymap_syms]
        if not new and self.keymap_syms:
            return
        for s in new:
            self.keymap_syms[s] = len(self.keymap_syms) + 1
        # Like wtype: a keymap made up on the spot, one keycode per keysym we need. Modifier
        # keysyms are bound to their real modifiers so shortcuts work.
        codes = "\n".join(f"<K{i}> = {i + 8};" for i in self.keymap_syms.values())
        mod_of = {"Shift_L": "Shift", "Control_L": "Control", "Alt_L": "Mod1", "Super_L": "Mod4"}
        keys, mods = [], []
        for s, i in self.keymap_syms.items():
            keys.append(f"key <K{i}> {{ [ {s} ] }};")
            if s in mod_of:
                mods.append(f"modifier_map {mod_of[s]} {{ <K{i}> }};")
        keymap = (
            "xkb_keymap {\n"
            f'xkb_keycodes "clud" {{ minimum = 8; maximum = {len(self.keymap_syms) + 9}; {codes} }};\n'
            'xkb_types "clud" { include "complete" };\n'
            'xkb_compatibility "clud" { include "complete" };\n'
            f'xkb_symbols "clud" {{ {" ".join(keys)} {" ".join(mods)} }};\n'
            "};\n"
        ).encode() + b"\0"
        fd = os.memfd_create("clud-keymap")
        try:
            os.write(fd, keymap)
            self.send(self.keyboard, 0, ("u", 1), ("u", len(keymap)), fds=[fd])  # XKB_V1
        finally:
            os.close(fd)
        self.roundtrip()

    def key(self, sym: str, pressed: bool) -> None:
        self.send(self.keyboard, 1, ("u", _ms()), ("u", self.keymap_syms[sym] + 0), ("u", 1 if pressed else 0))

    def modifiers(self, mask: int) -> None:
        self.send(self.keyboard, 2, ("u", mask), ("u", 0), ("u", 0), ("u", 0))

    def combo(self, mods: list[str], mask: int, sym: str) -> None:
        self._ensure_keyboard(mods + [sym])
        for m in mods:
            self.key(m, True)
        self.modifiers(mask)
        self.key(sym, True)
        self.key(sym, False)
        for m in reversed(mods):
            self.key(m, False)
        self.modifiers(0)
        self.roundtrip()

    def type_text(self, text: str, delay: float) -> None:
        syms = [char_keysym(c) for c in text]
        # Keymaps over a few hundred keycodes misbehave; type long, varied text in chunks
        start = 0
        while start < len(syms):
            chunk, seen = [], set()
            for s in syms[start:]:
                if s not in seen and len(seen | set(self.keymap_syms)) >= 200:
                    break
                seen.add(s)
                chunk.append(s)
            if not chunk:
                self.keymap_syms = {}
                continue
            self._ensure_keyboard(chunk)
            for s in chunk:
                self.key(s, True)
                self.key(s, False)
                if delay:
                    self.roundtrip()
                    time.sleep(delay)
            self.roundtrip()
            start += len(chunk)


def _string(p: bytes, off: int) -> tuple[str, int]:
    n = struct.unpack_from("<I", p, off)[0]
    s = p[off + 4: off + 4 + n].rstrip(b"\0").decode(errors="replace")
    return s, off + 4 + n + (-n % 4)


def _ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


class WaylandBackend:
    name = "wayland"

    def __init__(self) -> None:
        self.wl = Wayland()
        caps = [g for g in ("zwlr_screencopy_manager_v1", "zwlr_virtual_pointer_manager_v1",
                            "zwp_virtual_keyboard_manager_v1") if g in self.wl.globals]
        if not caps:
            raise WaylandError("not a wlroots compositor")
        self.missing = {"screencopy": "zwlr_screencopy_manager_v1" not in self.wl.globals,
                        "pointer": "zwlr_virtual_pointer_manager_v1" not in self.wl.globals,
                        "keyboard": "zwp_virtual_keyboard_manager_v1" not in self.wl.globals}

    def extent(self) -> tuple[int, int, int, int]:
        outs = self.wl.layout()
        x0 = min(o["LX"] for o in outs)
        y0 = min(o["LY"] for o in outs)
        x1 = max(o["LX"] + o["LW"] for o in outs)
        y1 = max(o["LY"] + o["LH"] for o in outs)
        return x0, y0, x1 - x0, y1 - y0

    def screenshot(self) -> Image.Image:
        x0, y0, w, h = self.extent()
        outs = self.wl.layout()
        if len(outs) == 1:
            img = self.wl.capture(outs[0])
            return img if img.size == (w, h) else img.resize((w, h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (w, h))
        for o in outs:
            img = self.wl.capture(o)
            if img.size != (o["LW"], o["LH"]):
                img = img.resize((o["LW"], o["LH"]), Image.Resampling.LANCZOS)
            canvas.paste(img, (o["LX"] - x0, o["LY"] - y0))
        return canvas

    def move(self, x: int, y: int) -> None:
        self.wl.move(x, y, self.extent())

    def button(self, button: str, pressed: bool) -> None:
        self.wl.button({"left": 0x110, "right": 0x111, "middle": 0x112}[button], pressed)

    def scroll(self, dx: int, dy: int) -> None:
        self.wl.scroll(dx, dy)

    def combo(self, combo: str) -> None:
        self.wl.combo(*parse_combo(combo))

    def type_text(self, text: str) -> None:
        self.wl.type_text(text, 0.004)

    def pointer_position(self):
        return None  # Wayland doesn't tell ordinary clients where the pointer is


# --- X11 ----------------------------------------------------------------------------------

class X11Backend:
    name = "x11"

    def __init__(self) -> None:
        from Xlib import X, XK, display
        from Xlib.ext import xtest

        self.X, self.XK, self.xtest = X, XK, xtest
        self.d = display.Display()
        if not self.d.has_extension("XTEST"):
            raise RuntimeError("X server lacks the XTEST extension")
        self.root = self.d.screen().root

    def extent(self) -> tuple[int, int, int, int]:
        g = self.root.get_geometry()
        return 0, 0, g.width, g.height

    def screenshot(self) -> Image.Image:
        _, _, w, h = self.extent()
        raw = self.root.get_image(0, 0, w, h, self.X.ZPixmap, 0xFFFFFFFF)
        return Image.frombytes("RGB", (w, h), raw.data, "raw", "BGRX")

    def move(self, x: int, y: int) -> None:
        self.xtest.fake_input(self.d, self.X.MotionNotify, x=int(x), y=int(y))
        self.d.sync()

    def button(self, button: str, pressed: bool) -> None:
        code = {"left": 1, "middle": 2, "right": 3}[button]
        self.xtest.fake_input(self.d, self.X.ButtonPress if pressed else self.X.ButtonRelease, code)
        self.d.sync()

    def scroll(self, dx: int, dy: int) -> None:
        for steps, neg, pos in ((dy, 4, 5), (dx, 6, 7)):
            for _ in range(abs(steps)):
                b = pos if steps > 0 else neg
                self.xtest.fake_input(self.d, self.X.ButtonPress, b)
                self.xtest.fake_input(self.d, self.X.ButtonRelease, b)
        self.d.sync()

    def _keycode(self, sym_name: str) -> tuple[int, bool]:
        """(keycode, needs shift) for a keysym, remapping a spare keycode if none has it."""
        sym = self.XK.string_to_keysym(sym_name)
        if not sym and sym_name.startswith("U") and len(sym_name) > 1:
            cp = int(sym_name[1:], 16)
            sym = cp if (0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF) else 0x01000000 | cp
        if not sym:
            raise ValueError(f"unknown key {sym_name!r}")
        for code, index in self.d.keysym_to_keycodes(sym):
            if index in (0, 1):
                return code, index == 1
        # Not on the keyboard: borrow the highest keycode with nothing mapped to it
        first = self.d.display.info.min_keycode
        count = self.d.display.info.max_keycode - first + 1
        mapping = self.d.get_keyboard_mapping(first, count)
        for i in range(count - 1, -1, -1):
            if not any(mapping[i]):
                code = first + i
                self.d.change_keyboard_mapping(code, [(sym, sym)])
                self.d.sync()
                self._borrowed = code
                return code, False
        raise RuntimeError("no free keycode to type that character")

    def _tap(self, code: int, shift: bool) -> None:
        shift_code = self.d.keysym_to_keycode(self.XK.XK_Shift_L)
        if shift:
            self.xtest.fake_input(self.d, self.X.KeyPress, shift_code)
        self.xtest.fake_input(self.d, self.X.KeyPress, code)
        self.xtest.fake_input(self.d, self.X.KeyRelease, code)
        if shift:
            self.xtest.fake_input(self.d, self.X.KeyRelease, shift_code)
        self.d.sync()

    def _release_borrowed(self) -> None:
        code = getattr(self, "_borrowed", None)
        if code:
            time.sleep(0.05)  # let the app read the key before the mapping goes away
            self.d.change_keyboard_mapping(code, [(0, 0)])
            self.d.sync()
            self._borrowed = None

    def combo(self, combo: str) -> None:
        mods, _, sym = parse_combo(combo)
        mod_codes = [self.d.keysym_to_keycode(self.XK.string_to_keysym(m)) for m in mods]
        code, shift = self._keycode(sym)
        for c in mod_codes:
            self.xtest.fake_input(self.d, self.X.KeyPress, c)
        self._tap(code, shift and not mods)
        for c in reversed(mod_codes):
            self.xtest.fake_input(self.d, self.X.KeyRelease, c)
        self.d.sync()
        self._release_borrowed()

    def type_text(self, text: str) -> None:
        for ch in text:
            code, shift = self._keycode(char_keysym(ch))
            self._tap(code, shift)
            self._release_borrowed()
            time.sleep(0.004)

    def pointer_position(self):
        p = self.root.query_pointer()
        return p.root_x, p.root_y

# --- macOS ----------------------------------------------------------------------------------

class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# Virtual keycodes by key position on a US keyboard. Apps read Cmd shortcuts through the
# current layout, so these work on other layouts too.
MAC_CHAR_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50, " ": 49,
}
MAC_SHIFTED = dict(zip('!@#$%^&*()_+{}|:"<>?~', "1234567890-=[]\\;',./`"))
# X keysym names (what parse_combo returns) -> keycodes
MAC_KEYCODES = {
    "Return": 36, "Tab": 48, "space": 49, "BackSpace": 51, "Escape": 53, "Delete": 117,
    "Insert": 114, "Home": 115, "End": 119, "Prior": 116, "Next": 121,
    "Left": 123, "Right": 124, "Down": 125, "Up": 126, "Caps_Lock": 57,
    "minus": 27, "equal": 24, "comma": 43, "period": 47, "slash": 44, "backslash": 42, "semicolon": 41,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98, "F8": 100,
    "F9": 101, "F10": 109, "F11": 103, "F12": 111, "F13": 105, "F14": 107, "F15": 113,
    "F16": 106, "F17": 64, "F18": 79, "F19": 80, "F20": 90,
    "Shift_L": 56, "Control_L": 59, "Alt_L": 58, "Super_L": 55,
}
# Modifier keysym -> CGEventFlags bit
MAC_FLAGS = {"Shift_L": 0x20000, "Control_L": 0x40000, "Alt_L": 0x80000, "Super_L": 0x100000}
# CGEventType per button: (down, up, dragged), and CGMouseButton
MAC_BUTTONS = {"left": (1, 2, 6, 0), "right": (3, 4, 7, 1), "middle": (25, 26, 27, 2)}
MAC_MOVED = 5
HID_TAP = 0  # kCGHIDEventTap
CLICK_STATE = 1  # kCGMouseEventClickState: 2 makes the second click of a pair a double-click


class MacPermissionError(RuntimeError):
    pass


def _mac_app() -> str:
    """The app macOS gives (or refuses) the permissions to: the terminal CLUD runs in."""
    prog = os.environ.get("TERM_PROGRAM", "")
    return {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code",
            "WarpTerminal": "Warp"}.get(prog, prog or "the terminal app")


class MacBackend:
    name = "macos"

    def __init__(self) -> None:
        fw = "/System/Library/Frameworks/{0}.framework/{0}"
        cg = self.cg = ctypes.CDLL(fw.format("CoreGraphics"))
        self.cf = ctypes.CDLL(fw.format("CoreFoundation"))
        self.ax = ctypes.CDLL(fw.format("ApplicationServices"))
        vp, u32, b = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_bool
        for fn, res, args in (
            ("CGEventSourceCreate", vp, [ctypes.c_int32]),
            ("CGEventCreate", vp, [vp]),
            ("CGEventCreateMouseEvent", vp, [vp, u32, CGPoint, u32]),
            ("CGEventCreateKeyboardEvent", vp, [vp, ctypes.c_uint16, b]),
            # Not CGEventCreateScrollWheelEvent: ctypes can't call variadic functions on arm64
            ("CGEventCreateScrollWheelEvent2", vp, [vp, u32, u32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]),
            ("CGEventSetIntegerValueField", None, [vp, u32, ctypes.c_int64]),
            ("CGEventSetFlags", None, [vp, ctypes.c_uint64]),
            ("CGEventKeyboardSetUnicodeString", None, [vp, ctypes.c_ulong, ctypes.c_char_p]),
            ("CGEventGetLocation", CGPoint, [vp]),
            ("CGEventPost", None, [u32, vp]),
            ("CGGetActiveDisplayList", ctypes.c_int32, [u32, ctypes.POINTER(u32), ctypes.POINTER(u32)]),
            ("CGDisplayBounds", CGRect, [u32]),
            ("CGPreflightScreenCaptureAccess", b, []),
            ("CGRequestScreenCaptureAccess", b, []),
            ("CGPreflightPostEventAccess", b, []),
            ("CGRequestPostEventAccess", b, []),
        ):
            f = getattr(cg, fn)
            f.restype, f.argtypes = res, args
        self.cf.CFRelease.argtypes = [vp]
        self.ax.AXIsProcessTrusted.restype = b
        self.src = cg.CGEventSourceCreate(1)  # kCGEventSourceStateHIDSystemState
        self.held = None  # button being held, so moves become drags
        self.last_click = (0.0, None, 0, 0, 0)  # time, button, x, y, click count
        self.asked: set[str] = set()

    # permissions
    def permissions(self) -> dict:
        return {"screen_recording": bool(self.cg.CGPreflightScreenCaptureAccess()),
                "accessibility": bool(self.cg.CGPreflightPostEventAccess() or self.ax.AXIsProcessTrusted())}

    def _need(self, what: str) -> None:
        if self.permissions()[what]:
            return
        app = _mac_app()
        if what == "screen_recording":
            pane, setting = "Privacy_ScreenCapture", "Screen & System Audio Recording"
            after = f"then quit {app} (Cmd+Q) and start CLUD again"
            request = self.cg.CGRequestScreenCaptureAccess
        else:
            pane, setting = "Privacy_Accessibility", "Accessibility"
            after = "then try again"
            request = self.cg.CGRequestPostEventAccess
        if what not in self.asked:
            self.asked.add(what)
            request()  # macOS asks the user, once, and puts the app in the list
        raise MacPermissionError(
            f"macOS hasn't allowed {app} {setting}. Ask the user to turn on {app} in System Settings > "
            f"Privacy & Security > {setting} (it may ask for their password), {after}. "
            f"You can open that page for them: open 'x-apple.systempreferences:com.apple.preference.security?{pane}'")

    # screen
    def _displays(self) -> list[tuple[int, int, int, int]]:
        ids, n = (ctypes.c_uint32 * 16)(), ctypes.c_uint32()
        self.cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
        out = []
        for i in range(n.value):
            r = self.cg.CGDisplayBounds(ids[i])
            out.append((round(r.origin.x), round(r.origin.y), round(r.size.width), round(r.size.height)))
        if not out:
            raise RuntimeError("no displays found (is the Mac asleep or the lid closed?)")
        return out

    def extent(self) -> tuple[int, int, int, int]:
        # In points, the units of mouse events; the main display's top left is 0,0
        ds = self._displays()
        x0, y0 = min(d[0] for d in ds), min(d[1] for d in ds)
        return x0, y0, max(d[0] + d[2] for d in ds) - x0, max(d[1] + d[3] for d in ds) - y0

    def screenshot(self) -> Image.Image:
        self._need("screen_recording")
        x0, y0, w, h = self.extent()
        canvas = Image.new("RGB", (w, h))
        with tempfile.TemporaryDirectory(prefix="clud-shot-") as tmp:
            for i, (x, y, dw, dh) in enumerate(self._displays()):
                f = os.path.join(tmp, f"{i}.png")
                r = subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", f"-R{x},{y},{dw},{dh}", f],
                                   capture_output=True, text=True, timeout=30)
                if not os.path.exists(f):
                    raise RuntimeError(f"screencapture failed: {(r.stderr or r.stdout).strip()}")
                with Image.open(f) as img:
                    # Retina screens capture at 2x: bring it to points, like the mouse uses
                    img = img.convert("RGB")
                    if img.size != (dw, dh):
                        img = img.resize((dw, dh), Image.Resampling.LANCZOS)
                    canvas.paste(img, (x - x0, y - y0))
        return canvas

    # input
    def _post(self, ev) -> None:
        if not ev:
            raise RuntimeError("couldn't create the input event")
        self.cg.CGEventPost(HID_TAP, ev)
        self.cf.CFRelease(ev)

    def _mouse(self, kind: int, x: float, y: float, button: int, clicks: int = 0) -> None:
        ev = self.cg.CGEventCreateMouseEvent(self.src, kind, CGPoint(x, y), button)
        if clicks:
            self.cg.CGEventSetIntegerValueField(ev, CLICK_STATE, clicks)
        self._post(ev)

    def pointer_position(self):
        ev = self.cg.CGEventCreate(None)
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        return round(p.x), round(p.y)

    def move(self, x: int, y: int) -> None:
        self._need("accessibility")
        if self.held:
            _, _, dragged, code = MAC_BUTTONS[self.held]
            self._mouse(dragged, x, y, code)
        else:
            self._mouse(MAC_MOVED, x, y, 0)
        time.sleep(0.01)

    def button(self, button: str, pressed: bool) -> None:
        self._need("accessibility")
        down, up, _, code = MAC_BUTTONS[button]
        x, y = self.pointer_position()
        if pressed:
            # macOS counts double-clicks from a field on the event, not from timing
            t, b, lx, ly, n = self.last_click
            n = n + 1 if b == button and time.monotonic() - t < 0.5 and abs(x - lx) + abs(y - ly) < 5 else 1
            self.last_click = (time.monotonic(), button, x, y, n)
            self.held = button
        self._mouse(down if pressed else up, x, y, code, self.last_click[4])
        if not pressed:
            self.held = None

    def scroll(self, dx: int, dy: int) -> None:
        self._need("accessibility")
        for _ in range(max(abs(dx), abs(dy))):
            # Positive is up / left; three lines a wheel step, like other systems
            wy = -3 if dy > 0 else 3 if dy < 0 else 0
            wx = -3 if dx > 0 else 3 if dx < 0 else 0
            dy, dx = dy - (dy > 0) + (dy < 0), dx - (dx > 0) + (dx < 0)
            self._post(self.cg.CGEventCreateScrollWheelEvent2(self.src, 1, 2, wy, wx, 0))
            time.sleep(0.01)

    def _key(self, code: int, down: bool, flags: int, text: str | None = None) -> None:
        ev = self.cg.CGEventCreateKeyboardEvent(self.src, code, down)
        self.cg.CGEventSetFlags(ev, flags)
        if text is not None:
            raw = text.encode("utf-16-le")
            self.cg.CGEventKeyboardSetUnicodeString(ev, len(raw) // 2, raw)
        self._post(ev)

    @staticmethod
    def _keycode(sym: str) -> tuple[int, int]:
        """(keycode, extra flags) for a keysym name from parse_combo."""
        if sym in MAC_KEYCODES:
            return MAC_KEYCODES[sym], 0
        ch = sym if len(sym) == 1 else None
        if sym[:1] == "U" and len(sym) > 1:
            try:
                ch = chr(int(sym[1:], 16))
            except ValueError:
                pass
        if ch and ch.lower() in MAC_CHAR_CODES:
            return MAC_CHAR_CODES[ch.lower()], MAC_FLAGS["Shift_L"] if ch != ch.lower() else 0
        if ch and ch in MAC_SHIFTED:
            return MAC_CHAR_CODES[MAC_SHIFTED[ch]], MAC_FLAGS["Shift_L"]
        raise ValueError(f"unknown key {sym!r} (macOS media keys aren't supported; use osascript)")

    def combo(self, combo: str) -> None:
        self._need("accessibility")
        mods, _, sym = parse_combo(combo)
        code, extra = self._keycode(sym)
        flags = 0
        for m in mods:
            flags |= MAC_FLAGS[m]
            self._key(MAC_KEYCODES[m], True, flags)
        self._key(code, True, flags | extra)
        self._key(code, False, flags | extra)
        for m in reversed(mods):
            flags &= ~MAC_FLAGS[m]
            self._key(MAC_KEYCODES[m], False, flags)

    def type_text(self, text: str) -> None:
        self._need("accessibility")
        for ch in text:
            if ch in "\n\t":
                code = MAC_KEYCODES["Return" if ch == "\n" else "Tab"]
                self._key(code, True, 0)
                self._key(code, False, 0)
            else:
                # Any character, whatever the keyboard layout: the event carries the text
                self._key(0, True, 0, ch)
                self._key(0, False, 0, ch)
            time.sleep(0.008)


# --- Screenshot-only fallback (GNOME / KDE on Wayland) --------------------------------------

class ScreenshotOnlyBackend:
    name = "screenshot-only"

    TOOLS = (["gnome-screenshot", "-f", "{f}"], ["spectacle", "-b", "-n", "-f", "-o", "{f}"],
             ["grim", "{f}"])

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def screenshot(self) -> Image.Image:
        for cmd in self.TOOLS:
            if shutil.which(cmd[0]):
                with tempfile.TemporaryDirectory() as tmp:
                    f = os.path.join(tmp, "shot.png")
                    subprocess.run([c.replace("{f}", f) for c in cmd], capture_output=True, timeout=20)
                    if os.path.exists(f):
                        img = Image.open(f)
                        img.load()
                        self._size = img.size
                        return img.convert("RGB")
        raise RuntimeError(f"Can't take screenshots here: {self.reason}")

    def extent(self):
        if not getattr(self, "_size", None):
            self.screenshot()
        return 0, 0, *self._size

    def _no_input(self, *a, **k):
        raise RuntimeError(f"Can't control the mouse or keyboard here: {self.reason}. "
                           "Use the shell instead, or ask the user to switch to an X11 session.")

    move = button = scroll = combo = type_text = _no_input

    def pointer_position(self):
        return None


def pick_backend():
    if sys.platform == "darwin":
        return MacBackend()
    errors = []
    if os.environ.get("WAYLAND_DISPLAY") or os.path.exists(
            os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent"), "wayland-0")):
        try:
            return WaylandBackend()
        except Exception as e:
            errors.append(f"Wayland: {e}")
    if os.environ.get("DISPLAY"):
        try:
            return X11Backend()
        except Exception as e:
            errors.append(f"X11: {e}")
    if not errors:
        errors.append("no graphical session found (no WAYLAND_DISPLAY or DISPLAY)")
    return ScreenshotOnlyBackend("; ".join(errors))


# --- Tools --------------------------------------------------------------------------------

class Desktop:
    def __init__(self) -> None:
        self._backend = None

    @property
    def b(self):
        if self._backend is None:
            self._backend = pick_backend()
            log("backend:", self._backend.name)
        return self._backend

    def reset(self) -> None:
        self._backend = None

    def geometry(self) -> tuple[int, int, int, int, float]:
        """Desktop origin and size, and how much screenshots are scaled down. Worked out
        fresh each time, so it stays right if the resolution changes."""
        x0, y0, w, h = self.b.extent()
        return x0, y0, w, h, min(1.0, MAX_W / w, MAX_H / h)

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        x0, y0, _, _, scale = self.geometry()
        return round(x0 + x / scale), round(y0 + y / scale)

    def screenshot(self) -> tuple[bytes, str]:
        img = self.b.screenshot()
        _, _, w, h, scale = self.geometry()
        if scale < 1:
            img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "PNG", compress_level=6)
        note = (f"Screenshot {img.width}x{img.height} (screen {w}x{h}, {self.b.name}). "
                "Give coordinates in this image's pixels.")
        return out.getvalue(), note


desktop = Desktop()
MAC = sys.platform == "darwin"

TOOLS = [
    {"name": "Screenshot", "description":
        "Take a screenshot of the whole desktop. Coordinates for the other tools are pixels in the "
        "latest screenshot, so take one before clicking.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "Click", "description": "Move the mouse to (x, y) and click.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
         "clicks": {"type": "integer", "default": 1, "description": "2 for a double-click"}},
         "required": ["x", "y"]}},
    {"name": "Move", "description": "Move the mouse to (x, y) without clicking (hover).",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]}},
    {"name": "Drag", "description": "Press the left button at (x, y), drag to (to_x, to_y) and release.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "to_x": {"type": "number"}, "to_y": {"type": "number"}},
         "required": ["x", "y", "to_x", "to_y"]}},
    {"name": "Scroll", "description": "Scroll the mouse wheel at (x, y).",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "amount": {"type": "integer", "default": 3, "description": "wheel steps"}},
         "required": ["direction"]}},
    {"name": "Type", "description":
        "Type text with the keyboard into whatever has focus. Click the field first if needed.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"},
         "press_enter": {"type": "boolean", "default": False}}, "required": ["text"]}},
    {"name": "Shortcut", "description":
        ("Press a key or key combination, e.g. 'enter', 'esc', 'cmd+c', 'cmd+q', 'cmd+tab', "
         "'cmd+space' (Spotlight: type an app's name, then enter), 'cmd+shift+g', 'F5'. "
         if MAC else
         "Press a key or key combination, e.g. 'enter', 'esc', 'ctrl+c', 'alt+f4', 'ctrl+alt+t', "
         "'super' (opens the menu on many desktops), 'F5'. ") +
        "Several separated by spaces: 'down down enter'.",
     "inputSchema": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}},
    {"name": "Wait", "description": "Wait a number of seconds (for an app to open or a page to load).",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
    {"name": "Status", "description":
        "What this server can do on this desktop: backend (wayland / x11 / screenshot-only / macos), "
        "screen size, and anything that isn't supported or allowed.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def text(s: str) -> dict:
    return {"type": "text", "text": s}


def call_tool(name: str, a: dict) -> list[dict]:
    d = desktop
    if name == "Screenshot":
        png, note = d.screenshot()
        return [{"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"}, text(note)]
    if name == "Status":
        b = d.b
        info = {"backend": b.name, "session": os.environ.get("XDG_SESSION_TYPE", ""),
                "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "")}
        try:
            info["screen"] = "{2}x{3} at {0},{1}".format(*b.extent())
        except Exception as e:
            info["screen"] = f"unknown ({e})"
        if isinstance(b, WaylandBackend):
            info["unsupported"] = [k for k, v in b.missing.items() if v]
        if isinstance(b, ScreenshotOnlyBackend):
            info["limits"] = b.reason
        if isinstance(b, MacBackend):
            info["permissions"] = {**b.permissions(), "granted_to": _mac_app()}
        return [text(json.dumps(info, indent=2))]
    if name == "Wait":
        time.sleep(max(0.0, min(float(a.get("seconds", 1)), 60.0)))
        return [text("Done")]
    if name in ("Click", "Move", "Drag") or (name == "Scroll" and "x" in a and "y" in a):
        x, y = d.to_screen(float(a["x"]), float(a["y"]))
        d.b.move(x, y)
    if name == "Click":
        clicks = max(1, min(int(a.get("clicks", 1)), 3))
        for i in range(clicks):
            d.b.button(a.get("button", "left"), True)
            d.b.button(a.get("button", "left"), False)
            if i + 1 < clicks:
                time.sleep(0.06)
        return [text(f"Clicked at {a['x']},{a['y']}")]
    if name == "Move":
        return [text(f"Moved to {a['x']},{a['y']}")]
    if name == "Drag":
        d.b.button("left", True)
        tx, ty = d.to_screen(float(a["to_x"]), float(a["to_y"]))
        sx, sy = d.to_screen(float(a["x"]), float(a["y"]))
        for i in range(1, 11):
            time.sleep(0.03)
            d.b.move(sx + (tx - sx) * i / 10, sy + (ty - sy) * i / 10)
        d.b.button("left", False)
        return [text(f"Dragged to {a['to_x']},{a['to_y']}")]
    if name == "Scroll":
        n = max(1, min(int(a.get("amount", 3)), 50))
        dx, dy = {"up": (0, -n), "down": (0, n), "left": (-n, 0), "right": (n, 0)}[a["direction"]]
        d.b.scroll(dx, dy)
        return [text(f"Scrolled {a['direction']} {n}")]
    if name == "Type":
        d.b.type_text(a["text"] + ("\n" if a.get("press_enter") else ""))
        return [text(f"Typed {len(a['text'])} characters")]
    if name == "Shortcut":
        for combo in a["keys"].split():
            d.b.combo(combo)
            time.sleep(0.03)
        return [text(f"Pressed {a['keys']}")]
    raise ValueError(f"unknown tool {name}")


# --- MCP over stdio -----------------------------------------------------------------------

def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    if method == "initialize":
        version = msg.get("params", {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "clud-desktop", "version": "1.0"},
                  "instructions": f"Computer use for the {'macOS' if sys.platform == 'darwin' else 'Linux'} "
                                  "desktop CLUD is plugged into. "
                                  "Take a Screenshot first; coordinates are screenshot pixels."}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            result = {"content": call_tool(p.get("name"), p.get("arguments") or {})}
        except Exception as e:
            log(traceback.format_exc())
            if isinstance(e, (WaylandError, OSError)):
                desktop.reset()  # reconnect next time, e.g. after the session restarted
            result = {"content": [text(f"Error: {e}")], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


if __name__ == "__main__":
    main()

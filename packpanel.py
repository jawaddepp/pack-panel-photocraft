#!/usr/bin/env python3
"""Pack Panel: an asset browser panel for PhotoCraft.

Browses a folder of texture packs (images, PSD templates, brushes, layer styles)
and places assets into the open PhotoCraft document over PhotoCraft's local,
token-protected control server. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import posixpath
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Pack Panel"
VERSION = "1.5.0"
HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"

PHOTOCRAFT_APP = Path("/Applications/PhotoCraft.app")
PHOTOCRAFT_CLI = PHOTOCRAFT_APP / "Contents/MacOS/photocraft-cli"

SUPPORT_DIR = Path(os.environ.get("PACK_PANEL_SUPPORT_DIR") or Path.home() / "Library/Application Support" / APP_NAME)
CACHE_DIR = Path(os.environ.get("PACK_PANEL_CACHE_DIR") or Path.home() / "Library/Caches" / APP_NAME / "thumbs")
CONFIG_FILE = SUPPORT_DIR / "config.json"
TOKEN_FILE = SUPPORT_DIR / "photocraft-control.token"
USER_FONTS = Path.home() / "Library/Fonts"

PANEL_PORT = int(os.environ.get("PACK_PANEL_PORT", "47821"))
CONTROL_PORT = int(os.environ.get("PACK_PANEL_CONTROL_PORT", "47820"))
IDLE_EXIT_SECONDS = 180  # quit when no panel window has checked in for this long

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif", ".bmp", ".tga", ".avif", ".svg"}
DOC_EXT = {".psd", ".psb", ".kra", ".ora", ".pcraft"}
FONT_EXT = {".otf", ".ttf", ".ttc", ".otc"}
IMPORT_EXT = {".abr": "brushes", ".pat": "patterns", ".asl": "styles", ".atn": "actions"}
THUMB_EXT = IMAGE_EXT | {".psd", ".psb"}
SKIP_DIRS = {"__MACOSX", ".git", ".svn", "_downloads"}
FORMAT_DIRS = {"png", "svg", "jpg", "jpeg", "psd", "tif", "tiff", "webp", "files", "assets", "images"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


# ---------------------------------------------------------------- config ---

DEFAULT_CONFIG = {
    "assetsRoot": "",
    "fitOnPlace": True,
    "favorites": [],
    "recents": [],
    "launchedRoot": "",
    "imported": {},
    "inspoUrl": "",
    "storeUrl": "",
    "autoUpdate": True,
    "autoActivate": True,
    "store": {},
}
_config_lock = threading.RLock()


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text("utf-8"))
        if isinstance(data, dict):
            return {**DEFAULT_CONFIG, **data}
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


CONFIG = load_config()


def update_config(**changes) -> dict:
    with _config_lock:
        CONFIG.update(changes)
        save_config(CONFIG)
        return dict(CONFIG)


def mutate_config(fn) -> dict:
    """Read-modify-write the config atomically: fn(cfg) edits a deep copy, which is then saved."""
    with _config_lock:
        cfg = json.loads(json.dumps(CONFIG))
        fn(cfg)
        CONFIG.clear()
        CONFIG.update(cfg)
        save_config(CONFIG)
        return dict(CONFIG)


# --------------------------------------------------------------- library ---

def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def asset_kind(ext: str) -> str | None:
    if ext in IMAGE_EXT:
        return "image"
    if ext in DOC_EXT:
        return "document"
    if ext in FONT_EXT:
        return "font"
    if ext in IMPORT_EXT:
        return IMPORT_EXT[ext]
    return None


def assets_root() -> Path | None:
    root = CONFIG.get("assetsRoot") or ""
    if not root:
        return None
    p = Path(root).expanduser()
    return p if p.is_dir() else None


def resolve_asset(rel: str) -> Path:
    """Map a library-relative path to a real file, refusing anything outside the root."""
    root = assets_root()
    if root is None:
        raise PanelError("Choose your assets folder first.")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise PanelError("That file is no longer in your assets folder. Refresh the library.")
    return target


def scan_library() -> dict:
    root = assets_root()
    if root is None:
        return {"root": CONFIG.get("assetsRoot") or "", "packs": [], "rootMissing": bool(CONFIG.get("assetsRoot"))}
    packs: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted((d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")), key=natural_key)
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        banner = None
        assets = []
        for name in sorted(filenames, key=natural_key):
            if name in SKIP_FILES or name.startswith("._") or name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            kind = asset_kind(ext)
            if kind is None:
                continue
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if kind == "image" and os.path.splitext(name)[0].lower() == "header":
                banner = rel
                continue
            assets.append({
                "rel": rel,
                "name": os.path.splitext(name)[0],
                "ext": ext[1:],
                "kind": kind,
                "thumb": ext in THUMB_EXT,
            })
        if not assets:
            continue
        if banner is None:
            banner = next((a["rel"] for a in assets if a["thumb"]), None)
        parts = Path(rel_dir).parts if rel_dir else (root.name,)
        name, parent = parts[-1], parts[:-1]
        if name.lower() in FORMAT_DIRS and parent:  # "Guilloche Patterns/SVG" -> "Guilloche Patterns (SVG)"
            name, parent = f"{parent[-1]} ({name.upper()})", parent[:-1]
        packs[rel_dir] = {
            "id": rel_dir or ".",
            "name": name,
            "parent": " / ".join(parent),
            "banner": banner,
            "count": len(assets),
            "assets": assets,
        }
    ordered = sorted(packs.values(), key=lambda p: natural_key(p["id"]))
    return {"root": str(root), "packs": ordered, "rootMissing": False}


# ------------------------------------------------------------ thumbnails ---

_thumb_slots = threading.BoundedSemaphore(4)


ALPHA_EXT = {".png", ".tif", ".tiff", ".webp", ".gif", ".psd", ".psb", ".svg", ".tga", ".avif"}


def image_size(path: Path) -> tuple[int, int] | None:
    out = subprocess.run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                         capture_output=True, text=True, timeout=30)
    w = re.search(r"pixelWidth:\s*(\d+)", out.stdout)
    h = re.search(r"pixelHeight:\s*(\d+)", out.stdout)
    return (int(w.group(1)), int(h.group(1))) if w and h else None


def thumbnail(rel: str, size: int, background: bool = False, mode: str = "fill") -> Path:
    """A cached, sharp preview.

    mode "fill": the SHORT side is `size` px, so square tiles that crop the image are
    never stretched (sharp on Retina). mode "fit": the LONG side is `size` px, for the
    large preview. Never enlarges past the original. Formats that can be transparent
    keep their transparency (PNG); others are high-quality JPEG.
    """
    src = resolve_asset(rel)
    st = src.stat()
    fmt = "png" if src.suffix.lower() in ALPHA_EXT else "jpeg"
    ext = "png" if fmt == "png" else "jpg"
    mode = "fit" if mode == "fit" else "fill"
    key = hashlib.sha1(f"v2|{src}|{st.st_mtime_ns}|{st.st_size}|{size}|{fmt}|{mode}".encode()).hexdigest()
    out = CACHE_DIR / key[:2] / f"{key}.{ext}"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{secrets.token_hex(4)}.{ext}")
    with _thumb_slots:
        if out.exists():
            return out
        dims = image_size(src)
        resize: list[str] = []
        if dims:
            w, h = dims
            if mode == "fill":
                short = min(w, h)
                if short > size:
                    resize = ["--resampleHeight", str(size)] if h <= w else ["--resampleWidth", str(size)]
            elif max(w, h) > size:
                resize = ["-Z", str(size)]
        else:
            resize = ["-Z", str(size)]
        cmd = (["/usr/bin/nice", "-n", "15"] if background else []) + ["/usr/bin/sips", *resize, "-s", "format", fmt]
        if fmt == "jpeg":
            cmd += ["-s", "formatOptions", "92"]
        cmd += [str(src), "--out", str(tmp)]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            raise
        if proc.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise PanelError("No preview for this file.")
        os.replace(tmp, out)
    return out


def warm_thumbnails() -> None:
    """Build pack banners and tile previews in the background so browsing feels instant."""
    time.sleep(5)
    seen_root = None
    while True:
        try:
            lib = scan_library()
            if lib["packs"] and lib["root"] != seen_root:
                for pack in lib["packs"]:
                    if pack["banner"]:
                        try:
                            thumbnail(pack["banner"], 300, background=True)
                        except (PanelError, subprocess.TimeoutExpired):
                            pass
                for pack in lib["packs"]:
                    for a in pack["assets"]:
                        if a["thumb"]:
                            try:
                                thumbnail(a["rel"], 480, background=True)
                            except (PanelError, subprocess.TimeoutExpired):
                                pass
                seen_root = lib["root"]
        except Exception:
            pass
        time.sleep(300)


# ------------------------------------------------------ PhotoCraft bridge ---

class PanelError(Exception):
    """An error with a message meant for the person using the panel."""
    extra: dict = {}


class NeedsMemoryApproval(PanelError):
    """PhotoCraft wants explicit approval before opening a file this large."""
    def __init__(self, message: str, needed: int, soft: int, hard: int):
        super().__init__(message)
        self.extra = {"needsApproval": True, "neededGB": round(needed / 1073741824, 1),
                      "ramGB": round(int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                                                        capture_output=True, text=True).stdout or 0) / 1073741824)}


def memory_error(e: PanelError, name: str) -> PanelError:
    """Turn PhotoCraft's raw memory-limit refusal into something the person can act on."""
    msg = str(e)
    m = re.search(r"estimated (\d+) bytes.*?soft limit (\d+) bytes, hard limit (\d+) bytes", msg)
    if "explicit approval required" in msg and m:
        needed, soft, hard = (int(x) for x in m.groups())
        return NeedsMemoryApproval(f"{name} needs about {needed / 1073741824:.1f} GB of memory to open in PhotoCraft.",
                                   needed, soft, hard)
    if "hard limit" in msg or "exceeds" in msg and "memory" in msg:
        return PanelError(f"{name} is larger than PhotoCraft's memory limit. Raise “Per-document RAM limit” in "
                          "PhotoCraft › Settings › Performance, then try again.")
    return e


def open_in_photocraft(path: Path, approve: bool = False) -> None:
    params = {"path": str(path)}
    if approve:
        params["approveHighMemory"] = True
    try:
        BRIDGE.control("app.open", params, timeout=900)
    except PanelError as e:
        raise memory_error(e, path.stem) from None


class PhotoCraftBridge:
    """Talks to a running PhotoCraft through `photocraft-cli mcp --bridge`.

    The CLI performs PhotoCraft's control-token handshake; we speak MCP JSON-RPC
    to it over stdio and use its `control_call` tool for raw control methods.
    """

    def __init__(self, port: int, token_file: Path):
        self.port = port
        self.token_file = token_file
        self.proc: subprocess.Popen | None = None
        self.buf = b""
        self.next_id = 0
        self.lock = threading.RLock()

    # -- process plumbing
    def _start(self) -> None:
        self.close()
        self.proc = subprocess.Popen(
            [str(PHOTOCRAFT_CLI), "mcp", "--bridge", str(self.port), "--control-token-file", str(self.token_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.buf = b""
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pack-panel", "version": VERSION},
        }, timeout=15)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        self.proc = None

    def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _readline(self, timeout: float) -> bytes:
        assert self.proc and self.proc.stdout
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise ConnectionError("bridge closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def _rpc(self, method: str, params: dict, timeout: float = 120) -> dict:
        self.next_id += 1
        want = self.next_id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        while True:
            msg = json.loads(self._readline(timeout))
            if msg.get("id") == want:
                if "error" in msg:
                    raise PanelError(msg["error"].get("message", "PhotoCraft refused the request."))
                return msg["result"]

    # -- public API
    def tool(self, name: str, arguments: dict, timeout: float = 120):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.proc is None or self.proc.poll() is not None:
                        self._start()
                    result = self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout)
                    break
                except TimeoutError:  # must come first: TimeoutError is an OSError, and retrying would repeat the action
                    self.close()
                    raise PanelError("PhotoCraft took too long to answer. It may be busy with a large file.")
                except (ConnectionError, BrokenPipeError, OSError, ValueError):
                    self.close()
                    if attempt == 2:
                        raise PanelError("PhotoCraft isn't connected. Open it from the panel and try again.")
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        data = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        if result.get("isError") or (isinstance(data, dict) and set(data) == {"error"}):
            message = data.get("error") if isinstance(data, dict) else str(data)
            raise PanelError(friendly_error(str(message)))
        return data

    def control(self, method: str, params: dict | None = None, timeout: float = 120):
        return self.tool("control_call", {"method": method, "params": params or {}}, timeout)

    def execute(self, command: str, params: dict | None = None, timeout: float = 120):
        return self.control("engine.execute", {"command": command, "params": params or {}}, timeout)

    def menu(self, item_id: str):
        return self.tool("ui_menu_invoke", {"id": item_id})


def friendly_error(message: str) -> str:
    message = re.sub(r"^app:\s*", "", message.strip())
    if "automation read root" in message or "filesystem access is not granted" in message:
        return "PhotoCraft can't read this folder yet. Use Reconnect PhotoCraft so it gets access to your assets folder."
    if "modal dialog is open" in message:
        return "PhotoCraft has a dialog open. Finish it in PhotoCraft, then try again."
    if "authentication" in message or "not authenticated" in message:
        return "PhotoCraft refused the panel's key. Use Reconnect PhotoCraft."
    return message[:1].upper() + message[1:] if message else "PhotoCraft refused the request."


BRIDGE = PhotoCraftBridge(CONTROL_PORT, TOKEN_FILE)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def photocraft_running() -> bool:
    out = subprocess.run(["/usr/bin/pgrep", "-x", "PhotoCraft"], capture_output=True)
    return out.returncode == 0


def connection_state() -> dict:
    installed = PHOTOCRAFT_CLI.exists()
    running = installed and photocraft_running()
    listening = running and port_open(CONTROL_PORT)
    root = str(assets_root() or "")
    state = "connected" if listening else ("running-unlinked" if running else ("closed" if installed else "missing"))
    stale = listening and root and CONFIG.get("launchedRoot") != root
    return {"state": state, "needsRelaunch": bool(stale)}


def launch_photocraft() -> None:
    root = assets_root()
    if root is None:
        raise PanelError("Choose your assets folder first.")
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SUPPORT_DIR, 0o700)
    if TOKEN_FILE.exists():
        os.chmod(TOKEN_FILE, 0o600)
    subprocess.run([
        "/usr/bin/open", "-a", str(PHOTOCRAFT_APP), "--args",
        "--control", str(CONTROL_PORT),
        "--control-token-file", str(TOKEN_FILE),
        "--automation-read-root", str(root),
    ], check=True)
    update_config(launchedRoot=str(root))
    for _ in range(60):
        if port_open(CONTROL_PORT) and TOKEN_FILE.exists():
            BRIDGE.close()
            return
        time.sleep(0.5)
    raise PanelError("PhotoCraft opened but didn't accept the panel's connection. Quit PhotoCraft and try again.")


def quit_photocraft() -> None:
    """Ask PhotoCraft to quit normally, so it can prompt to save open work."""
    BRIDGE.close()
    subprocess.run(["/usr/bin/osascript", "-e", 'tell application id "ai.storyteller.photocraft" to quit'],
                   capture_output=True, timeout=120)
    for _ in range(240):
        if not photocraft_running():
            return
        time.sleep(0.5)
    raise PanelError("PhotoCraft is still open. Save or close your documents, quit it, then press Connect.")


# --------------------------------------------------------------- actions ---

def session() -> dict:
    return BRIDGE.control("ui.inspect")["session"]


def activate_document(index: int) -> None:
    if session()["active"] != index:
        BRIDGE.menu(f"window.document.{index}")


def place_asset(path: Path, fit: bool, approve: bool = False) -> str:
    """Place a file into the active document as a centred Smart Object.

    Mirrors Photoshop's Place Embedded: the image's full frame is centred on the
    canvas, the layer is named after the file and lands just above the selected
    layer, images larger than the canvas are scaled down to fit ("Resize Image
    During Place"), and the whole place is one step in the target's History.
    All preparation happens in a temporary document, so the target document
    only receives the finished layer.
    """
    before = session()
    docs = before["documents"]
    if not docs:
        open_in_photocraft(path, approve)
        return "opened"
    target = before["active"]
    tw, th = docs[target]["width"], docs[target]["height"]

    open_in_photocraft(path, approve)
    opened = BRIDGE.control("ui.inspect")
    tmp = opened["session"]["active"]
    if len(opened["session"]["documents"]) != len(docs) + 1:
        raise PanelError("PhotoCraft didn't open the file as a separate document.")
    target_now = target if tmp > target else target + 1
    try:
        tmp_doc = opened["session"]["documents"][tmp]
        iw, ih = tmp_doc["width"], tmp_doc["height"]
        layers = opened["document"]["layers"]
        if len(layers) == 1 and layers[0].get("kind") == "Smart Object":
            source_layer = layers[0]["id"]
        elif len(layers) == 1:
            source_layer = BRIDGE.execute("layer.smartObjects.convertToSmartObject", {"layer": layers[0]["id"]})["layer"]
        else:
            BRIDGE.execute("select.allLayers")
            source_layer = BRIDGE.execute("layer.smartObjects.convertToSmartObject")["layer"]
        if fit and (iw > tw or ih > th):
            scale = min(tw / iw, th / ih) * 100
            BRIDGE.execute("edit.transform", {
                "layer": source_layer, "scaleX": scale, "scaleY": scale, "reference": [iw / 2, ih / 2],
            })
        BRIDGE.execute("layer.copyToDocument", {
            "document": target_now,
            "source": tmp,
            "layers": [source_layer],
            "offset": [round((tw - iw) / 2), round((th - ih) / 2)],
            "name": path.stem,
        })
    finally:
        BRIDGE.execute("file.close", {"document": tmp, "save": "discard"})
    activate_document(target)
    return "placed"


def _custom_brush_count() -> int:
    presets = BRIDGE.execute("brush.presets.list").get("presets", [])
    return sum(1 for p in presets if not p.get("builtin"))


def _dialogs() -> list:
    return BRIDGE.control("ui.inspect").get("dialogs") or []


def _settle(check=None, seconds: float = 90) -> dict | None:
    """PhotoCraft finishes preset imports just after `app.open` returns: wait for it.

    Returns the first dialog PhotoCraft opens, else None once `check()` (if given)
    reports a stable non-zero value or the wait runs out.
    """
    deadline = time.monotonic() + seconds
    last, stable = None, 0
    time.sleep(0.4)
    while time.monotonic() < deadline:
        dialogs = _dialogs()
        if dialogs:
            return dialogs[0]
        if check is None:
            return None
        value = check()
        if value == last:
            stable += 1
            if (value and stable >= 2) or (not value and stable >= 6):
                return None
        else:
            stable = 0
        last = value
        time.sleep(0.6)
    return None


PANEL_FOR = {"brushes": "Brushes", "styles": "Styles", "actions": "Actions", "patterns": "Patterns"}
_style_cache = {"at": 0.0, "groups": set()}


def style_groups(max_age: float = 20, wait: bool = True) -> set:
    if time.monotonic() - _style_cache["at"] > max_age:
        if not wait and not BRIDGE.lock.acquire(blocking=False):
            return _style_cache["groups"]  # PhotoCraft is busy: use what we know
        try:
            data = BRIDGE.execute("style.presets.list")
            _style_cache["groups"] = {g.get("name", "") for g in data.get("groups", [])}
            _style_cache["at"] = time.monotonic()
        finally:
            if not wait:
                BRIDGE.lock.release()
    return _style_cache["groups"]


def mark_imported(rel: str, kind: str, count: int | None = None) -> None:
    def fn(cfg):
        cfg.setdefault("imported", {})[rel] = {"kind": kind, "count": count, "at": int(time.time())}
    mutate_config(fn)


def installed_rels() -> list:
    """Preset files already in PhotoCraft: ones the panel imported, plus styles PhotoCraft lists."""
    rels = set(CONFIG.get("imported") or {})
    if connection_state()["state"] == "connected":
        try:
            groups = style_groups(wait=False)
            for pack in scan_library()["packs"]:
                for a in pack["assets"]:
                    if a["kind"] == "styles" and Path(a["rel"]).stem in groups:
                        rels.add(a["rel"])
        except PanelError:
            pass
    return sorted(rels)


def already_message(path: Path, kind: str) -> str:
    return f"{path.stem} is already in PhotoCraft, in the {PANEL_FOR[kind]} panel"


PANEL_MENU = {"Brushes": "window.panel.brushes", "Styles": "window.panel.styles",
              "Actions": "window.panel.actions", "Patterns": "window.panel.patterns"}


def show_in_photocraft(panel: str) -> str:
    """Bring PhotoCraft forward with the given panel open (and the Brush tool for brushes)."""
    if panel not in PANEL_MENU:
        raise PanelError("Unknown panel.")
    if connection_state()["state"] != "connected":
        raise PanelError("PhotoCraft isn't connected. Open it from the panel first.")
    menu_id = PANEL_MENU[panel]
    items = BRIDGE.control("ui.menu.list")
    open_now = next((m.get("checked") for m in items if m.get("id") == menu_id), False)
    if not open_now:  # the menu item toggles: only open it if it's closed
        BRIDGE.menu(menu_id)
    if panel == "Brushes":
        try:
            BRIDGE.control("ui.set", {"tool": "Brush"})
        except PanelError:
            pass
    subprocess.run(["/usr/bin/open", "-b", "ai.storyteller.photocraft"], check=False)
    return {"Brushes": "Brushes panel is open in PhotoCraft, with the Brush tool selected",
            "Styles": "Styles panel is open in PhotoCraft. Select a layer, then click a style to apply it",
            "Actions": "Actions panel is open in PhotoCraft",
            "Patterns": "Patterns panel is open in PhotoCraft"}[panel]


def import_preset(path: Path, kind: str, rel: str, force: bool = False) -> str:
    """Import brushes, styles, actions or patterns.

    PhotoCraft's `app.open` reads preset files straight from the packs folder
    (the granted read root), so there is no size limit; the bridge's raw-data
    route is capped at 1 MB per request, which large .abr files exceed.
    Files already imported are not re-imported unless `force` is set.
    """
    if kind == "patterns":
        try:
            BRIDGE.control("app.open", {"path": str(path)})
            mark_imported(rel, kind)
            return f"Imported the patterns in {path.stem}"
        except PanelError:
            # PhotoCraft 0.23 refuses automated pattern imports: hand off to its Patterns panel.
            BRIDGE.menu("window.panel.patterns")
            subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)
            return ("PhotoCraft only imports patterns itself. In the Patterns panel, choose Import Patterns… "
                    "and pick the file highlighted in Finder")

    if not force:
        if rel in (CONFIG.get("imported") or {}):
            return already_message(path, kind)
        if kind == "styles" and path.stem in style_groups(max_age=0):
            mark_imported(rel, kind)
            return already_message(path, kind)

    if kind == "brushes":
        before = _custom_brush_count()
        BRIDGE.control("app.open", {"path": str(path)}, timeout=600)
        dialog = _settle(lambda: _custom_brush_count() - before, seconds=180)
        if dialog:
            fields = dialog.get("fields") or {}
            if fields.get("__presetGroupClash") and not force:
                # PhotoCraft says this group is already installed: answer for the person.
                BRIDGE.control("ui.dialog.cancel", {"dialog": dialog["id"]})
                mark_imported(rel, kind)
                return already_message(path, kind)
            return (f"PhotoCraft is asking how to import {path.stem}. Choose Replace to update the brushes, "
                    "or Cancel to keep the ones you have")
        added = _custom_brush_count() - before
        mark_imported(rel, kind, max(added, 0))
        if added <= 0:
            return already_message(path, kind)
        return f"Imported {added} brushes from {path.stem}. Find them in the Brushes panel"

    if kind in ("styles", "actions"):
        BRIDGE.control("app.open", {"path": str(path)}, timeout=300)
        dialog = _settle(seconds=2)
        _style_cache["at"] = 0.0
        if dialog:
            return f"PhotoCraft is asking how to import {path.stem}. Choose an option in PhotoCraft"
        mark_imported(rel, kind)
        return f"Imported {path.stem}. Find it in the {PANEL_FOR[kind]} panel"
    raise PanelError("PhotoCraft can't import this kind of file.")


def install_font(path: Path) -> str:
    USER_FONTS.mkdir(parents=True, exist_ok=True)
    dest = USER_FONTS / path.name
    if dest.exists():
        return f"{path.stem} is already installed"
    shutil.copy2(path, dest)
    return f"Installed {path.stem}. Restart PhotoCraft to see it in the font list"


def remember_recent(rel: str) -> None:
    def fn(cfg):
        cfg["recents"] = [rel, *[r for r in cfg.get("recents", []) if r != rel]][:40]
    mutate_config(fn)


def run_action(rel: str, action: str, approve: bool = False) -> str:
    path = resolve_asset(rel)
    kind = asset_kind(path.suffix.lower())
    if action == "reveal":
        subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)
        return "Shown in Finder"
    if action == "install":
        if kind != "font":
            raise PanelError("Only font files can be installed.")
        message = install_font(path)
        remember_recent(rel)
        return message
    if connection_state()["state"] != "connected":
        raise PanelError("PhotoCraft isn't connected. Press Connect first.")
    if action == "place":
        if kind not in ("image", "document"):
            raise PanelError("This file can't be placed. Try importing it instead.")
        outcome = place_asset(path, bool(CONFIG.get("fitOnPlace", True)), approve)
        where = f" from {path.parent.name}" if path.parent != assets_root() else ""
        message = f"Opened {path.stem}" if outcome == "opened" else f"Placed {path.stem}{where}"
    elif action == "open":
        open_in_photocraft(path, approve)
        message = f"Opened {path.stem}"
    elif action in ("import", "reimport"):
        if kind not in IMPORT_EXT.values():
            raise PanelError("This file isn't a brush, pattern, style or action set.")
        message = import_preset(path, kind, rel, force=action == "reimport")
    else:
        raise PanelError("Unknown action.")
    remember_recent(rel)
    return message


def choose_zips() -> list[str]:
    script = ('set picked to choose file with prompt "Choose pack .zip files to add" of type {"zip", "public.zip-archive"} '
              'with multiple selections allowed\n'
              'set out to ""\nrepeat with f in picked\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out')
    out = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=600)
    return [line for line in out.stdout.splitlines() if line.strip()]


def safe_name(name: str) -> str:
    """A folder name that can't escape or replace the packs folder ('.', '..', hidden names)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().lstrip(".").strip()
    return cleaned or "Pack"


def extract_zip_to(zpath: str, target: Path) -> int:
    """Unpack a .zip into `target`, flattening a single top folder; replaces `target` only on success."""
    if target.name in ("", ".", "..") or target.resolve().parent != target.parent.resolve():
        raise PanelError("That pack name can't be used as a folder name.")
    tmp = target.with_name("." + target.name + ".adding")  # hidden while it fills, so it never shows as a pack
    shutil.rmtree(tmp, ignore_errors=True)
    count = 0
    try:
        with zipfile.ZipFile(zpath) as z:
            members = [m for m in z.infolist() if not m.is_dir()
                       and not m.filename.startswith("__MACOSX/") and "/._" not in "/" + m.filename
                       and not m.filename.endswith(".DS_Store")]
            tops = {m.filename.split("/", 1)[0] for m in members}
            strip = tops.pop() + "/" if len(tops) == 1 and all("/" in m.filename for m in members) else ""
            for m in members:
                rel = posixpath.normpath(m.filename[len(strip):] if strip else m.filename)
                if rel.startswith("../") or rel.startswith("/") or rel in ("..", "."):
                    continue
                out = tmp / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                count += 1
    except zipfile.BadZipFile:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PanelError(f"{Path(zpath).name} isn't a readable .zip file.")
    except Exception as e:  # encrypted or Deflate64 zips, full disk, name clashes
        shutil.rmtree(tmp, ignore_errors=True)
        name = Path(zpath).name
        if isinstance(e, RuntimeError) and "encrypt" in str(e).lower():
            raise PanelError(f"{name} is password-protected. Unzip it in Finder, then add the folder to your packs folder.")
        if isinstance(e, NotImplementedError):
            raise PanelError(f"{name} uses a compression type Pack Panel can't read. Unzip it in Finder instead.")
        raise PanelError(f"Couldn't unpack {name}: {e}")
    if count == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PanelError(f"{Path(zpath).name} has no files in it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    os.replace(tmp, target)
    return count


def add_pack_zip(zpath: str) -> str:
    """Unpack one pack .zip into the packs folder as <zip name>/."""
    root = assets_root()
    if root is None:
        raise PanelError("Choose your packs folder first.")
    target = root / safe_name(Path(zpath).stem)
    count = extract_zip_to(zpath, target)
    return f"{target.name} ({count} files)"


def choose_folder() -> str | None:
    script = 'POSIX path of (choose folder with prompt "Choose the folder you unpacked your packs into")'
    out = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=600)
    path = out.stdout.strip()
    return path or None


# ----------------------------------------------------------------- store ---
# Signs in to the shop's download portal with the buyer's own account (email +
# emailed code, or order number), lists the packs that account owns, downloads
# missing ones into the packs folder and keeps them updated. It uses only the
# portal's own download flow, one file at a time, and backs off when the
# portal's daily download limit is reached.

STORE_TOKEN_FILE = SUPPORT_DIR / "store-session.json"
STORE_LISTING_FILE = SUPPORT_DIR / "store-listing.json"   # last pack list, so covers show instantly at launch
STORE_CHECK_EVERY = 6 * 3600
STORE_PAUSE = 6 * 3600
_store_lock = threading.RLock()
STORE_RUNTIME = {"listing": None, "queue": [], "current": None, "errors": {}, "message": "",
                 "wake": threading.Event(), "completed": 0}


class StoreError(PanelError):
    def __init__(self, message: str, status: int = 0, reason: str | None = None):
        super().__init__(message)
        self.status, self.reason = status, reason


def store_cfg() -> dict:
    return dict(CONFIG.get("store") or {})


def store_save(**changes) -> dict:
    def fn(cfg):
        cfg.setdefault("store", {}).update(changes)
    return mutate_config(fn).get("store", {})


def store_record_installed(item_id: str, record: dict) -> None:
    mutate_config(lambda cfg: cfg.setdefault("store", {}).setdefault("installed", {}).__setitem__(item_id, record))


def store_base() -> str:
    url = (CONFIG.get("storeUrl") or "").strip().rstrip("/")
    return url if url.startswith("https://") else ""


def store_session() -> dict | None:
    try:
        data = json.loads(STORE_TOKEN_FILE.read_text("utf-8"))
        if data.get("token") and data.get("base") == store_base():
            return data
    except (OSError, ValueError):
        pass
    return None


def store_save_session(token: str, email: str) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SUPPORT_DIR, 0o700)
    tmp = STORE_TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "email": email, "base": store_base()}, f)
    os.replace(tmp, STORE_TOKEN_FILE)


def store_forget_session() -> None:
    STORE_TOKEN_FILE.unlink(missing_ok=True)
    STORE_LISTING_FILE.unlink(missing_ok=True)
    with _store_lock:
        STORE_RUNTIME["queue"].clear()
        STORE_RUNTIME["listing"] = None
        STORE_RUNTIME["errors"].clear()


def store_api(path: str, body: dict | None = None, token: str | None = None, timeout: float = 30) -> dict:
    base = store_base()
    if not base:
        raise StoreError("No store address is set. Add it in Settings.")
    headers = {"User-Agent": f"PackPanel/{VERSION}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise StoreError("Can't reach the store right now. Check your internet connection and try again.")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = {}
    if status >= 400 or payload.get("ok") is False:
        raise StoreError(payload.get("error") or f"The store refused the request ({status}).", status, payload.get("reason"))
    return payload


def store_token() -> str:
    session = store_session()
    if not session:
        raise StoreError("Sign in to the store first.", 401)
    return session["token"]


def store_call(path: str, body: dict | None = None, timeout: float = 30) -> dict:
    token = store_token()  # "Sign in to the store first." when there is no session
    try:
        return store_api(path, body, token, timeout)
    except StoreError as e:
        if e.status == 401:
            store_forget_session()
            raise StoreError("Your store sign-in expired. Sign in again.", 401)
        raise


def store_kind(item_id: str, group: str) -> str:
    if item_id.startswith("brush:"):
        return "brushes"
    return {"packs": "pack", "styles": "style", "templates": "template"}[group]


def store_target(item: dict, root: Path) -> Path:
    if item["kind"] == "style":
        return root / "Layer Styles" / safe_name(item["name"])
    if item["kind"] == "template":
        return root / "Templates"
    name = safe_name(item["name"])
    if name.lower() in ("templates", "layer styles"):  # never replace the shared folders
        name += " Pack"
    return root / name


def normalize_listing(data: dict) -> list[dict]:
    base = store_base()
    items = []
    for group in ("packs", "styles", "templates"):
        for p in data.get(group) or []:
            if not p.get("id") or not p.get("name"):
                continue
            cover = p.get("cover") or p.get("banner")
            if cover and cover.startswith("/"):
                cover = base + cover
            items.append({
                "id": str(p["id"]), "name": str(p["name"]), "kind": store_kind(str(p["id"]), group),
                "files": p.get("files"), "bytes": p.get("downloadBytes") or p.get("bytes") or 0,
                "updated": p.get("updated") or 0, "added": p.get("added") or 0,
                "isNew": bool(p.get("isNew")), "ready": p.get("ready", True) is not False,
                "cover": cover if isinstance(cover, str) and cover.startswith("https://") else None,
            })
    return items


def seed_installed(items: list[dict]) -> None:
    """Treat packs already in the folder as installed at the store's current version."""
    root = assets_root()
    if root is None:
        return
    cfg = store_cfg()
    installed = dict(cfg.get("installed") or {})
    changed = False
    for it in items:
        if it["id"] in installed:
            continue
        target = store_target(it, root)
        found = None
        if it["kind"] == "template":
            if target.is_dir():
                for f in target.iterdir():
                    if f.is_file() and it["bytes"] and f.stat().st_size == it["bytes"]:
                        found = str(f.relative_to(root))
        elif target.is_dir() and any(target.rglob("*")):
            found = str(target.relative_to(root))
        if found:
            installed[it["id"]] = {"updated": it["updated"], "bytes": it["bytes"], "path": found, "at": int(time.time())}
            changed = True
    if changed:
        new = {k: v for k, v in installed.items() if k not in (store_cfg().get("installed") or {})}
        def fn(cfg):
            inst = cfg.setdefault("store", {}).setdefault("installed", {})
            for k, v in new.items():
                inst.setdefault(k, v)
        mutate_config(fn)


def item_status(it: dict) -> str:
    rt = STORE_RUNTIME
    cur = rt["current"]
    if cur and cur.get("id") == it["id"]:
        return "downloading"
    if it["id"] in rt["queue"]:
        return "queued"
    inst = (store_cfg().get("installed") or {}).get(it["id"])
    root = assets_root()
    if inst and root is not None and (root / inst.get("path", "")).exists():
        if (it["updated"] or 0) > (inst.get("updated") or 0) or (it["bytes"] and inst.get("bytes") and it["bytes"] != inst["bytes"]):
            return "update"
        return "installed"
    if it["id"] in rt["errors"]:
        return "failed"
    return "missing"


def store_check(auto: bool = True) -> list[dict]:
    data = store_call("/api/packs")
    items = normalize_listing(data)
    with _store_lock:
        STORE_RUNTIME["listing"] = items
    try:
        tmp = STORE_LISTING_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"base": store_base(), "items": items}), "utf-8")
        os.replace(tmp, STORE_LISTING_FILE)
    except OSError:
        pass
    seed_installed(items)
    cfg = store_cfg()
    known = set(cfg.get("known") or [])
    store_save(known=sorted({it["id"] for it in items}), lastCheck=int(time.time()), email=data.get("email") or cfg.get("email"))
    if auto and CONFIG.get("autoUpdate", True):
        wanted = []
        for it in items:
            if not it["ready"]:
                continue
            status = item_status(it)
            if status == "update" or (status == "missing" and known and it["id"] not in known):
                wanted.append(it["id"])
        store_enqueue(wanted)
    return items


def store_enqueue(ids: list[str]) -> int:
    if not store_session():
        raise StoreError("Sign in to your store account to download.", 401)
    added = 0
    with _store_lock:
        listing = {it["id"]: it for it in (STORE_RUNTIME["listing"] or [])}
        for i in ids:
            cur = STORE_RUNTIME["current"]
            if i in listing and i not in STORE_RUNTIME["queue"] and not (cur and cur.get("id") == i):
                STORE_RUNTIME["queue"].append(i)
                STORE_RUNTIME["errors"].pop(i, None)
                added += 1
    if added:
        STORE_RUNTIME["wake"].set()
    return added


def _filename_from_url(url: str, fallback: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    cd = (q.get("response-content-disposition") or [""])[0]
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="([^"]+)"', cd)
    name = urllib.parse.unquote(m.group(1)) if m else urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    return os.path.basename(name) or fallback


def store_download_one(it: dict) -> None:
    root = assets_root()
    if root is None:
        raise StoreError("Choose your packs folder first.")
    link = store_call("/api/packs/download", {"id": it["id"]})["url"]
    if not str(link).startswith("https://"):
        raise StoreError("The store sent an unexpected download link.")
    name = _filename_from_url(link, safe_name(it["name"]) + ".zip")
    work = root / ".packpanel-downloads"
    work.mkdir(exist_ok=True)
    part = work / (safe_name(it["id"]) + ".part")
    req = urllib.request.Request(link, headers={"User-Agent": f"PackPanel/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or it["bytes"] or 0)
            done = 0
            with _store_lock:
                STORE_RUNTIME["current"].update(total=total, done=0)
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                STORE_RUNTIME["current"]["done"] = done
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        part.unlink(missing_ok=True)
        raise StoreError(f"The download of {it['name']} was interrupted. It will try again later.") from e
    target = store_target(it, root)
    if it["kind"] == "template" or not name.lower().endswith(".zip"):
        target.mkdir(parents=True, exist_ok=True)
        dest = target / name
        os.replace(part, dest)
        rel = str(dest.relative_to(root))
    else:
        try:
            extract_zip_to(str(part), target)
        finally:
            part.unlink(missing_ok=True)
        rel = str(target.relative_to(root))
    store_record_installed(it["id"], {"updated": it["updated"], "bytes": it["bytes"], "path": rel, "at": int(time.time())})
    def prune(cfg):  # updated brushes/styles: let them be imported again
        imported = cfg.get("imported") or {}
        cfg["imported"] = {k: v for k, v in imported.items() if not k.startswith(rel + "/")}
    mutate_config(prune)


def store_worker() -> None:
    rt = STORE_RUNTIME
    while True:
        rt["wake"].wait(timeout=60)
        rt["wake"].clear()
        while True:
            paused = (store_cfg().get("pausedUntil") or 0) > time.time()
            with _store_lock:
                if paused or not rt["queue"] or not store_session():
                    break
                item_id = rt["queue"].pop(0)
                it = next((x for x in (rt["listing"] or []) if x["id"] == item_id), None)
                if it is None:
                    continue
                rt["current"] = {"id": it["id"], "name": it["name"], "done": 0, "total": it["bytes"] or 0}
            try:
                store_download_one(it)
                rt["completed"] += 1
                rt["message"] = f"Downloaded {it['name']}"
                if it["kind"] in ("brushes", "style") and CONFIG.get("autoActivate", True):
                    try:
                        r = activate_presets([it["id"]])
                        if r["added"]:
                            rt["message"] = f"Downloaded {it['name']} and added it to PhotoCraft"
                    except PanelError:
                        rt["message"] = f"Downloaded {it['name']}. Open PhotoCraft from the panel to add it there."
            except StoreError as e:
                if e.status == 429:
                    with _store_lock:
                        rt["queue"].insert(0, item_id)
                    store_save(pausedUntil=int(time.time()) + STORE_PAUSE)
                    rt["message"] = f"{e} Pack Panel will try again later."
                elif e.status == 401:
                    rt["message"] = str(e)
                else:
                    rt["errors"][item_id] = str(e)
                    rt["message"] = str(e)
            except PanelError as e:
                rt["errors"][item_id] = str(e)
                rt["message"] = str(e)
            except Exception as e:  # keep the worker alive
                rt["errors"][item_id] = f"Couldn't install {it['name']}: {e}"
                rt["message"] = rt["errors"][item_id]
            finally:
                with _store_lock:
                    rt["current"] = None


def load_cached_listing() -> None:
    try:
        data = json.loads(STORE_LISTING_FILE.read_text("utf-8"))
        if data.get("base") == store_base() and store_session() and STORE_RUNTIME["listing"] is None:
            STORE_RUNTIME["listing"] = data.get("items") or None
    except (OSError, ValueError):
        pass


def store_scheduler() -> None:
    load_cached_listing()
    time.sleep(1.5)
    while True:
        try:
            cfg = store_cfg()
            if store_base() and store_session():
                if time.time() - (cfg.get("lastCheck") or 0) > STORE_CHECK_EVERY or STORE_RUNTIME["listing"] is None:
                    store_check(auto=True)
                if (cfg.get("pausedUntil") or 0) and cfg["pausedUntil"] <= time.time():
                    store_save(pausedUntil=0)
                    STORE_RUNTIME["wake"].set()
        except PanelError as e:
            STORE_RUNTIME["message"] = str(e)
        except Exception:
            pass
        time.sleep(15 * 60)


def annotate_library(lib: dict) -> dict:
    """Link local packs and templates to their store item, so the panel can show the shop's cover art."""
    installed = store_cfg().get("installed") or {}
    by_path = {v.get("path"): k for k, v in installed.items() if v.get("path")}
    for pack in lib.get("packs", []):
        pid = pack["id"]
        sid = by_path.get(pid) or next((k for path, k in by_path.items() if path and pid.startswith(path + "/")), None)
        if sid:
            pack["storeId"] = sid
        for a in pack["assets"]:
            if a["kind"] == "document" and a["rel"] in by_path:
                a["storeId"] = by_path[a["rel"]]
    return lib


def preset_files(rel_dir: str) -> list[str]:
    root = assets_root()
    if root is None:
        return []
    base = root / rel_dir
    out = []
    for f in sorted(base.rglob("*")) if base.is_dir() else []:
        if f.is_file() and f.suffix.lower() in (".abr", ".asl", ".atn") and not f.name.startswith("."):
            out.append(str(f.relative_to(root)))
    return out


def activate_presets(ids: list[str] | None = None) -> dict:
    """Add downloaded brushes, layer styles and actions to PhotoCraft (skips ones already added)."""
    if connection_state()["state"] != "connected":
        raise PanelError("PhotoCraft isn't connected. Open it from the panel, then try again.")
    installed = store_cfg().get("installed") or {}
    listing = {it["id"]: it for it in (STORE_RUNTIME["listing"] or [])}
    rels = []
    for sid, rec in installed.items():
        if ids is not None and sid not in ids:
            continue
        kind = (listing.get(sid) or {}).get("kind")
        if kind in ("brushes", "style") or (kind is None and (sid.startswith("brush:") or sid.startswith("style:"))):
            rels.extend(preset_files(rec.get("path", "")))
    added, already, failed = [], [], []
    for rel in rels:
        if rel in (CONFIG.get("imported") or {}):
            already.append(rel)
            continue
        try:
            msg = run_action(rel, "import")
            (already if "already in PhotoCraft" in msg else added).append(rel)
        except PanelError as e:
            failed.append(f"{Path(rel).stem}: {e}")
    return {"added": len(added), "already": len(already), "failed": failed}


def store_state() -> dict:
    rt = STORE_RUNTIME
    cfg = store_cfg()
    session = store_session()
    items = []
    for it in rt["listing"] or []:
        items.append({**{k: it[k] for k in ("id", "name", "kind", "files", "bytes", "updated", "added", "isNew", "ready")},
                      "hasCover": bool(it.get("cover")), "status": item_status(it), "error": rt["errors"].get(it["id"])})
    paused_until = cfg.get("pausedUntil") or 0
    return {
        "configured": bool(store_base()),
        "signedIn": bool(session),
        "email": (session or {}).get("email") or cfg.get("email") or "",
        "autoUpdate": bool(CONFIG.get("autoUpdate", True)),
        "autoActivate": bool(CONFIG.get("autoActivate", True)),
        "lastCheck": cfg.get("lastCheck") or 0,
        "pausedUntil": paused_until if paused_until > time.time() else 0,
        "current": dict(cur) if (cur := rt["current"]) else None,
        "queue": list(rt["queue"]),
        "completed": rt["completed"],
        "message": rt["message"],
        "items": items,
        "updates": sum(1 for i in items if i["status"] == "update"),
        "missing": sum(1 for i in items if i["status"] == "missing"),
    }


def store_cover(item_id: str) -> Path:
    it = next((x for x in (STORE_RUNTIME["listing"] or []) if x["id"] == item_id), None)
    if not it or not it.get("cover"):
        raise PanelError("No cover.")
    key = hashlib.sha1(f"{it['cover']}|{it['updated']}".encode()).hexdigest()
    out = CACHE_DIR / "covers" / f"{key}.img"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(it["cover"], headers={"User-Agent": f"PackPanel/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(8 << 20)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PanelError("Cover unavailable.")
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


# ------------------------------------------------------------ web server ---

SESSION_TOKEN = secrets.token_urlsafe(24)
LAST_SEEN = [time.monotonic()]
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"PackPanel/{VERSION}"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- helpers
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{PANEL_PORT}", f"localhost:{PANEL_PORT}")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json", {"Cache-Control": "no-store"})

    def _authorised(self, query: dict) -> bool:
        token = self.headers.get("X-Panel-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(token, SESSION_TOKEN)

    # -- GET
    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # answer instead of dropping the connection
            try:
                self._json(500, {"error": f"Something went wrong: {e}"})
            except OSError:
                pass

    def _get(self):
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/api/ping":
            return self._json(200, {"app": APP_NAME, "version": VERSION})
        if url.path in ("/", "/index.html"):
            html = (UI_DIR / "index.html").read_text("utf-8").replace("__PANEL_TOKEN__", SESSION_TOKEN)
            return self._send(200, html.encode(), CONTENT_TYPES[".html"], {
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            })
        if url.path.startswith("/ui/"):
            name = url.path[4:]
            target = (UI_DIR / name).resolve()
            if not target.is_relative_to(UI_DIR) or not target.is_file():
                return self._send(404, b"Not found", "text/plain")
            return self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
                              {"Cache-Control": "no-cache"})
        if not self._authorised(query):
            return self._send(403, b"Forbidden", "text/plain")
        LAST_SEEN[0] = time.monotonic()
        if url.path == "/api/state":
            return self._json(200, self._state())
        if url.path == "/api/library":
            return self._json(200, annotate_library(scan_library()))
        if url.path == "/thumb":
            try:
                size = max(64, min(int((query.get("s") or ["480"])[0]), 3000))
                file = thumbnail((query.get("p") or [""])[0], size, mode=(query.get("m") or ["fill"])[0])
            except (PanelError, ValueError, subprocess.TimeoutExpired):
                return self._send(404, b"No preview", "text/plain")
            return self._send(200, file.read_bytes(), "image/png" if file.suffix == ".png" else "image/jpeg",
                              {"Cache-Control": "private, max-age=86400"})
        if url.path == "/api/store":
            return self._json(200, store_state())
        if url.path == "/cover":
            try:
                file = store_cover((query.get("id") or [""])[0])
            except PanelError:
                return self._send(404, b"No cover", "text/plain")
            data = file.read_bytes()
            ctype = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
            return self._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})
        return self._send(404, b"Not found", "text/plain")

    # -- POST
    def do_POST(self):
        if not self._host_ok() or not self._authorised({}):
            return self._send(403, b"Forbidden", "text/plain")
        LAST_SEEN[0] = time.monotonic()
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "Bad request."})
        path = urllib.parse.urlsplit(self.path).path
        try:
            result = self._post(path, body)
        except PanelError as e:
            return self._json(409, {"error": str(e), **(e.extra or {})})
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": "That took too long. Try again."})
        except Exception as e:  # surface unexpected failures instead of hanging the UI
            return self._json(500, {"error": f"Something went wrong: {e}"})
        return self._json(200, result)

    def _post(self, path: str, body: dict):
        if path == "/api/action":
            rel, action = str(body.get("rel", "")), str(body.get("action", ""))
            message = run_action(rel, action, approve=bool(body.get("approve")))
            extra = {}
            kind = asset_kind(Path(rel).suffix.lower())
            if action in ("import", "reimport") and kind in PANEL_FOR and "Patterns panel, choose" not in message:
                extra["reveal"] = {"panel": PANEL_FOR[kind], "group": Path(rel).stem,
                                   "where": f"Window › {PANEL_FOR[kind]}"}
            if action == "place" and message.startswith("Placed"):
                extra["undo"] = True
            return {"message": message, "state": self._state(), **extra}
        if path == "/api/show":
            return {"message": show_in_photocraft(str(body.get("panel", ""))), "state": self._state()}
        if path == "/api/undo":
            if connection_state()["state"] != "connected":
                raise PanelError("PhotoCraft isn't connected.")
            BRIDGE.execute("edit.undo")
            return {"message": "Undone in PhotoCraft", "state": self._state()}
        if path == "/api/connect":
            state = connection_state()["state"]
            if state == "missing":
                raise PanelError("PhotoCraft isn't installed in /Applications.")
            if state == "running-unlinked":
                raise PanelError("PhotoCraft is already open without the panel link. Use Reconnect PhotoCraft.")
            if state == "closed":
                launch_photocraft()
            return self._state()
        if path == "/api/relaunch":
            if photocraft_running():
                quit_photocraft()
            launch_photocraft()
            return self._state()
        if path == "/api/config":
            changes = {}
            if "fitOnPlace" in body:
                changes["fitOnPlace"] = bool(body["fitOnPlace"])
            if "assetsRoot" in body:
                root = Path(str(body["assetsRoot"])).expanduser()
                if not root.is_dir():
                    raise PanelError("That folder doesn't exist.")
                changes["assetsRoot"] = str(root.resolve())
            if "inspoUrl" in body:
                url = str(body["inspoUrl"]).strip()
                if url and not re.match(r"^https://[^\s]+$", url):
                    raise PanelError("The Inspo page must be an https:// address.")
                changes["inspoUrl"] = url
            if "storeUrl" in body:
                url = str(body["storeUrl"]).strip().rstrip("/")
                if url and not re.match(r"^https://[^\s/]+(/[^\s]*)?$", url):
                    raise PanelError("The store address must be an https:// address.")
                changes["storeUrl"] = url
                if url != (CONFIG.get("storeUrl") or ""):
                    STORE_RUNTIME["listing"] = None
            update_config(**changes)
            return self._state()
        if path == "/api/add-pack":
            added = [add_pack_zip(z) for z in choose_zips()]
            if not added:
                return {"message": "", "state": self._state()}
            return {"message": "Added " + ", ".join(added), "state": self._state()}
        if path == "/api/rebuild-previews":
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            return {"message": "Previews will rebuild as you scroll", "state": self._state()}
        if path == "/api/choose-folder":
            chosen = choose_folder()
            if chosen:
                update_config(assetsRoot=str(Path(chosen).resolve()))
            return self._state()
        if path == "/api/favorite":
            rel = str(body.get("rel", ""))
            on = bool(body.get("on"))
            def fn(cfg):
                favs = [f for f in cfg.get("favorites", []) if f != rel]
                if on:
                    favs.insert(0, rel)
                cfg["favorites"] = favs
            mutate_config(fn)
            return self._state()
        if path == "/api/heartbeat":
            return {"ok": True}
        if path.startswith("/api/store/"):
            return self._store_post(path[len("/api/store/"):], body)
        raise PanelError("Unknown request.")

    def _store_post(self, op: str, body: dict):
        email = str(body.get("email", "")).strip()
        if op == "start":
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                raise PanelError("Enter the email you used at checkout.")
            store_api("/api/auth/start", {"email": email})
            store_save(email=email)
            return {"message": f"Code sent to {email}. It expires in 10 minutes.", "store": store_state()}
        if op in ("verify", "order"):
            if op == "verify":
                code = re.sub(r"\D", "", str(body.get("code", "")))
                if len(code) != 6:
                    raise PanelError("Enter the 6-digit code from the email.")
                res = store_api("/api/auth/verify", {"email": email, "code": code})
            else:
                order = re.sub(r"[^0-9A-Za-z-]", "", str(body.get("orderNumber", "")).lstrip("#"))
                if not order:
                    raise PanelError("Enter your order number.")
                res = store_api("/api/auth/order", {"email": email, "orderNumber": order})
            if not res.get("token"):
                raise PanelError("The store didn't sign you in. Try again.")
            store_save_session(res["token"], res.get("email") or email)
            store_save(email=res.get("email") or email)
            try:
                store_check(auto=True)
            except PanelError:
                pass
            return {"message": "Signed in", "store": store_state()}
        if op == "signout":
            store_forget_session()
            return {"message": "Signed out of the store", "store": store_state()}
        if op == "check":
            store_check(auto=True)
            st = store_state()
            msg = (f"{st['updates']} update{'s' if st['updates'] != 1 else ''} available" if st["updates"]
                   else "Everything is up to date")
            return {"message": msg, "store": st}
        if op == "download":
            ids = [str(i) for i in (body.get("ids") or [])][:100]
            if body.get("all") == "missing":
                ids = [i["id"] for i in store_state()["items"] if i["status"] in ("missing", "failed") and i["ready"]]
            elif body.get("all") == "updates":
                ids = [i["id"] for i in store_state()["items"] if i["status"] == "update"]
            elif body.get("all") == "everything":
                ids = [i["id"] for i in store_state()["items"] if i["status"] in ("missing", "failed", "update") and i["ready"]]
            n = store_enqueue(ids)
            if (store_cfg().get("pausedUntil") or 0) > time.time():
                msg = "The store's daily download limit was reached. Downloads continue automatically later."
            else:
                msg = f"Downloading {n} item{'s' if n != 1 else ''}" if n else "Nothing new to download"
            return {"message": msg, "store": store_state()}
        if op == "activate":
            r = activate_presets()
            bits = []
            if r["added"]:
                bits.append(f"Added {r['added']} brush and style file{'s' if r['added'] != 1 else ''} to PhotoCraft")
            if r["already"] and not r["added"]:
                bits.append("Your brushes and layer styles are already in PhotoCraft")
            if r["failed"]:
                bits.append(f"{len(r['failed'])} couldn't be added: {r['failed'][0]}")
            return {"message": ". ".join(bits) or "No brushes or layer styles to add yet", "store": store_state()}
        if op == "auto-activate":
            update_config(autoActivate=bool(body.get("on")))
            return {"message": "Brushes and styles will be added automatically" if CONFIG["autoActivate"]
                    else "Brushes and styles won't be added automatically", "store": store_state()}
        if op == "auto":
            update_config(autoUpdate=bool(body.get("on")))
            return {"message": "Automatic updates on" if CONFIG["autoUpdate"] else "Automatic updates off", "store": store_state()}
        raise PanelError("Unknown store request.")

    @staticmethod
    def _state() -> dict:
        return {
            "version": VERSION,
            "config": {k: CONFIG.get(k) for k in ("assetsRoot", "fitOnPlace", "favorites", "recents", "inspoUrl")},
            "connection": connection_state(),
            "controlPort": CONTROL_PORT,
            "installed": installed_rels(),
            "store": {k: v for k, v in store_state().items() if k != "items"} | {"storeUrl": CONFIG.get("storeUrl") or ""},
        }


def open_panel_window(url: str) -> None:
    """Open the panel as a compact app-style window when a Chromium browser is available.

    Only used by the browser fallback (builds without the native Swift panel). If a
    panel window from an earlier launch is still open, it is reused instead of
    opening another one.
    """
    profile = SUPPORT_DIR / "window-profile"
    running = subprocess.run(["/usr/bin/pgrep", "-f", f"user-data-dir={profile}"], capture_output=True)
    if running.returncode == 0:
        return
    for app in ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium"):
        if Path(f"/Applications/{app}.app").exists():
            subprocess.Popen([
                "/usr/bin/open", "-na", app, "--args",
                f"--app={url}", "--window-size=460,900", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check",
            ])
            return
    webbrowser.open(url)


def idle_watchdog(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(15)
        if time.monotonic() - LAST_SEEN[0] > IDLE_EXIT_SECONDS:
            BRIDGE.close()
            server.shutdown()
            return


def main() -> int:
    no_window = "--no-window" in sys.argv
    stay = "--stay" in sys.argv
    if not PHOTOCRAFT_CLI.exists():
        print("PhotoCraft wasn't found in /Applications. Install it, then run Pack Panel again.", file=sys.stderr)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PANEL_PORT), Handler)
    except OSError:
        # Already running: just bring up another window.
        try:
            with socket.create_connection(("127.0.0.1", PANEL_PORT), timeout=1):
                pass
        except OSError:
            print(f"Port {PANEL_PORT} is busy. Set PACK_PANEL_PORT to another port.", file=sys.stderr)
            return 1
        if not no_window:
            open_panel_window(f"http://127.0.0.1:{PANEL_PORT}/")
        return 0
    url = f"http://127.0.0.1:{PANEL_PORT}/"
    print(f"{APP_NAME} {VERSION} running at {url}")
    if not stay:
        threading.Thread(target=idle_watchdog, args=(server,), daemon=True).start()
    threading.Thread(target=store_worker, daemon=True).start()
    threading.Thread(target=store_scheduler, daemon=True).start()
    threading.Thread(target=warm_thumbnails, daemon=True).start()
    if not no_window:
        open_panel_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        BRIDGE.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

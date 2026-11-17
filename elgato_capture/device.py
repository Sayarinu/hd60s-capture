"""Elgato Game Capture HD60 S discovery, signal detection and picture controls."""

import glob
import os
import re
import subprocess
from dataclasses import dataclass, field

import gi

gi.require_version("GUdev", "1.0")
from gi.repository import GLib, GUdev  # noqa: E402

VIDEO_GLOB = "/dev/v4l/by-id/usb-Elgato_Game_Capture_HD60_S_*-video-index0"
AUDIO_PREFIX = "alsa_input.usb-Elgato_Game_Capture_HD60_S_"

CONTROL_LABELS = {
    "brightness": "Brightness",
    "contrast": "Contrast",
    "saturation": "Saturation",
    "hue": "Hue",
    "rx_rgb_quantization_range": "RGB range",
}


class DeviceTimeout(Exception):
    """The card did not answer a control request: its control endpoint is wedged."""


@dataclass
class Timings:
    width: int
    height: int
    fps: float
    interlaced: bool

    def describe(self):
        scan = "i" if self.interlaced else "p"
        return f"{self.width}×{self.height}{scan} @ {self.fps:.2f} fps".replace(".00 fps", " fps")


@dataclass
class Control:
    name: str
    kind: str
    minimum: int
    maximum: int
    step: int
    default: int
    value: int
    menu: dict = field(default_factory=dict)

    @property
    def label(self):
        return CONTROL_LABELS.get(self.name, self.name.replace("_", " ").capitalize())


def _run(args, timeout=3):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeviceTimeout(" ".join(args)) from exc
    except FileNotFoundError:
        return None


def find_video():
    paths = sorted(glob.glob(VIDEO_GLOB))
    return paths[0] if paths else None


def find_audio_node():
    result = _run(["pactl", "list", "short", "sources"])
    if not result or result.returncode:
        return None
    for line in result.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) > 1 and cols[1].startswith(AUDIO_PREFIX) and not cols[1].endswith(".monitor"):
            return cols[1]
    return None


def usb_interface(video_path):
    """USB interface id such as '1-9:1.0', matching the driver's kernel log prefix."""
    node = os.path.basename(os.path.realpath(video_path))
    try:
        return os.path.basename(os.path.realpath(f"/sys/class/video4linux/{node}/device"))
    except OSError:
        return None


def card_frozen(interface):
    """True if the driver logged a wedged control endpoint since this interface last became ready."""
    if not interface:
        return False
    result = _run(["journalctl", "-k", "-b", "-n", "400", "-o", "cat", "--no-pager"], timeout=5)
    if not result or result.returncode:
        return False
    frozen = False
    prefix = f"hd60s {interface}:"
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        if "ready: /dev/video" in line:
            frozen = False
        elif "EP0 is stuck" in line or "EP0 wedged" in line:
            frozen = True
    return frozen


def query_timings(video_path):
    """Current HDMI input timings, or None when there is no signal. Raises DeviceTimeout."""
    result = _run(["v4l2-ctl", "-d", video_path, "--query-dv-timings"])
    if not result or result.returncode:
        return None
    text = result.stdout

    def num(pattern):
        match = re.search(pattern, text)
        return int(match.group(1)) if match else 0

    width = num(r"Active width:\s*(\d+)")
    height = num(r"Active height:\s*(\d+)")
    if not width or not height:
        return None
    fps_match = re.search(r"\(([\d.]+) frames per second\)", text)
    fps = float(fps_match.group(1)) if fps_match else 0.0
    return Timings(width, height, fps, "Frame format: interlaced" in text)


def lock_timings(video_path):
    """Make the card capture in the mode the source is currently sending."""
    _run(["v4l2-ctl", "-d", video_path, "--set-dv-bt-timings", "query"])
    _run(["v4l2-ctl", "-d", video_path, "--set-audio-input=0"])


_CTRL_RE = re.compile(
    r"^\s*(\w+) 0x[0-9a-f]+ \((\w+)\)\s*: min=(-?\d+) max=(-?\d+)"
    r"(?: step=(\d+))? default=(-?\d+) value=(-?\d+)"
)
_MENU_RE = re.compile(r"^\s+(\d+): (.+)$")


def list_controls(video_path):
    result = _run(["v4l2-ctl", "-d", video_path, "--list-ctrls-menus"])
    if not result or result.returncode:
        return []
    controls = []
    for line in result.stdout.splitlines():
        match = _CTRL_RE.match(line)
        if match:
            name, kind, lo, hi, step, default, value = match.groups()
            controls.append(
                Control(name, kind, int(lo), int(hi), int(step or 1), int(default), int(value))
            )
            continue
        match = _MENU_RE.match(line)
        if match and controls and controls[-1].kind == "menu":
            controls[-1].menu[int(match.group(1))] = match.group(2).strip()
    return [c for c in controls if c.kind in ("int", "menu")]


def set_control(video_path, name, value):
    _run(["v4l2-ctl", "-d", video_path, "-c", f"{name}={int(value)}"])


class Watcher:
    """Calls `callback()` (debounced, on the main loop) when capture devices come or go."""

    def __init__(self, callback):
        self._callback = callback
        self._pending = 0
        self._client = GUdev.Client.new(["video4linux", "sound", "usb"])
        self._client.connect("uevent", self._on_uevent)

    def _on_uevent(self, _client, action, dev):
        if action not in ("add", "remove", "change", "bind", "unbind"):
            return
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(600, self._fire)

    def _fire(self):
        self._pending = 0
        self._callback()
        return GLib.SOURCE_REMOVE

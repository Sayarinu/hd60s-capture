import logging
import signal

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_NAME, ICON_NAME  # noqa: E402
from .settings import Settings  # noqa: E402

APP_ID = "studio.nouvellelune.ElgatoCapture"


class CaptureApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name(ICON_NAME)
        self.window = None
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)
        # App-level actions so desktop global shortcuts can drive a running instance:
        #   gapplication action studio.nouvellelune.ElgatoCapture screenshot
        for name, window_action in (("screenshot", "screenshot"), ("record", "record"),
                                    ("mute", "mute")):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._forward, window_action)
            self.add_action(action)

    def _forward(self, _action, _param, window_action):
        if self.window is not None:
            self.window.activate_action(f"win.{window_action}", None)

    def do_activate(self):
        # Single instance: a second launch lands here in the running process.
        if self.window is None:
            from .window import CaptureWindow

            self.window = CaptureWindow(self, Settings())
        self.window.present()

    def _on_signal(self):
        if self.window is not None:
            self.window.close()
        else:
            self.quit()
        return GLib.SOURCE_REMOVE


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return CaptureApp().run(argv)

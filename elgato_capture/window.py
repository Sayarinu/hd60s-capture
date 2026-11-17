import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import device, pipeline  # noqa: E402
from .pipeline import BUSY, FROZEN, LIVE, NO_DEVICE, NO_SIGNAL, STARTING  # noqa: E402

CSS = b"""
.video-area { background: black; }
.osd-info {
  background: alpha(black, 0.6); color: white; border-radius: 8px;
  padding: 6px 10px; font-feature-settings: "tnum";
}
.rec-badge {
  background: alpha(black, 0.6); color: white; border-radius: 8px;
  padding: 4px 10px; font-weight: bold; font-feature-settings: "tnum";
}
.rec-dot { color: #ff3b30; }
"""

STATUS = {
    NO_DEVICE: (
        "media-removable-symbolic",
        "Capture card not connected",
        "Connect the Elgato Game Capture HD60 S with its USB cable.",
    ),
    NO_SIGNAL: (
        "video-display-symbolic",
        "No HDMI signal",
        "Check that your HDMI source is turned on and connected to HDMI IN on the capture card.",
    ),
    STARTING: (None, "Starting capture…", "Waiting for the first video frame."),
    BUSY: (
        "action-unavailable-symbolic",
        "Capture card is in use",
        "Another app (such as OBS) is using the HD60 S. Close it and the preview will start.",
    ),
    FROZEN: (
        "dialog-warning-symbolic",
        "Capture card stopped responding",
        "Unplug the HD60 S USB cable, wait 10 seconds, then plug it back in. "
        "The preview will resume on its own.",
    ),
}


class CaptureWindow(Adw.ApplicationWindow):
    def __init__(self, app, settings):
        super().__init__(application=app, title=pipeline.APP_NAME)
        self.settings = settings
        self.set_default_size(settings["width"], settings["height"])
        self.set_icon_name(pipeline.ICON_NAME)
        self._hide_bars_id = 0
        self._rec_timer = 0
        self._syncing_volume = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.capture = pipeline.Capture(settings)
        self._build_ui()
        self._install_actions(app)

        self.capture.connect("state-changed", self._on_state)
        self.capture.connect("paintable-changed", self._on_paintable)
        self.capture.connect("stats", self._on_stats)
        self.capture.connect("volume-changed", self._on_volume_changed)
        self.capture.connect("recording-changed", self._on_recording_changed)
        self.capture.connect("recording-finished", self._on_recording_finished)
        self.capture.connect("screenshot-done", self._on_screenshot_done)
        self.capture.connect("notice", lambda _c, text: self._toast(text))
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

        self._on_state(self.capture, self.capture.state)
        if not pipeline.HAVE_GTK4_SINK:
            GLib.timeout_add_seconds(
                2,
                lambda: self._toast("Software rendering: install gst-plugin-gtk4 for GPU video")
                and False,
            )

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        header = Adw.HeaderBar()

        self.record_button = Gtk.ToggleButton(icon_name="media-record-symbolic")
        self.record_button.set_tooltip_text("Record (F9)")
        self.record_button.set_action_name("win.record")
        header.pack_start(self.record_button)

        shot = Gtk.Button(icon_name="camera-photo-symbolic", tooltip_text="Screenshot (F12)")
        shot.set_action_name("win.screenshot")
        header.pack_start(shot)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Picture Settings…", "win.picture")
        section.append("Show Signal Info", "win.info")
        section.append("Fullscreen", "win.fullscreen")
        menu.append_section(None, section)
        folders = Gio.Menu()
        folders.append("Open Screenshots Folder", "win.open-screenshots")
        folders.append("Open Recordings Folder", "win.open-recordings")
        menu.append_section(None, folders)
        about = Gio.Menu()
        about.append("Keyboard Shortcuts", "win.shortcuts")
        menu.append_section(None, about)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.set_tooltip_text("Menu")
        header.pack_end(menu_button)

        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.volume.set_draw_value(False)
        self.volume.set_size_request(130, -1)
        self.volume.set_value(self.settings["volume"])
        self.volume.set_tooltip_text("Capture volume")
        self.volume.connect("value-changed", self._on_volume_slider)
        self.mute_button = Gtk.ToggleButton()
        self.mute_button.set_tooltip_text("Mute (M)")
        self.mute_button.set_action_name("win.mute")
        volume_box = Gtk.Box(spacing=2)
        volume_box.append(self.mute_button)
        volume_box.append(self.volume)
        header.pack_end(volume_box)

        # Video page
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.add_css_class("video-area")
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)

        self.info_label = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.START,
                                    margin_start=12, margin_top=12, xalign=0)
        self.info_label.add_css_class("osd-info")
        self.info_label.set_visible(self.settings["show_info"])

        self.rec_dot = Gtk.Label(label="●")
        self.rec_dot.add_css_class("rec-dot")
        self.rec_time = Gtk.Label(label="REC 00:00")
        self.rec_badge = Gtk.Box(spacing=6, halign=Gtk.Align.END, valign=Gtk.Align.START,
                                 margin_end=12, margin_top=12, visible=False)
        self.rec_badge.add_css_class("rec-badge")
        self.rec_badge.append(self.rec_dot)
        self.rec_badge.append(self.rec_time)

        video = Gtk.Overlay(child=self.picture)
        video.add_css_class("video-area")
        video.add_overlay(self.info_label)
        video.add_overlay(self.rec_badge)

        double_click = Gtk.GestureClick()
        double_click.connect("pressed", self._on_video_pressed)
        video.add_controller(double_click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        video.add_controller(motion)

        # Status page
        self.status = Adw.StatusPage()
        self.spinner = Adw.SpinnerPaintable.new(self.status)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.status, "status")
        self.stack.add_named(video, "video")

        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.toolbar = Adw.ToolbarView(content=self.toasts)
        self.toolbar.add_top_bar(header)
        self.set_content(self.toolbar)
        self._update_mute_icon()

    def _install_actions(self, app):
        def add(name, callback, accels=(), state=None):
            if state is None:
                action = Gio.SimpleAction.new(name, None)
            else:
                action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", state))
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                app.set_accels_for_action(f"win.{name}", list(accels))
            return action

        self.record_action = add("record", lambda *_: self.toggle_recording(), ["F9"],
                                 state=False)
        self.shot_action = add("screenshot", lambda *_: self.capture.screenshot(), ["F12"])
        add("fullscreen", lambda *_: self.toggle_fullscreen(), ["F11", "f"])
        add("unfullscreen", lambda *_: self.unfullscreen(), ["Escape"])
        self.mute_action = add("mute", lambda *_: self.toggle_mute(), ["m"],
                               state=self.settings["muted"])
        self.info_action = add("info", lambda *_: self.toggle_info(), ["i"],
                               state=self.settings["show_info"])
        add("picture", lambda *_: self.open_picture_settings(), ["<Control>comma"])
        add("open-screenshots", lambda *_: self._open_folder(
            GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures"))
        add("open-recordings", lambda *_: self._open_folder(
            GLib.UserDirectory.DIRECTORY_VIDEOS, "Videos"))
        add("shortcuts", lambda *_: self._show_shortcuts(), ["<Control>question"])
        self._update_live_actions()

    # --------------------------------------------------------------- state

    def _on_state(self, _capture, state):
        if state == LIVE:
            self.stack.set_visible_child_name("video")
            self.toolbar.set_reveal_top_bars(not self.is_fullscreen())
            if self.is_fullscreen():
                self._schedule_hide_bars()
        else:
            icon, title, description = STATUS[state]
            if icon:
                self.status.set_paintable(None)
                self.status.set_icon_name(icon)
            else:
                self.status.set_paintable(self.spinner)
            self.status.set_title(title)
            self.status.set_description(description)
            self.stack.set_visible_child_name("status")
            self.toolbar.set_reveal_top_bars(True)
            self.set_cursor(None)
        self._update_live_actions()

    def _update_live_actions(self):
        live = self.capture.state == LIVE
        self.shot_action.set_enabled(live)
        self.record_action.set_enabled(live or self.capture.recording is not None)

    def _on_paintable(self, _capture, paintable):
        self.picture.set_paintable(paintable)

    def _on_stats(self, _capture, stats):
        lines = [
            f"Input   {stats['signal']}",
            f"Capture {stats['fps']:.1f} fps ({stats['renderer']} rendering)",
            f"Audio   {stats['audio']}",
        ]
        self.info_label.set_label("\n".join(lines))

    # -------------------------------------------------------------- volume

    def _on_volume_slider(self, scale):
        if not self._syncing_volume:
            self.capture.set_volume(scale.get_value())

    def toggle_mute(self):
        muted = not self.settings["muted"]
        self.capture.set_muted(muted)
        self.mute_action.set_state(GLib.Variant("b", muted))
        self._update_mute_icon()

    def _on_volume_changed(self, _capture, volume, muted):
        self._syncing_volume = True
        self.volume.set_value(volume)
        self._syncing_volume = False
        self.mute_action.set_state(GLib.Variant("b", muted))
        self._update_mute_icon()

    def _update_mute_icon(self):
        muted = self.settings["muted"]
        self.mute_button.set_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        )

    # -------------------------------------------------- record / screenshot

    def toggle_recording(self):
        if self.capture.recording is not None:
            self.capture.stop_recording()
        else:
            self.capture.start_recording()

    def _on_recording_changed(self, _capture, active):
        self.record_action.set_state(GLib.Variant("b", active))
        self.rec_badge.set_visible(active)
        self.record_button.set_tooltip_text("Stop recording (F9)" if active else "Record (F9)")
        if active:
            self.record_button.add_css_class("destructive-action")
            self._tick_recording()
            self._rec_timer = GLib.timeout_add(500, self._tick_recording)
        else:
            self.record_button.remove_css_class("destructive-action")
            if self._rec_timer:
                GLib.source_remove(self._rec_timer)
                self._rec_timer = 0
        self._update_live_actions()

    def _tick_recording(self):
        rec = self.capture.recording
        if rec is None:
            return GLib.SOURCE_REMOVE
        elapsed = int(time.monotonic() - rec.started)
        hours, rest = divmod(elapsed, 3600)
        stamp = f"{hours}:{rest // 60:02}:{rest % 60:02}" if hours else f"{rest // 60:02}:{rest % 60:02}"
        self.rec_time.set_label(f"REC {stamp}")
        self.rec_dot.set_opacity(1.0 if elapsed % 2 == 0 else 0.35)
        return GLib.SOURCE_CONTINUE

    def _on_recording_finished(self, _capture, path, message):
        self._toast(message, path if path else None)

    def _on_screenshot_done(self, _capture, path, error):
        if path:
            self._toast("Screenshot saved", path)
        else:
            self._toast(f"Screenshot failed: {error}")

    # ------------------------------------------------------------- display

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_):
        if self.is_fullscreen() and self.capture.state == LIVE:
            self._schedule_hide_bars()
        else:
            self.toolbar.set_reveal_top_bars(True)
            self.set_cursor(None)

    def _on_video_pressed(self, gesture, n_press, _x, _y):
        if n_press == 2:
            self.toggle_fullscreen()

    def _on_motion(self, *_):
        if self.is_fullscreen():
            self.toolbar.set_reveal_top_bars(True)
            self.set_cursor(None)
            self._schedule_hide_bars()

    def _schedule_hide_bars(self):
        if self._hide_bars_id:
            GLib.source_remove(self._hide_bars_id)
        self._hide_bars_id = GLib.timeout_add(2000, self._hide_bars)

    def _hide_bars(self):
        self._hide_bars_id = 0
        if self.is_fullscreen() and self.capture.state == LIVE:
            self.toolbar.set_reveal_top_bars(False)
            self.set_cursor(Gdk.Cursor.new_from_name("none"))
        return GLib.SOURCE_REMOVE

    def toggle_info(self):
        show = not self.settings["show_info"]
        self.settings["show_info"] = show
        self.info_label.set_visible(show)
        self.info_action.set_state(GLib.Variant("b", show))

    # ---------------------------------------------------- picture settings

    def open_picture_settings(self):
        video = self.capture.device
        if not video or self.capture.state == FROZEN:
            self._toast("Connect the capture card to adjust the picture")
            return

        def load():
            try:
                controls = device.list_controls(video)
            except device.DeviceTimeout:
                controls = []
            GLib.idle_add(self._show_picture_dialog, video, controls)

        threading.Thread(target=load, daemon=True).start()

    def _show_picture_dialog(self, video, controls):
        if not controls:
            self._toast("The capture card did not report any picture controls")
            return GLib.SOURCE_REMOVE
        saved = self.settings["controls"]

        def apply(name, value):
            saved[name] = int(value)
            threading.Thread(target=device.set_control, args=(video, name, value),
                             daemon=True).start()

        dialog = Adw.PreferencesDialog(title="Picture Settings")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Picture",
                                     description="Applied live and remembered for next time.")
        widgets = []
        for control in controls:
            if control.kind == "menu":
                labels = [control.menu.get(i, str(i))
                          for i in range(control.minimum, control.maximum + 1)]
                labels = [lbl.replace("RGB ", "").replace(" Range", "") for lbl in labels]
                row = Adw.ComboRow(title=control.label, model=Gtk.StringList.new(labels))
                row.set_selected(control.value - control.minimum)
                row.connect("notify::selected", lambda r, _p, c=control:
                            apply(c.name, c.minimum + r.get_selected()))
                widgets.append((control, lambda r=row, c=control:
                                r.set_selected(c.default - c.minimum)))
            else:
                row = Adw.ActionRow(title=control.label)
                scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, control.minimum,
                                                 control.maximum, control.step)
                scale.set_value(control.value)
                scale.set_hexpand(True)
                scale.set_size_request(240, -1)
                scale.set_draw_value(True)
                scale.set_value_pos(Gtk.PositionType.RIGHT)
                scale.set_digits(0)
                scale.add_mark(control.default, Gtk.PositionType.BOTTOM, None)
                scale.connect("value-changed", lambda s, c=control:
                              apply(c.name, round(s.get_value())))
                row.add_suffix(scale)
                widgets.append((control, lambda s=scale, c=control: s.set_value(c.default)))
            group.add(row)
        page.add(group)

        reset_group = Adw.PreferencesGroup()
        reset = Adw.ButtonRow(title="Reset to Defaults")
        reset.add_css_class("destructive-action")

        def on_reset(*_):
            for _control, reset_widget in widgets:
                reset_widget()
            saved.clear()

        reset.connect("activated", on_reset)
        reset_group.add(reset)
        page.add(reset_group)
        dialog.add(page)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------- helpers

    def _toast(self, text, path=None):
        toast = Adw.Toast(title=text, timeout=4)
        if path:
            toast.set_button_label("Open")
            toast.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
                Path(path).as_uri(), None))
        self.toasts.add_toast(toast)
        return False

    def _open_folder(self, kind, fallback):
        folder = pipeline._xdg_dir(kind, fallback)
        folder.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)

    def _show_shortcuts(self):
        dialog = Adw.AlertDialog(
            heading="Keyboard Shortcuts",
            body="F9\tStart / stop recording\n"
                 "F12\tScreenshot\n"
                 "F11 or F\tFullscreen (double-click works too)\n"
                 "Esc\tLeave fullscreen\n"
                 "M\tMute\n"
                 "I\tSignal info\n"
                 "Ctrl+,\tPicture settings",
        )
        dialog.add_response("close", "Close")
        dialog.present(self)

    def _on_close(self, *_):
        width, height = self.get_default_size()
        if not self.is_fullscreen() and not self.is_maximized():
            self.settings["width"], self.settings["height"] = width, height
        self.capture.shutdown()
        self.settings.save()
        return False

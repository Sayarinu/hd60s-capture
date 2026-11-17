"""Capture engine: device state machine, GStreamer pipeline, recording and screenshots."""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gst, GstVideo  # noqa: E402

from . import APP_NAME, ICON_NAME, device  # noqa: E402

Gst.init(None)
log = logging.getLogger("elgato-capture")

NO_DEVICE = "no-device"
NO_SIGNAL = "no-signal"
STARTING = "starting"
LIVE = "live"
FROZEN = "frozen"
BUSY = "busy"

FIRST_FRAME_TIMEOUT = 6.0
STALL_TIMEOUT = 4.0
TEARDOWN_TIMEOUT = 3.0

HAVE_GTK4_SINK = Gst.ElementFactory.find("gtk4paintablesink") is not None


def _xdg_dir(kind, fallback):
    path = GLib.get_user_special_dir(kind)
    return Path(path or Path.home() / fallback) / "Elgato"


def _make(factory, name=None, **props):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    for key, value in props.items():
        key = key.replace("_", "-")
        if isinstance(value, str) and element.find_property(key) and GObject.type_is_a(
            element.find_property(key).value_type, GObject.TYPE_ENUM
        ):
            Gst.util_set_object_arg(element, key, value)
        else:
            element.set_property(key, value)
    return element


def _link(*elements):
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise RuntimeError(f"Could not link {a.get_name()} → {b.get_name()}")


class _Recording:
    def __init__(self, path):
        self.path = path
        self.started = time.monotonic()
        self.elements = []
        self.tee_pads = []  # (tee, tee_pad, branch_sink_pad)
        self.done = threading.Event()
        self.finished = False
        self.reason = ""


class Capture(GObject.Object):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "paintable-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "stats": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "volume-changed": (GObject.SignalFlags.RUN_FIRST, None, (float, bool)),
        "recording-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "recording-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "screenshot-done": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "notice": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.state = NO_DEVICE
        self.device = None
        self.audio_node = None
        self.timings = None
        self.pipeline = None
        self._interface = None
        self._vtee = self._atee = self._snap = self._asink = None
        self._frames = 0
        self._last_frame = 0.0
        self._started_at = 0.0
        self._fps_mark = (0.0, 0)
        self._measured_fps = 0.0
        self._stalls = 0
        self._poll_busy = False
        self._rec = None
        self._pending_texture = False
        self._closing = False
        self._last_error = ""
        self._busy_ticks = 0
        self._retry_at = 0.0
        self._watcher = device.Watcher(self._on_hotplug)
        self._tick_id = GLib.timeout_add(1000, self._tick)
        GLib.idle_add(self._poll)

    # ---------------------------------------------------------------- state

    def _set_state(self, state):
        if state != self.state:
            log.info("state: %s → %s", self.state, state)
            self.state = state
            self.emit("state-changed", state)

    def _tick(self):
        if self._closing:
            return GLib.SOURCE_REMOVE
        if self.pipeline is not None:
            self._watchdog()
            self._sync_volume()
            self._emit_stats()
        elif self.state == BUSY:
            self._busy_ticks += 1
            if self._busy_ticks % 3 == 0:
                self._poll()
        else:
            self._poll()
        return GLib.SOURCE_CONTINUE

    def _on_hotplug(self):
        if self._closing:
            return
        present = device.find_video()
        if self.pipeline and (not present or not Path(self.device or "").exists()):
            log.info("capture card removed")
            self._stop("Card disconnected")
        if present and device.usb_interface(present) != self._interface:
            self._stalls = 0
        self._poll()

    def _poll(self):
        if self._poll_busy or self._closing or self.pipeline is not None:
            return GLib.SOURCE_REMOVE
        self._poll_busy = True
        threading.Thread(target=self._poll_worker, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _poll_worker(self):
        result = {"state": NO_DEVICE}
        try:
            video = device.find_video()
            if video:
                interface = device.usb_interface(video)
                result.update(video=video, interface=interface)
                if device.card_frozen(interface):
                    result["state"] = FROZEN
                else:
                    timings = device.query_timings(video)
                    if timings is None:
                        result["state"] = NO_SIGNAL
                    else:
                        device.lock_timings(video)
                        result.update(
                            state=STARTING, timings=timings, audio=device.find_audio_node()
                        )
        except device.DeviceTimeout:
            result["state"] = FROZEN
        except Exception:  # never let the poller die
            log.exception("device poll failed")
            result["state"] = NO_SIGNAL
        GLib.idle_add(self._poll_done, result)

    def _poll_done(self, result):
        self._poll_busy = False
        if self._closing or self.pipeline is not None:
            return GLib.SOURCE_REMOVE
        state = result["state"]
        if result.get("interface") != self._interface:
            self._stalls = 0
        self._interface = result.get("interface")
        self.device = result.get("video")
        if state == STARTING and self._stalls >= 2:
            state = FROZEN
        if state == STARTING and time.monotonic() < self._retry_at:
            return GLib.SOURCE_REMOVE  # back off after a failed start
        if state == STARTING:
            self._start(result["timings"], result.get("audio"))
        else:
            self._set_state(state)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------- pipeline

    def _start(self, timings, audio_node):
        self.timings = timings
        self.audio_node = audio_node
        try:
            self._build()
        except Exception as exc:
            log.exception("could not build pipeline")
            self._teardown_pipeline()
            self._set_state(NO_SIGNAL)
            self.emit("notice", f"Preview failed: {exc}")
            return
        self._frames = 0
        self._last_frame = 0.0
        self._started_at = time.monotonic()
        self._fps_mark = (self._started_at, 0)
        if self.state != BUSY:  # keep the "in use" screen up while retrying
            self._set_state(STARTING)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log.error("pipeline refused to start")
            self._stop("Could not start capture")

    def _build(self):
        pipeline = Gst.Pipeline.new("elgato-capture")
        self.pipeline = pipeline

        # Video: card → tee → (display, screenshot grabber, recorder)
        src = _make("v4l2src", "vsrc", device=self.device, do_timestamp=True)
        caps = _make("capsfilter", caps=Gst.Caps.from_string("video/x-raw,format=YUY2"))
        vq = _make("queue", leaky="downstream", max_size_buffers=3, max_size_bytes=0, max_size_time=0)
        self._vtee = _make("tee", "vtee", allow_not_linked=True)
        dq = _make("queue", leaky="downstream", max_size_buffers=2, max_size_bytes=0, max_size_time=0)
        display = self._make_display()
        sq = _make("queue", leaky="downstream", max_size_buffers=1, max_size_bytes=0, max_size_time=0)
        self._snap = _make("appsink", "snap", max_buffers=1, drop=True, sync=False, emit_signals=False)
        for element in (src, caps, vq, self._vtee, dq, display, sq, self._snap):
            pipeline.add(element)
        _link(src, caps, vq, self._vtee)
        _link(self._vtee, dq, display)
        _link(self._vtee, sq, self._snap)
        self._vtee.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_video_buffer)

        # Audio: only ever the card's own PipeWire node, never a fallback device.
        self._atee = self._asink = None
        if self.audio_node:
            asrc = _make(
                "pipewiresrc",
                "asrc",
                target_object=self.audio_node,
                client_name=APP_NAME,
                provide_clock=False,
                do_timestamp=True,  # same clock as the video, so recordings stay in sync
                on_disconnect="error",
                stream_properties=Gst.Structure.new_from_string(
                    "props,node.dont-fallback=true,node.dont-reconnect=true,"
                    "node.dont-move=true,media.name=\"Elgato capture input\""
                ),
            )
            aconv = _make("audioconvert")
            ares = _make("audioresample")
            acaps = _make(
                "capsfilter", caps=Gst.Caps.from_string("audio/x-raw,rate=48000,channels=2")
            )
            self._atee = _make("tee", "atee", allow_not_linked=True)
            aq = _make("queue", leaky="downstream", max_size_time=200 * Gst.MSECOND,
                       max_size_buffers=0, max_size_bytes=0)
            self._asink = _make(
                "pulsesink",
                "asink",
                client_name=APP_NAME,
                stream_properties=Gst.Structure.new_from_string(
                    f"props,application.icon_name={ICON_NAME},media.role=game,"
                    "media.name=\"Game Capture\""
                ),
                volume=float(self.settings["volume"]),
                mute=bool(self.settings["muted"]),
                buffer_time=40000,
                latency_time=10000,
                sync=False,  # play as it arrives, like a loopback; never drop "late" audio
            )
            for element in (asrc, aconv, ares, acaps, self._atee, aq, self._asink):
                pipeline.add(element)
            _link(asrc, aconv, ares, acaps, self._atee, aq, self._asink)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _make_display(self):
        if HAVE_GTK4_SINK:
            gtksink = _make("gtk4paintablesink", sync=False)
            paintable = gtksink.get_property("paintable")
            if paintable.get_property("gl-context") is not None:
                sink = _make("glsinkbin", sink=gtksink)
            else:
                sink = Gst.Bin.new("display")
                conv = _make("videoconvert", n_threads=4)
                sink.add(conv)
                sink.add(gtksink)
                _link(conv, gtksink)
                sink.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
            self.emit("paintable-changed", paintable)
            return sink

        # Fallback without gst-plugin-gtk4: hand RGB frames to GTK as textures.
        sink = Gst.Bin.new("display")
        conv = _make("videoconvert", n_threads=4)
        caps = _make("capsfilter", caps=Gst.Caps.from_string("video/x-raw,format=BGRx"))
        appsink = _make("appsink", max_buffers=1, drop=True, sync=False, emit_signals=True)
        appsink.connect("new-sample", self._on_display_sample)
        for element in (conv, caps, appsink):
            sink.add(element)
        _link(conv, caps, appsink)
        sink.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
        self.emit("paintable-changed", None)
        return sink

    def _on_display_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if sample is None or self._pending_texture:
            return Gst.FlowReturn.OK
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = GLib.Bytes.new(mapped.data)
        buffer.unmap(mapped)
        self._pending_texture = True

        def deliver():
            self._pending_texture = False
            if self.pipeline is not None:
                texture = Gdk.MemoryTexture.new(
                    info.width, info.height, Gdk.MemoryFormat.B8G8R8X8, data, info.stride[0]
                )
                self.emit("paintable-changed", texture)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)
        return Gst.FlowReturn.OK

    def _on_video_buffer(self, _pad, _info):
        self._frames += 1
        self._last_frame = time.monotonic()
        if self._frames == 1:
            GLib.idle_add(self._on_first_frame)
        return Gst.PadProbeReturn.OK

    def _on_first_frame(self):
        if self.pipeline is None or self.state == LIVE:
            return GLib.SOURCE_REMOVE
        self._stalls = 0
        self._set_state(LIVE)
        self._emit_stats()
        controls = dict(self.settings.get("controls", {}))
        video = self.device
        if controls and video:
            threading.Thread(
                target=lambda: [device.set_control(video, k, v) for k, v in controls.items()],
                daemon=True,
            ).start()
        return GLib.SOURCE_REMOVE

    def _on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.warning("pipeline error from %s: %s (%s)", message.src.get_name(), err.message, debug)
            self._last_error += f" {err.message} {debug or ''}".lower()
            if self.pipeline is not None:
                GLib.idle_add(self._stop, "Capture interrupted")
        elif message.type == Gst.MessageType.WARNING:
            err, _debug = message.parse_warning()
            log.info("pipeline warning: %s", err.message)
        elif message.type == Gst.MessageType.LATENCY and self.pipeline is not None:
            self.pipeline.recalculate_latency()

    def _watchdog(self):
        now = time.monotonic()
        if self.state != LIVE and now - self._started_at > FIRST_FRAME_TIMEOUT:
            stalled = True
        elif self.state == LIVE and now - self._last_frame > STALL_TIMEOUT:
            stalled = True
        else:
            stalled = False
        if stalled:
            self._stalls += 1
            log.warning("no video frames (stall %d)", self._stalls)
            self._stop("Video stopped")

    def _stop(self, reason="", *_):
        if self.pipeline is None and self._rec is None and not self._closing:
            return GLib.SOURCE_REMOVE
        if self._rec is not None:
            self.stop_recording(reason or "Recording saved", wait=True)
        hung = not self._teardown_pipeline()
        self._retry_at = time.monotonic() + 2.0
        if self._closing:
            return GLib.SOURCE_REMOVE
        busy = "busy" in self._last_error
        self._last_error = ""
        if hung or self._stalls >= 2:
            self._set_state(FROZEN)
        elif busy:
            self._busy_ticks = 0
            self._set_state(BUSY)
            return GLib.SOURCE_REMOVE
        else:
            self._set_state(NO_SIGNAL)
        GLib.timeout_add(500, self._poll)
        return GLib.SOURCE_REMOVE

    def _teardown_pipeline(self):
        pipeline, self.pipeline = self.pipeline, None
        self._vtee = self._atee = self._snap = self._asink = None
        self.emit("paintable-changed", None)
        if pipeline is None:
            return True
        bus = pipeline.get_bus()
        bus.remove_signal_watch()
        worker = threading.Thread(target=pipeline.set_state, args=(Gst.State.NULL,), daemon=True)
        worker.start()
        worker.join(TEARDOWN_TIMEOUT)
        if worker.is_alive():
            log.error("capture card did not release the stream; it is wedged")
            return False
        return True

    def shutdown(self):
        self._closing = True
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        self._stop("Recording saved")

    # --------------------------------------------------------------- stats

    def _emit_stats(self):
        now = time.monotonic()
        mark_time, mark_frames = self._fps_mark
        if now - mark_time >= 0.9:
            self._measured_fps = (self._frames - mark_frames) / (now - mark_time)
            self._fps_mark = (now, self._frames)
        self.emit(
            "stats",
            {
                "signal": self.timings.describe() if self.timings else "—",
                "fps": self._measured_fps,
                "audio": "48 kHz stereo" if self.audio_node else "no audio input",
                "renderer": "GPU" if HAVE_GTK4_SINK else "software",
            },
        )

    # -------------------------------------------------------------- volume

    def set_volume(self, volume):
        self.settings["volume"] = volume
        if self._asink is not None:
            self._asink.set_property("volume", volume)

    def set_muted(self, muted):
        self.settings["muted"] = muted
        if self._asink is not None:
            self._asink.set_property("mute", muted)

    def _sync_volume(self):
        """Pick up changes made in the desktop volume mixer."""
        if self._asink is None or self.state != LIVE:
            return
        volume = self._asink.get_property("volume")
        muted = self._asink.get_property("mute")
        if abs(volume - self.settings["volume"]) > 0.005 or muted != self.settings["muted"]:
            self.settings["volume"] = volume
            self.settings["muted"] = muted
            self.emit("volume-changed", volume, muted)

    # --------------------------------------------------------- screenshots

    def screenshot(self):
        snap = self._snap
        if snap is None or self.state != LIVE:
            self.emit("screenshot-done", "", "No video to capture")
            return

        def work():
            path, error = "", ""
            try:
                sample = snap.emit("try-pull-sample", 500 * Gst.MSECOND)
                if sample is None:
                    raise RuntimeError("no frame arrived")
                png = GstVideo.video_convert_sample(
                    sample, Gst.Caps.from_string("image/png"), 3 * Gst.SECOND
                )
                buffer = png.get_buffer()
                ok, mapped = buffer.map(Gst.MapFlags.READ)
                if not ok:
                    raise RuntimeError("could not read encoded image")
                try:
                    folder = _xdg_dir(GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures")
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
                    target.write_bytes(bytes(mapped.data))
                    path = str(target)
                finally:
                    buffer.unmap(mapped)
            except Exception as exc:
                log.exception("screenshot failed")
                error = str(exc) if not isinstance(exc, GLib.Error) else exc.message
            GLib.idle_add(lambda: self.emit("screenshot-done", path, error) and False)

        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------- recording

    @property
    def recording(self):
        return self._rec

    def start_recording(self):
        if self._rec is not None or self.state != LIVE or self.pipeline is None:
            return False
        folder = _xdg_dir(GLib.UserDirectory.DIRECTORY_VIDEOS, "Videos")
        folder.mkdir(parents=True, exist_ok=True)
        rec = _Recording(str(folder / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"))
        pipeline = self.pipeline
        try:
            mux = _make("mp4mux", fragment_duration=1000)
            fsink = _make("filesink", location=rec.path, sync=False)
            fsink.set_property("async", False)
            rec.elements += [mux, fsink]

            vq = _make("queue", leaky="downstream", max_size_time=2 * Gst.SECOND,
                       max_size_buffers=0, max_size_bytes=0)
            video_chain = [vq] + self._encoder_chain()
            rec.elements += video_chain

            audio_chain = []
            if self._atee is not None:
                aq = _make("queue", leaky="downstream", max_size_time=2 * Gst.SECOND,
                           max_size_buffers=0, max_size_bytes=0)
                audio_chain = [aq, _make("audioconvert"), _make("avenc_aac", bitrate=192000),
                               _make("aacparse")]
                rec.elements += audio_chain

            for element in rec.elements:
                pipeline.add(element)
            _link(*video_chain, mux)
            if audio_chain:
                _link(*audio_chain, mux)
            _link(mux, fsink)
            for element in reversed(rec.elements):
                element.sync_state_with_parent()

            clock = pipeline.get_clock()
            now = clock.get_time() - pipeline.get_base_time() if clock else 0
            branches = [(self._vtee, vq)] + ([(self._atee, audio_chain[0])] if audio_chain else [])
            for tee, queue in branches:
                sink_pad = queue.get_static_pad("sink")
                sink_pad.add_probe(
                    Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    self._make_preroll_dropper(),
                )
                tee_pad = tee.request_pad_simple("src_%u")
                tee_pad.set_offset(-now)
                if tee_pad.link(sink_pad) != Gst.PadLinkReturn.OK:
                    raise RuntimeError("could not attach recorder")
                rec.tee_pads.append((tee, tee_pad, sink_pad))

            fsink.get_static_pad("sink").add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_recorder_event, rec
            )
        except Exception as exc:
            log.exception("could not start recording")
            self._rec = rec
            self._cleanup_recording(rec)
            self._rec = None
            self.emit("recording-finished", "", f"Recording failed: {exc}")
            return False
        self._rec = rec
        self.emit("recording-changed", True)
        return True

    def _encoder_chain(self):
        encoder = dict(
            preset="p4", tune="high-quality", rc_mode="vbr", bitrate=20000,
            max_bitrate=30000, gop_size=120,
        )
        # The card only outputs YUY2, which cudaconvert can't take; convert on the CPU.
        return [
            _make("videoconvert", n_threads=4),
            _make("capsfilter", caps=Gst.Caps.from_string("video/x-raw,format=NV12")),
            _make("nvh264enc", **encoder),
            _make("h264parse"),
        ]

    @staticmethod
    def _make_preroll_dropper():
        """Drop buffers captured before the recording started (negative running time)."""
        state = {"segment": None}

        def probe(_pad, info):
            if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
                event = info.get_event()
                if event.type == Gst.EventType.SEGMENT:
                    state["segment"] = event.parse_segment()
                return Gst.PadProbeReturn.OK
            buffer = info.get_buffer()
            segment = state["segment"]
            if segment is None or buffer.pts == Gst.CLOCK_TIME_NONE:
                return Gst.PadProbeReturn.DROP
            sign, _rt = segment.to_running_time_full(Gst.Format.TIME, buffer.pts)
            if sign <= 0:
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        return probe

    def _on_recorder_event(self, _pad, info, rec):
        if info.get_event().type == Gst.EventType.EOS:
            rec.done.set()
            GLib.idle_add(self._cleanup_recording, rec)
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def stop_recording(self, reason="Recording saved", wait=False):
        rec = self._rec
        if rec is None:
            return
        self._rec = None
        rec.reason = reason

        def detach(tee, tee_pad, sink_pad):
            def on_idle(pad, _info):
                pad.unlink(sink_pad)
                sink_pad.send_event(Gst.Event.new_eos())
                return Gst.PadProbeReturn.REMOVE

            tee_pad.add_probe(Gst.PadProbeType.IDLE, on_idle)

        for tee, tee_pad, sink_pad in rec.tee_pads:
            detach(tee, tee_pad, sink_pad)
        if not rec.tee_pads:
            rec.done.set()
        if wait:
            rec.done.wait(3)
            self._cleanup_recording(rec)
        else:
            GLib.timeout_add(4000, self._cleanup_recording, rec)
        self.emit("recording-changed", False)

    def _cleanup_recording(self, rec):
        if rec.finished:
            return GLib.SOURCE_REMOVE
        rec.finished = True
        pipeline = self.pipeline
        for tee, tee_pad, sink_pad in rec.tee_pads:
            if tee_pad.is_linked():
                tee_pad.unlink(sink_pad)
            if pipeline is not None and tee.get_parent() is pipeline:
                tee.release_request_pad(tee_pad)
        for element in rec.elements:
            element.set_state(Gst.State.NULL)
            parent = element.get_parent()
            if parent is not None:
                parent.remove(element)
        if rec.reason:
            self.emit("recording-finished", rec.path, rec.reason)
        return GLib.SOURCE_REMOVE

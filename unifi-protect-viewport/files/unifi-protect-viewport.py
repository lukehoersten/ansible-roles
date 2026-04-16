#!/usr/bin/env python3
"""
unifi-protect-viewport: UniFi Protect doorbell display daemon

Connects to UniFi Protect, listens for doorbell ring events, and manages
display power + video playback on a Raspberry Pi portrait touchscreen.

States:
  IDLE   — display backlight off, no playback
  ACTIVE — display on, live RTSP stream playing via mpv

Transitions:
  ring event    : IDLE -> ACTIVE (or extend timer if already ACTIVE)
  touch (idle)  : IDLE -> ACTIVE
  touch (active): ACTIVE -> IDLE (immediate)
  timeout       : ACTIVE -> IDLE (after unifi_protect_viewport_timeout seconds)
"""

import asyncio
import json
import logging
import os
import signal
import ssl
import struct
import subprocess
import sys
import time
import zlib
from enum import Enum
from pathlib import Path

import evdev
import requests
import urllib3
import websockets

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("unifi-protect-viewport")


class State(Enum):
    IDLE = "idle"
    ACTIVE = "active"


class Config:
    def __init__(self):
        self.protect_host = os.environ["UNIFI_PROTECT_VIEWPORT_PROTECT_HOST"]
        self.protect_username = os.environ["UNIFI_PROTECT_VIEWPORT_PROTECT_USERNAME"]
        self.protect_password = os.environ["UNIFI_PROTECT_VIEWPORT_PROTECT_PASSWORD"]
        self.camera_id = os.environ["UNIFI_PROTECT_VIEWPORT_CAMERA_ID"]
        self.timeout = int(os.environ.get("UNIFI_PROTECT_VIEWPORT_TIMEOUT", "45"))
        self.touch_match = os.environ.get("UNIFI_PROTECT_VIEWPORT_TOUCH_MATCH", "")
        self.orientation = int(os.environ.get("UNIFI_PROTECT_VIEWPORT_ORIENTATION", "270"))
        self.drm_device = os.environ.get("UNIFI_PROTECT_VIEWPORT_DRM_DEVICE", "/dev/dri/card1")
        self.drm_connector = os.environ.get("UNIFI_PROTECT_VIEWPORT_DRM_CONNECTOR", "HDMI-A-1")
        self.drm_mode = os.environ.get("UNIFI_PROTECT_VIEWPORT_DRM_MODE", "")
        self.rtsp_url = None

    def log_config(self):
        log.info(
            "Config: protect_host=%s camera_id=%s timeout=%ds "
            "orientation=%d drm_device=%s drm_connector=%s drm_mode=%s",
            self.protect_host,
            self.camera_id,
            self.timeout,
            self.orientation,
            self.drm_device,
            self.drm_connector,
            self.drm_mode,
        )


class DisplayController:
    """Controls display backlight via /sys/class/backlight sysfs (DRM/KMS)."""

    def on(self):
        log.info("Display: ON")
        self._sysfs_set(True)

    def off(self):
        log.info("Display: OFF")
        self._sysfs_set(False)

    def _sysfs_set(self, enabled: bool):
        paths = sorted(Path("/sys/class/backlight").glob("*"))
        if not paths:
            log.warning("No backlight device found in /sys/class/backlight")
            return
        path = paths[0]
        try:
            if enabled:
                max_b = int((path / "max_brightness").read_text().strip())
                (path / "brightness").write_text(str(max_b))
                log.info("Backlight %s: brightness -> %d (max)", path.name, max_b)
            else:
                (path / "brightness").write_text("0")
                log.info("Backlight %s: brightness -> 0", path.name)
        except Exception as exc:
            log.error("Backlight %s failed: %s", "on" if enabled else "off", exc)


class ProtectClient:
    """Handles UniFi Protect authentication and camera info retrieval."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.verify = False
        self.base_url = f"https://{config.protect_host}"

    def authenticate(self) -> bool:
        log.info("Protect: authenticating at %s", self.config.protect_host)
        try:
            resp = self.session.post(
                f"{self.base_url}/api/auth/login",
                json={
                    "username": self.config.protect_username,
                    "password": self.config.protect_password,
                },
                timeout=10,
            )
            resp.raise_for_status()
            log.info("Protect: authentication successful")
            return True
        except Exception as exc:
            log.error("Protect: authentication failed: %s", exc)
            return False

    def get_last_update_id(self) -> str | None:
        try:
            resp = self.session.get(
                f"{self.base_url}/proxy/protect/api/bootstrap",
                timeout=10,
            )
            resp.raise_for_status()
            uid = resp.json().get("lastUpdateId")
            log.info("Protect: lastUpdateId=%s", uid)
            return uid
        except Exception as exc:
            log.warning("Protect: failed to fetch lastUpdateId: %s", exc)
            return None

    def get_camera_rtsp_url(self) -> str | None:
        log.info("Protect: fetching camera info for id=%s", self.config.camera_id)
        try:
            resp = self.session.get(
                f"{self.base_url}/proxy/protect/api/cameras/{self.config.camera_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            for channel in data.get("channels", []):
                if channel.get("isRtspEnabled"):
                    alias = channel.get("rtspAlias")
                    if alias:
                        url = f"rtsp://{self.config.protect_host}:7447/{alias}"
                        log.info("Protect: RTSP URL: %s", url)
                        return url
            log.error("Protect: no enabled RTSP channel for camera %s", self.config.camera_id)
            return None
        except Exception as exc:
            log.error("Protect: failed to fetch camera info: %s", exc)
            return None

    def cookie_header(self) -> dict:
        cookies = self.session.cookies.get_dict()
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def decode_protect_packets(data: bytes) -> list:
    """
    Decode all binary UniFi Protect WebSocket packets from a single message.

    Each WebSocket message contains one or more concatenated packets.
    Each packet header (8 bytes):
      [0] packet_type    1=action, 2=data
      [1] payload_format 1=JSON, 2=UTF8, 3=buffer
      [2] deflated       0 or 1
      [3] unused
      [4:8] payload_size big-endian uint32

    Followed by payload_size bytes of payload.
    """
    packets = []
    offset = 0
    while offset + 8 <= len(data):
        packet_type = data[offset]
        payload_format = data[offset + 1]
        deflated = bool(data[offset + 2])
        payload_size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        end = offset + 8 + payload_size
        if end > len(data):
            break
        payload_bytes = data[offset + 8:end]
        if deflated:
            try:
                payload_bytes = zlib.decompress(payload_bytes)
            except Exception as exc:
                log.debug("Packet decompression failed: %s", exc)
                offset = end
                continue
        if payload_format in (1, 2):
            try:
                packets.append({"packet_type": packet_type, "payload": json.loads(payload_bytes)})
            except Exception:
                pass
        offset = end
    return packets


class DoorbellViewport:
    def __init__(self, config: Config):
        self.config = config
        self.state = State.IDLE
        self.display = DisplayController()
        self.protect = ProtectClient(config)
        self.mpv_proc = None
        self.timer_task = None
        self._running = True

    async def run(self):
        log.info("unifi-protect-viewport starting")
        self.config.log_config()

        self.display.off()
        await self._login_and_fetch_rtsp()

        if not self.config.rtsp_url:
            log.warning("No RTSP URL at startup; will retry after reconnect")

        await asyncio.gather(
            self.protect_listener(),
            self.touch_listener(),
        )

    async def _login_and_fetch_rtsp(self):
        ok = await asyncio.to_thread(self.protect.authenticate)
        if ok and not self.config.rtsp_url:
            url = await asyncio.to_thread(self.protect.get_camera_rtsp_url)
            if url:
                self.config.rtsp_url = url

    async def activate(self):
        """Transition IDLE->ACTIVE, or extend timer if already ACTIVE."""
        was_idle = self.state == State.IDLE
        self.state = State.ACTIVE

        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
        self.timer_task = asyncio.create_task(self._timeout_task())

        if was_idle:
            log.info("State: IDLE -> ACTIVE")
            await self.start_mpv()
            self.display.on()
        else:
            log.info("State: ACTIVE -> timer extended")

    async def deactivate(self):
        """Transition ACTIVE->IDLE: stop playback and turn off display."""
        if self.state == State.IDLE:
            return
        self.state = State.IDLE
        log.info("State: ACTIVE -> IDLE")

        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
        self.timer_task = None

        self.display.off()
        await self.stop_mpv()

    async def on_touch(self):
        log.info("Event: touch input (state=%s)", self.state.value)
        if self.state == State.IDLE:
            await self.activate()
        else:
            await self.deactivate()

    async def _timeout_task(self):
        await asyncio.sleep(self.config.timeout)
        log.info("Timeout: %ds elapsed", self.config.timeout)
        await self.deactivate()

    async def start_mpv(self):
        if self.mpv_proc and self.mpv_proc.returncode is None:
            log.debug("mpv already running (pid=%d)", self.mpv_proc.pid)
            return
        if not self.config.rtsp_url:
            log.error("Cannot start mpv: no RTSP URL")
            return

        cmd = [
            "mpv",
            "--vo=drm",
            f"--drm-device={self.config.drm_device}",
            f"--drm-connector={self.config.drm_connector}",
            *(
                [f"--drm-mode={self.config.drm_mode}"]
                if self.config.drm_mode else []
            ),
            f"--video-rotate={self.config.orientation}",
            "--fullscreen",
            "--no-border",
            "--no-osc",
            "--no-audio",
            "--no-input-default-bindings",
            "--no-config",
            "--really-quiet",
            "--loop=no",
            "--cache=yes",
            "--demuxer-max-bytes=1M",
            "--hwdec=no",
            self.config.rtsp_url,
        ]
        log.info("Starting mpv: %s", " ".join(cmd))
        try:
            self.mpv_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("mpv started (pid=%d)", self.mpv_proc.pid)
            asyncio.create_task(self._watch_mpv())
        except Exception as exc:
            log.error("Failed to start mpv: %s", exc)

    async def stop_mpv(self):
        if not self.mpv_proc or self.mpv_proc.returncode is not None:
            return
        pid = self.mpv_proc.pid
        log.info("Stopping mpv (pid=%d)", pid)
        try:
            self.mpv_proc.terminate()
            await asyncio.wait_for(self.mpv_proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("mpv (pid=%d) did not terminate cleanly, killing", pid)
            self.mpv_proc.kill()
            await self.mpv_proc.wait()
        self.mpv_proc = None

    async def _watch_mpv(self):
        """Restart mpv on unexpected exit while display is active."""
        proc = self.mpv_proc
        if not proc:
            return
        await proc.wait()
        rc = proc.returncode
        if not self._running:
            return
        if rc != 0:
            log.warning("mpv exited unexpectedly (rc=%d)", rc)
            if self.mpv_proc is proc:
                self.mpv_proc = None
            await asyncio.sleep(2)
            if self.state == State.ACTIVE:
                log.info("Restarting mpv after unexpected exit")
                await self.start_mpv()

    async def protect_listener(self):
        """Maintain UniFi Protect WebSocket connection with exponential backoff."""
        backoff = 1
        while self._running:
            try:
                await self._connect_protect_ws()
                backoff = 1
            except Exception as exc:
                log.error("Protect WebSocket error: %s", exc)
            if not self._running:
                break
            log.info("Protect: reconnecting in %ds", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            await self._login_and_fetch_rtsp()

    async def _connect_protect_ws(self):
        log.info("Protect: connecting to WebSocket")
        last_update_id = await asyncio.to_thread(self.protect.get_last_update_id)
        ws_url = f"wss://{self.config.protect_host}/proxy/protect/ws/updates"
        if last_update_id:
            ws_url += f"?lastUpdateId={last_update_id}"

        async with websockets.connect(
            ws_url,
            extra_headers=self.protect.cookie_header(),
            ssl=_SSL_CTX,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            log.info("Protect: WebSocket connected")
            pending_action = None
            async for msg in ws:
                if isinstance(msg, bytes):
                    packets = decode_protect_packets(msg)
                    for pkt in packets:
                        if pkt["packet_type"] == 1:
                            pending_action = pkt["payload"]
                        elif pkt["packet_type"] == 2 and pending_action is not None:
                            await self._handle_protect_event(pending_action, pkt["payload"])
                            pending_action = None

    async def _handle_protect_event(self, action: dict, data: dict):
        if action.get("action") != "add":
            return
        if action.get("modelKey") != "event":
            return
        event_type = data.get("type")
        camera = data.get("camera") or data.get("cameraId") or ""
        if event_type == "ring" and camera == self.config.camera_id:
            log.info("Protect: ring event from camera %s", camera)
            await self.activate()

    async def touch_listener(self):
        """Monitor evdev touch input with automatic device re-discovery."""
        while self._running:
            try:
                await self._monitor_touch_device()
            except Exception as exc:
                log.error("Touch device error: %s", exc)
            if not self._running:
                break
            log.info("Touch: retrying device discovery in 10s")
            await asyncio.sleep(10)

    def _find_touch_device_sync(self):
        """Find touch device by name match or multitouch capability."""
        match = self.config.touch_match.lower()
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if match and match in dev.name.lower():
                    log.info("Touch: matched by name: %s (%s)", dev.name, path)
                    return dev
                caps = dev.capabilities()
                if evdev.ecodes.EV_ABS in caps:
                    axis_codes = [code for code, _ in caps[evdev.ecodes.EV_ABS]]
                    if evdev.ecodes.ABS_MT_POSITION_X in axis_codes:
                        log.info("Touch: matched by capability: %s (%s)", dev.name, path)
                        return dev
                dev.close()
            except Exception:
                continue
        return None

    async def _monitor_touch_device(self):
        dev = await asyncio.to_thread(self._find_touch_device_sync)
        if not dev:
            log.warning("Touch: no device found")
            await asyncio.sleep(10)
            return

        log.info("Touch: monitoring %s (%s)", dev.name, dev.path)
        last_touch_time = 0.0
        try:
            async for event in dev.async_read_loop():
                if not self._running:
                    break
                triggered = (
                    (event.type == evdev.ecodes.EV_ABS
                     and event.code == evdev.ecodes.ABS_MT_TRACKING_ID
                     and event.value >= 0)
                    or (event.type == evdev.ecodes.EV_KEY
                        and event.code == evdev.ecodes.BTN_TOUCH
                        and event.value == 1)
                )
                if triggered:
                    now = time.monotonic()
                    if now - last_touch_time > 0.5:  # 500ms debounce
                        last_touch_time = now
                        await self.on_touch()
        finally:
            dev.close()


async def main():
    try:
        config = Config()
    except KeyError as exc:
        log.error("Missing required environment variable: %s", exc)
        sys.exit(1)

    viewport = DoorbellViewport(config)
    loop = asyncio.get_running_loop()

    def shutdown(sig):
        log.info("Signal %s received, shutting down", sig.name)
        viewport._running = False
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: shutdown(s))

    try:
        await viewport.run()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down")
        await viewport.stop_mpv()
        viewport.display.off()
        log.info("unifi-protect-viewport stopped")


if __name__ == "__main__":
    asyncio.run(main())

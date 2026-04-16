# doorbell-viewport

Raspberry Pi 4 portrait touchscreen that shows a live UniFi Protect doorbell
stream when the doorbell is pressed or the screen is touched. Display backlight
is fully off at idle.

## Hardware

- Raspberry Pi 4 + PoE HAT
- HDMI touchscreen (800×480, mounted portrait)
- Wired Ethernet (PoE)
- UniFi Protect PoE doorbell

## Behavior

| Trigger | From | To |
|---------|------|----|
| Doorbell ring | IDLE | ACTIVE |
| Doorbell ring | ACTIVE | extend timer |
| Touch | IDLE | ACTIVE |
| Touch | ACTIVE | immediate OFF |
| 45s timeout | ACTIVE | IDLE |

IDLE: backlight fully off, no visible pixels.  
ACTIVE: backlight on, live RTSP stream playing fullscreen.

---

## UniFi Protect Setup

### Create a local API user

The daemon authenticates directly with UniFi Protect using a **local account**.
UniFi SSO accounts (ubiquiti.com login) do not support API authentication and
will not work.

1. In UniFi OS, go to **OS Settings → Admins & Users → Users**
2. Click **Add User**
3. Fill in:
   - **Username**: choose a service account name (e.g. `doorbell-viewport`)
   - **Password**: generate a strong password (store in Ansible Vault)
   - **Account Type**: Local Access Only
4. Click **Add**

### Assign the user a Protect role

After creating the user:

1. Go to **UniFi Protect → Settings → Manage → Admins**
2. Find the user you created and click the pencil icon
3. Set the Protect role to **View Only**

View Only is sufficient. It grants:
- API login (`POST /api/auth/login`)
- Camera info and RTSP stream URLs (`GET /proxy/protect/api/cameras/{id}`)
- Live WebSocket event stream including ring events

### Get the camera ID

You need the camera's ID (a 24-character hex string) to filter ring events.
Run these two commands from any machine that can reach the Protect host:

```bash
curl -sk -c /tmp/prot.cookies \
  -H 'Content-Type: application/json' \
  -d '{"username":"doorbell-viewport","password":"YOUR_PASSWORD"}' \
  https://YOUR_PROTECT_HOST/api/auth/login
```

```bash
curl -sk -b /tmp/prot.cookies https://YOUR_PROTECT_HOST/proxy/protect/api/cameras | python3 -c "import sys,json; [print(c['id'], c.get('type',''), c['name']) for c in json.load(sys.stdin)]"
```

Output looks like:
```
aabbcc1122334455aabbcc00 UVC-G4-Doorbell  Front Door
```

Copy the ID of the doorbell camera and set it as `doorbell_viewport_camera_id`.

### Verify access

Once the role is deployed:

```bash
doorbell-viewport-debug test-protect
```

---

## DRM/KMS and the Framebuffer Console

This role uses mpv with `--vo=drm` — direct DRM/KMS rendering, no X11 or
Wayland required.

### DRM driver: use vc4-fkms-v3d (firmware KMS), not vc4-kms-v3d (full KMS)

`vc4-fkms-v3d` uses the VideoCore GPU firmware for HDMI negotiation and mode
setting. `vc4-kms-v3d` (full KMS) attempts atomic mode setting directly from
the kernel, which on some displays causes a color-cycling test pattern that mpv
cannot override. Use `vc4-fkms-v3d`. The Ansible role sets this automatically.

On Ubuntu Server the kernel framebuffer console (`fbcon`) holds the DRM device
while a getty is running on tty1. This will cause mpv to fail with a "device
busy" error on first boot.

### Fix: disable fbcon on HDMI

Add `video=HDMI-A-1:D` to `/boot/firmware/cmdline.txt`. This tells the kernel
not to bind the framebuffer console to the HDMI output, freeing the DRM device
for mpv. Since this is an appliance with no local console, this is the correct
permanent state.

Edit the file on the Pi:

```bash
sudo sed -i 's/$/ video=HDMI-A-1:D/' /boot/firmware/cmdline.txt
```

The connector name (`HDMI-A-1`) is configurable via `doorbell_viewport_drm_connector` if your hardware uses a different name. The Ansible role manages this automatically.

Then reboot. The HDMI output will be dark at the console from that point on,
which is exactly what you want — the display is managed entirely by
doorbell-viewport.

Verify mpv can open the DRM device after reboot:

```bash
sudo -u doorbell-viewport mpv --vo=drm --length=3 /dev/zero
```

If it exits without a "device busy" error, DRM access is working.

---

## Configuration

### host_vars (plain)

```yaml
doorbell_viewport_protect_host: "unifi.lan.example.com"  # or IP address
doorbell_viewport_camera_id: "abcdef1234567890abcdef12"
doorbell_viewport_timeout: 45
doorbell_viewport_touch_match: ""          # substring match on evdev device name
doorbell_viewport_prebuffer_mode: "warm"   # warm | cold
doorbell_viewport_display_backend: "vcgencmd"  # vcgencmd | drm | panel
doorbell_viewport_orientation: 270         # degrees: 90 | 180 | 270
doorbell_viewport_drm_device: "/dev/dri/card1"   # RPi4: card1; RPi3: card0
doorbell_viewport_drm_connector: "HDMI-A-1"
doorbell_viewport_drm_mode: "848x480"      # nearest mode advertised by your display
```

### host_vars (vault)

```yaml
vault_doorbell_viewport_protect_username: "doorbell-viewport"
vault_doorbell_viewport_protect_password: "secret"
```

Create and encrypt the vault file:

```bash
ansible-vault create inventory/host_vars/HOSTNAME/vault.yaml
```

---

## Display Backends

### vcgencmd (default)

Uses `vcgencmd display_power 0/1` (Raspberry Pi firmware command).
Cuts the HDMI signal. Displays that support DPMS will cut their backlight.

```yaml
doorbell_viewport_display_backend: "vcgencmd"
```

### drm

Writes `0` / `max_brightness` to the first device under `/sys/class/backlight/`.
Use when the display exposes a sysfs backlight node.

```yaml
doorbell_viewport_display_backend: "drm"
```

Check available devices on the Pi: `ls /sys/class/backlight/`

### panel

Alias for `drm`. Use when the backlight path is panel-specific and the `drm`
label would be confusing.

### Testing

```bash
doorbell-viewport-debug test-display   # on -> 3s -> off
```

---

## Prebuffer Modes

### warm (default, recommended)

mpv starts at boot and continuously decodes the RTSP stream, rendering to the
framebuffer while the display stays off. Activation just turns on the backlight
— essentially zero latency. The live frame is already there.

```yaml
doorbell_viewport_prebuffer_mode: "warm"
```

### cold

mpv is launched only when the display activates, then stopped when idle. Higher
activation latency (stream connect + first keyframe decode). Use as fallback if
warm mode causes problems (power draw, memory pressure).

```yaml
doorbell_viewport_prebuffer_mode: "cold"
```

---

## Touch Discovery

The daemon discovers the touch device via evdev at startup and re-discovers it
automatically if it disappears and re-enumerates (e.g. USB replug).

If `doorbell_viewport_touch_match` is set, the daemon matches on any device
whose name contains that substring (case-insensitive). Otherwise it finds the
first device advertising `ABS_MT_POSITION_X` (multitouch absolute position).

To find the right match string for your display:

```bash
doorbell-viewport-debug test-touch
```

Example output:
```
/dev/input/event0: 'WaveShare WS170120' (multitouch=True, btn_touch=True)
```

```yaml
doorbell_viewport_touch_match: "waveshare"
```

---

## Debug Commands

```bash
doorbell-viewport-debug show           # turn display on
doorbell-viewport-debug hide           # turn display off
doorbell-viewport-debug test-display   # power cycle: on -> 3s -> off
doorbell-viewport-debug test-touch     # list touch devices
doorbell-viewport-debug test-stream    # fetch RTSP URL and play via mpv
doorbell-viewport-debug test-protect   # verify Protect API + camera info
```

---

## Logs

```bash
journalctl -u doorbell-viewport -f
```

---

## Dependencies

Installed by the role:

| Package | Purpose |
|---------|---------|
| `mpv` | DRM/KMS video playback |
| `python3-evdev` | Touch input via evdev |
| `python3-requests` | UniFi Protect HTTP API |
| `python3-websockets` | UniFi Protect WebSocket event stream |

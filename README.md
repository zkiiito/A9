# This is a fork

of <https://github.com/zkiiito/A9> great work. I only added audio support and a few of minor changes.
It was only tested in my environment:

- Linux Lubuntu 22.04.4 LTS
- Python 3.10.12
- Chromium 147.0.7750.0
- Firefox 135.0
- vlc 3.0.16
- mpv 0.34.1
- camera INO-A10N-V2.3 Ca13


av and numpy python modules are the dependancies, In my case i installed them with `python3 -m pip install --only-binary=:all: "av==14.2.0"` and `python3 -m pip install numpy`.
You may have different python versions and may try different module versions.
There are also different A9 cameras that may work with this.
Let me know of any other working rig.

Special infinite thanks to **Zoltan Feher aka zkiiito** who did the hard work.

# A9 local camera client

A standalone, local client for the cheap XCThings / Linklemo "A9" WiFi camera
(PCB `INO-IPC-A9`, Beken BK7252N, Android app `Linklemo` / `com.xcthings.fchan`).
It finds a camera you own on your LAN, authenticates, and pulls a JPEG, with no
cloud, no vendor app, and no external Python dependencies.

Built for local interoperability with a camera I own (the goal was a Home Assistant
feed). Along the way it documents the device's `pprpc` protocol and a credential
disclosure worth knowing about.

## The short version of the security finding

The camera answers an unauthenticated UDP broadcast with an encrypted packet, and
that packet is encrypted with a key that is **hardcoded in the firmware and the app
and shared across every unit**. Decrypt it and you get the device's own
`paired_auth_secret` in cleartext, which is all you need to log in and watch the
video. So anyone on the same network can harvest the credential from one broadcast.
Details in [`PROTOCOL.md`](PROTOCOL.md) §J. An independent write-up of the same
device reached the same conclusion about the transport (see references).

Use this only against cameras you own.

## Setup

One-time: the tools need the shared pprpc prekey, which is not committed. Put it in a
local `.env`:

```bash
cp .env.example .env     # then set A9_PREKEY= in .env   (or: export A9_PREKEY=...)
```

`A9_PREKEY` is a fixed secret hardcoded in the firmware and app, shared across all
units (not device-specific). This repo doesn't ship the value; it's the same one
documented in the public analyses under [References](#references). `.env` is gitignored.

## Quick start

Requirements: **Python 3.8+**, standard library only. A bundled pure-Python AES
(`a9_aes.py`) means no pip install is needed; `pycryptodome` is used automatically if
present (faster), and `Pillow` is optional (only for the `extract_a9_frames.py` contact
sheet). On the same network as the camera, or joined to its AP:

```bash
# discover the camera, read its own credentials, grab a frame — zero input
python3 a9_live_snapshot.py -o snapshot.jpg

# just list cameras on the network
python3 a9_discover.py

# save the camera's details once, then reuse (skips discovery next time)
python3 a9_discover.py --save-config          # writes a9_camera.json (chmod 600)
python3 a9_live_snapshot.py -o snapshot.jpg   # reads a9_camera.json if present
```

### Live feed (browser / Home Assistant / multimedia players)

```bash
python3 a9_mjpeg_server.py                     # then open http://127.0.0.1:8080/
python3 a9_mjpeg_server.py --bind 0.0.0.0      # expose to other hosts (e.g. HA)
```

It holds one session to the camera and serves `/stream.mjpg` (MJPEG, ~10 fps),
`/snapshot.jpg` (latest still), and `/` (a viewer page). It sends the camera's keepalive
(a `107` ping ~2/s acking frames received), so the stream runs continuously at full rate;
a stall watchdog reconnects only if the network actually drops.
`/status` returns JSON (battery, firmware/model, connected/streaming) decoded from the
camera's `2780` status pushes, handy for a Home Assistant REST sensor.

In this fork I added those endpoins: `/stream` a brovser stream with audio and `/live.ts` stream with audio for vlc, mpv and possibly other players.

For Home Assistant, point an **MJPEG IP Camera** at `.../stream.mjpg`, or a **Generic
Camera** still-image URL at `.../snapshot.jpg`. The feed is unauthenticated, so it
binds to `127.0.0.1` unless you pass `--bind 0.0.0.0`; keep it inside your own network.

`a9_camera.json` holds a device credential, so it is written `0600` and is
gitignored. Keep it local.

## Files

| file | purpose |
|---|---|
| `a9_live_snapshot.py` | online: grab a JPEG; auto-discovers IP + credentials, or reads the config |
| `a9_mjpeg_server.py` | online: live MJPEG + snapshot HTTP server (browser / Home Assistant) |
| `a9_discover.py` | online: UDP discovery; decodes DID / IP / port / firmware / secret; writes the config |
| `a9_proto.py` | shared PPRPC primitives (varint/protobuf, crypto, framing, pcap) used by all tools |
| `a9_aes.py` | pure-Python AES-CBC (stdlib only, so the tools run with no pip / no internet) |
| `a9_keys.py` | loads the shared prekey from `A9_PREKEY` / `.env` (not hardcoded) |
| `extract_a9_snapshot.py` | offline: a packet capture to the first JPEG (also a library used by the live client) |
| `extract_a9_frames.py` | offline: a capture to all frames plus a contact sheet |
| `decode_a9_pcap.py` | offline: decode the CTRL handshake and commands from a capture |
| `PROTOCOL.md` | the full protocol writeup (framing, crypto, discovery, command catalog) |

## Firmware

The firmware dump and the de-interleaved application image are **not included** (they
are the vendor's, and large). Two ways to get one:

- **Download a dump of this exact board**: `XCThings_Linklemo_A9_INO-IPC-V2.3_(BluePCB).bin`
  is in [`openshwprojects/FlashDumps`](https://github.com/openshwprojects/FlashDumps/tree/main/IoT/BK7252N)
  (originally shared on an elektroda.com forum thread).
- **Dump your own**: read the SPI flash and remove the Beken CRC interleaving
  (32 data bytes + 2 CRC bytes per block); see [`PROTOCOL.md`](PROTOCOL.md) §0.

The addresses in `PROTOCOL.md` are virtual addresses in the de-interleaved image at
base `0x00010000`.

## How it works

See [`PROTOCOL.md`](PROTOCOL.md). In brief: `pprpc` over TCP/UDP port 20190,
protobuf payloads, AES-256-CBC with keys derived by MD5 from a hardcoded secret
(CTRL) or the per-session key (MEDIA). The video is MJPEG at 640x480; the "HD/UHD"
settings in the app do not change the live stream.

## References

- FuseTim, "The inSecurity Camera", an independent analysis of the same device:
  <https://fusetim.space/posts/20251005-the-insecurity-camera/>
- `pprpc` transport library (what the firmware/app are built on):
  <https://github.com/pprpc> (the `core` module also survives on the Go module proxy,
  `proxy.golang.org/github.com/pprpc/core`)
- Firmware dump of this exact board:
  <https://github.com/openshwprojects/FlashDumps/tree/main/IoT/BK7252N>
- Related independent work on the same camera family:
  [`IvanFogel/ino-a9-local-bridge`](https://github.com/IvanFogel/ino-a9-local-bridge)
  (local PPRPC client + MJPEG bridge with Home Assistant integration) and
  [`Yeti47/cheap-shot`](https://github.com/Yeti47/cheap-shot) (Go PPRPC implementation)
- Vendor: XC Things; app `Linklemo` / `com.xcthings.fchan`
- Hardware: PCB `INO-IPC-A9` (also sold as "Pinmei A9"), Beken BK7252N
- <https://www.elektroda.com/rtvforum/topic4118499-120.html>
- 
## Scope and legal

For local interoperability and security research on hardware you own. Do not use it
against devices you do not own. No warranty.

## Offline captures and tests

Offline extraction accepts one authenticated camera session per capture. Captures
with multiple sessions are rejected instead of combining their keys and video.
The reader separates TCP connections using handshake and close packets, supports
sequence wrap, and rejects missing bytes or conflicting retransmissions. A capture
that omits connection boundaries cannot reliably distinguish a reused TCP tuple.
Each reassembled direction is limited to 64 MiB; each JPEG is limited to 8 MiB.

Explicit camera identifiers or IPs that differ from the saved config trigger fresh
discovery. Credentials from a different camera are never borrowed from that config.
Snapshots older than `--max-age` seconds return HTTP 503; the default is 10 seconds.

Run the hardware-free regression tests with:

```bash
python3 -m unittest discover -s tests -v
python3 a9_aes.py
```

CI runs these checks on Python 3.8 and 3.13, with and without PyCryptodome.

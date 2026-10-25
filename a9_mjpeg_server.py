#!/usr/bin/env python3
"""
a9_mjpeg_server.py - live MJPEG/snapshot server for an XCThings/Linklemo A9 camera.

Holds one PPRPC session to a camera you own, decodes the MJPEG stream continuously,
and serves it over HTTP so a browser or Home Assistant can show a live feed:

    python3 a9_mjpeg_server.py                 # discover (or read a9_camera.json)
    python3 a9_mjpeg_server.py 192.168.3.114   # a specific camera
    # then open http://127.0.0.1:8080/

Endpoints:
    /               small HTML viewer
    /stream.mjpg    multipart/x-mixed-replace MJPEG stream  (HA: "MJPEG IP Camera")
    /snapshot.jpg   the latest single frame                 (HA: "Generic Camera" still)
    /status         JSON: battery, firmware, connected/streaming (from cmd-2780 pushes)
    /stream         browser stream with audio
    /live.ts        vlc, mpv and possibly other players stream with audio
    
Uses the shared protocol/crypto in a9_proto.py, so
no external dependencies. Reconnects automatically if the camera drops the session.

Credentials come from --did/--secret, else a9_camera.json, else UDP discovery
(same precedence as the client). The stream is unauthenticated, so it binds to
127.0.0.1 by default; use --bind 0.0.0.0 to expose it to other hosts (e.g. HA on
another machine) and put it behind your own network controls.

For a camera you own.
"""
import sys
import os
import socket
import time
import threading
import argparse
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import a9_proto as P
import a9_discover as D
import av
import queue
from fractions import Fraction
import io
import numpy as np

def decode_alaw(data):
    pcm = bytearray()

    for value in data:
        value ^= 0x55
        sample = (value & 0x0F) << 4
        segment = (value & 0x70) >> 4

        if segment == 0:
            sample += 8
        else:
            sample += 0x108
            sample <<= segment - 1

        if value & 0x80:
            sample = sample
        else:
            sample = -sample

        sample = max(-32768, min(32767, sample * 2))
        pcm.extend(struct.pack("<h", sample))

    return bytes(pcm)


# ---------- Shared latest-frame state ----------

class Latest:
    def __init__(self):
        self.client_count = 0
        self.camera_wanted = False
        self.cond = threading.Condition()
        self.jpeg = None
        self.seq = 0
        self.stop = False
        self.connected = False
        self.status = None
        self.last_frame_ts = 0.0
        self.socket_lock = threading.Lock()
        self.camera_socket = None
        self.audio_cond = threading.Condition()
        self.audio_chunks = []
        self.audio_bytes = 0
        self.audio_max_bytes = 32000
        self.audio_seq = 0

    def publish(self, jpeg):
        with self.cond:
            self.jpeg = jpeg
            self.seq += 1
            self.last_frame_ts = time.time()
            self.cond.notify_all()
            
    def publish_audio(self, pcm):
        if not pcm:
            return

        with self.audio_cond:
            self.audio_seq += 1
            self.audio_chunks.append(
                (self.audio_seq, time.monotonic(), pcm)
            )
            self.audio_bytes += len(pcm)

            while (
                self.audio_chunks
                and self.audio_bytes > self.audio_max_bytes
            ):
                _, _, old_pcm = self.audio_chunks.pop(0)
                self.audio_bytes -= len(old_pcm)

            self.audio_cond.notify_all()            
            

    def set_status(self, st):
        with self.cond:
            self.status = st

    def wait_newer(self, last_seq, timeout):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            if self.seq != last_seq:
                return self.jpeg, self.seq
            return None, last_seq
    def acquire_client(self):
        """Registration of client requesting streaming."""
        with self.cond:
            self.client_count += 1

            if self.client_count == 1:
                self.camera_wanted = True
                self.connected = False
                self.jpeg = None
                self.last_frame_ts = 0.0

                with self.audio_cond:
                    self.audio_chunks.clear()
                    self.audio_bytes = 0

            self.cond.notify_all()

    def release_client(self):
        """Release client and stops sessione if necessary."""
        sock = None

        with self.cond:
            if self.client_count > 0:
                self.client_count -= 1

            if self.client_count == 0:
                self.camera_wanted = False
                self.connected = False

                with self.socket_lock:
                    sock = self.camera_socket

            self.cond.notify_all()

        # shuts socket.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                sock.close()
            except OSError:
                pass

# ---------- Camera session ----------

def camera_loop(ip, did, secret, state, timeout, start_seq):
    while not state.stop:

        # waits first client without opening connennection to camera.
        with state.cond:
            while not state.stop and not state.camera_wanted:
                state.cond.wait()

            if state.stop:
                break

        s = None

        try:
            # Verifies before opening new connection.
            with state.cond:
                if state.stop or not state.camera_wanted:
                    continue

            s = socket.create_connection(
                (ip, P.PORT),
                timeout=timeout,
            )

            with state.socket_lock:
                state.camera_socket = s

            # a client could have been disconnected during connect().
            with state.cond:
                wanted = state.camera_wanted and not state.stop

            if wanted:
                _run_session(
                    s, did, secret, state, timeout, start_seq
                )

        except Exception as exc:
            with state.cond:
                wanted = state.camera_wanted and not state.stop

            if wanted:
                sys.stderr.write(
                    f"[camera] session error: {exc}; "
                    "reconnecting in 2s\n"
                )

        finally:
            with state.socket_lock:
                if state.camera_socket is s:
                    state.camera_socket = None

            if s is not None:
                sys.stderr.write(
                    "[camera] Disconnection from camera.\n"
                )
                sys.stderr.flush()

                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

                try:
                    s.close()
                except OSError:
                    pass

            with state.cond:
                state.connected = False
                state.cond.notify_all()

        # Reconnection only if at least une client exists.
        with state.cond:
            if state.stop:
                break

            if not state.camera_wanted:
                continue

            # brakable wait: if every client exit,
            # go back to wait without making new connections.
            state.cond.wait_for(
                lambda: state.stop or not state.camera_wanted,
                timeout=2.0,
            )


def _run_session(s, did, secret, state, timeout, start_seq):
    rdr = P.FrameReader(s)
    seq = start_seq

    s.sendall(
        P.build_ctrl(
            seq,
            P.CMD_LANAUTH,
            P.pb_field_bytes(2, did) + P.pb_field_bytes(3, secret),
        )
    )
    seq += 1

    # Obtain session key from LanAuth reply.
    session_key = None
    t_end = time.time() + timeout

    while time.time() < t_end and session_key is None:
        mt, mf, body = rdr.next_frame(timeout=timeout)

        if mt == 4:
            cmd, rseq, rpc, ct = P.ctrl_exhdr(body)

            if cmd == P.CMD_LANAUTH:
                pt = P.ctrl_decrypt(cmd, rseq, rpc, ct)

                if pt:
                    f = P.pb_parse(pt)
                    if 1 in f and isinstance(f[1][0], bytes):
                        session_key = f[1][0]

    if not session_key:
        raise RuntimeError(
            "LanAuth gave no session key (wrong credentials?)"
        )

    def answer(cmd, rseq, rpc, ct):
        if rpc != 0:
            return

        if cmd == P.CMD_PING:
            pt = P.ctrl_decrypt(cmd, rseq, rpc, ct)
            echo = P.pb_parse(pt).get(1, [0])[0] if pt else 0

            pong = (
                P.pb_varint_field(1, echo)
                + P.pb_varint_field(2, P.HB_CONST)
                + P.pb_varint_field(3, int(time.time() * 1000))
            )

            s.sendall(
                P.build_ctrl(rseq, P.CMD_PING, pong, flag=0x0D)
            )

        elif cmd == P.CMD_HELLO:
            s.sendall(
                P.build_ctrl(
                    rseq,
                    P.CMD_HELLO,
                    P.pb_varint_field(1, 1),
                    flag=0x0D,
                )
            )
    last_video_avseq = 0
    frags = P.MediaFragments()
    started = False
    last_media = time.time()
    frame_count = 0
    frame_fragments = 0
    media_counts = {}
    # Diagnostic counters, indexed by MEDIA subtype.
    media_counts = {}

    def process(mt, body):
        nonlocal seq, frags, started, last_media, frame_count, frame_fragments, last_video_avseq

        if mt == 4:
            cmd, rseq, rpc, ct = P.ctrl_exhdr(body)
            answer(cmd, rseq, rpc, ct)

            if cmd == P.CMD_STATUS and ct and len(ct) % 16 == 0:
                pt = P.ctrl_decrypt(cmd, rseq, rpc, ct)

                if pt:
                    st = P.decode_status_2780(pt)
                    if st:
                        state.set_status(st)

        elif mt == 6:
            if not body:
                return

            last_media = time.time()

            # Bit 7: frame-start flag.
            # Bits 0-6: MEDIA subtype.
            subtype = body[0] & 0x7F
            frame_start = bool(body[0] & 0x80)
            try:
                _, avch, avseq, tt, enclen, payload = P.media_exhdr(body)

            except Exception as exc:
                print(f"[MEDIA-TRACE-ERROR] {exc!r}", flush=True)
            if subtype == 4 and frame_start:
                try:
                    _, _, last_video_avseq, _, _, _ = P.media_exhdr(body)
                except ValueError:
                    pass

            media_counts[subtype] = (
                media_counts.get(subtype, 0) + 1
            )
            count = media_counts[subtype]

            if subtype == 21:
                 import struct

                 try:
                     start, avch, avseq, tt, enclen, data = (
                         P.media_exhdr(body)
                     )
                     state.publish_audio(decode_alaw(data))

                 except Exception as exc:
                     print(
                         f"[AUDIO-DIAG] parse error: {exc}",
                         flush=True,
                     )

                 return

            # IMPORTANT:
            # Only subtype 4 is passed to the JPEG reconstruction.
            # Audio subtype 21 must never be appended to frags.
            if subtype != 4:
                return

            if frame_start:
                frame_count += 1

                # decodes precedent frame if present
                if started and frame_fragments > 0:
                    try:
                        jpg = P.decrypt_media_frame(
                            frags, session_key
                        )

                        if jpg and jpg.startswith(b"\xff\xd8") and jpg.endswith(b"\xff\xd9"):
                            state.publish(jpg)
                        
                    except Exception as exc:
                        print(
                            f"[VIDEO-CHECK] error "
                            f"frame={frame_count} "
                            f"fragments={frame_fragments}: {exc!r}",
                            flush=True,
                        )

                # starts new frame.
                frags = P.MediaFragments()
                frags.append(body)
                frame_fragments = 1
                started = True

            elif started:
            
                if (body[0] & 0x7F) != 4:
                    return
            
                frags.append(body)
                frame_fragments += 1

    def pump(dur):
        end = time.time() + dur

        while (
            time.time() < end
            and not state.stop
            and state.camera_wanted
        ):
            try:
                mt, mf, body = rdr.next_frame(
                    timeout=max(0.05, end - time.time())
                )
            except TimeoutError:
                break

            process(mt, body)

    # Answer initial handshake before starting video.
    s.sendall(
        P.build_ctrl(
            seq, P.CMD_HELLO, P.pb_varint_field(1, 1)
        )
    )
    seq += 1
    pump(0.3)

    # Original startup sequence: audio is not enabled here.
    for cmd, pt in [
        (2653, b""),
        (2645, b""),

        (P.CMD_VIDEOPLAY, b""),
    ]:
        s.sendall(P.build_ctrl(seq, cmd, pt))
        seq += 1
        pump(0.15)

    state.connected = True
    if state.stop or not state.camera_wanted:
        state.connected = False
        return
    last_media = time.time()

    sys.stderr.write("[camera] authenticated; streaming\n")

    # Keepalive and delayed audio activation.
    STALL = 6.0
    last_ping = 0.0

     #Send RPC 2614 once, can add varible delay after entering the main loop.
    audio_at = time.time()
#     + 10.0
    audio_sent = False

    while not state.stop and state.camera_wanted:
        now = time.time()

        if now - last_ping >= 0.1:
            s.sendall(
                P.build_ctrl(
                    seq,
                    P.CMD_PING,
                    P.pb_varint_field(1, int(now * 1000))
                    + P.pb_varint_field(4, last_video_avseq),
                )
            )
            seq += 1
            last_ping = now

        if not audio_sent and now >= audio_at:
            print(
#                "[RPC TEST] cmd=2614",
                flush=True,
            )
            s.sendall(P.build_ctrl(seq, 2614, b""))
            seq += 1
            audio_sent = True

        try:
            mt, mf, body = rdr.next_frame(timeout=0.3)
            process(mt, body)

        except TimeoutError:
            pass

        if time.time() - last_media > STALL:
            sys.stderr.write("[camera] media stalled; reconnecting\n")
            break


# ---------- HTTP server ----------

VIEWER = b"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body { margin:0; width:100%; height:100%; background:#000; overflow:hidden; }
canvas { display:block; width:100%; height:100%; object-fit:contain; }
</style>
</head>
<body>
<canvas id="video"></canvas>
<script>
(async () => {
  const canvas = document.getElementById("video");
  const ctx = canvas.getContext("2d");
  const magic = [65,57,65,86]; // A9AV
  let pending = new Uint8Array(0);
  let audio = null;
  let nextAudioTime = 0;

  function findMagic(b) {
    for (let i = 0; i <= b.length - 4; i++) {
      if (b[i] === magic[0] && b[i+1] === magic[1] &&
          b[i+2] === magic[2] && b[i+3] === magic[3]) return i;
    }
    return -1;
  }

  function playPCM(payload) {
    if (!audio) {
      audio = new AudioContext({sampleRate:8000});
      audio.resume().catch(() => {});
    }

    const count = Math.floor(payload.length / 2);
    if (!count) return;

    const view = new DataView(
      payload.buffer, payload.byteOffset, payload.byteLength
    );
    const buffer = audio.createBuffer(1, count, 8000);
    const samples = buffer.getChannelData(0);

    for (let i = 0; i < count; i++) {
      samples[i] = view.getInt16(i * 2, true) / 32768;
    }

    const source = audio.createBufferSource();
    source.buffer = buffer;
    const gain = audio.createGain();
    gain.gain.value = 1.5; // +3,5 dB circa
    source.connect(gain);
    gain.connect(audio.destination);

    const now = audio.currentTime;
    if (nextAudioTime < now || nextAudioTime > now + 0.5) {
      nextAudioTime = now + 0.08;
    }

    source.start(nextAudioTime);
    nextAudioTime += buffer.duration;
  }

  function showJPEG(payload) {
    createImageBitmap(new Blob([payload], {type:"image/jpeg"}))
      .then(bitmap => {
        if (canvas.width !== bitmap.width ||
            canvas.height !== bitmap.height) {
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
        }
        ctx.drawImage(bitmap, 0, 0);
        bitmap.close();
      })
      .catch(() => {});
  }

  const response = await fetch("/stream?raw=1", {cache:"no-store"});
  if (!response.ok || !response.body) {
    throw new Error("No stream available: HTTP " + response.status);
  }

  const reader = response.body.getReader();

  while (true) {
    const result = await reader.read();
    if (result.done) break;

    const incoming = result.value;
    const joined = new Uint8Array(pending.length + incoming.length);
    joined.set(pending);
    joined.set(incoming, pending.length);
    pending = joined;

    while (pending.length >= 17) {
      const pos = findMagic(pending);

      if (pos < 0) {
        pending = pending.slice(-3);
        break;
      }

      if (pos > 0) pending = pending.slice(pos);
      if (pending.length < 17) break;

      const header = new DataView(
        pending.buffer, pending.byteOffset, pending.byteLength
      );
      const type = pending[4];
      const length = header.getUint32(13, false);

      if (length > 10000000) {
        throw new Error("Invalid record length");
      }

      if (pending.length < 17 + length) break;

      const payload = pending.slice(17, 17 + length);
      pending = pending.slice(17 + length);

      if (type === 1) showJPEG(payload);
      else if (type === 2) playPCM(payload);
    }
  }
})().catch(err => console.error("A9 stream:", err));
</script>
</body>
</html>
"""

def make_handler(state, first_timeout, max_age):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _wait_first(self):
            end = time.time() + first_timeout

            while (
                self.jpeg0() is None
                and time.time() < end
                and not state.stop
            ):
                state.wait_newer(0, 0.5)

            return self.jpeg0()

        def jpeg0(self):
            with state.cond:
                return state.jpeg

        def stream_ts(self):
        
            output = av.open(
                self.wfile,
                mode="w",
                format="mpegts",
                options={
                    "mpegts_flags": "resend_headers",
                    "flush_packets": "1",
                },
                buffer_size=188 * 7,
            )

            video = output.add_stream("libx264", rate=10)
            video.width = 640
            video.height = 480
            video.pix_fmt = "yuv420p"
            video.time_base = Fraction(1, 10)
            video.codec_context.time_base = Fraction(1, 10)
            video.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "bf": "0",
                "g": "10",
                "sc_threshold": "0",
            }

            audio = output.add_stream("aac", rate=8000)
            audio.layout = "mono"
            audio.bit_rate = 64000
            audio.time_base = Fraction(1, 8000)
            audio.codec_context.time_base = Fraction(1, 8000)
            audio.codec_context.options = {"aac_coder": "twoloop"}

            video_pts = 0
            audio_pts = 0
            audio_pending = np.empty(0, dtype=np.int16)
            last_video = 0

            with state.audio_cond:
                last_audio = state.audio_seq

            def mux_packets(packets):
                for packet in packets:
                    output.mux(packet)

            stream_start = time.monotonic()
            last_video_pts = -1

            def encode_video(jpg):
                nonlocal video_pts, last_video_pts

                with av.open(
                    io.BytesIO(jpg), mode="r", format="mjpeg"
                ) as source:
                    frame = next(source.decode(video=0))

                frame = frame.reformat(
                    width=video.width,
                    height=video.height,
                    format="yuv420p",
                )

                # Timestamp based on elapsed time,
                # not on number of frames received.
                pts = int((time.monotonic() - stream_start) * 10)
                pts = max(pts, last_video_pts + 1)

                frame.pts = pts
                frame.time_base = Fraction(1, 10)

                video_pts = pts + 1
                last_video_pts = pts

                mux_packets(video.encode(frame))

            def encode_audio(samples):
                nonlocal audio_pts

                frame = av.AudioFrame.from_ndarray(
                    samples.reshape(1, -1),
                    format="s16",
                    layout="mono",
                )
                frame.sample_rate = 8000
                frame.pts = audio_pts
                frame.time_base = Fraction(1, 8000)
                audio_pts += len(samples)

                mux_packets(audio.encode(frame))

            try:
                while not state.stop:
                    jpg, last_video = state.wait_newer(
                        last_video, 0.01
                    )

                    if jpg is not None:
                        encode_video(jpg)

                    with state.audio_cond:
                        chunks = [
                            item for item in state.audio_chunks
                            if item[0] > last_audio
                        ]

                    for seq, timestamp, pcm in chunks:
                        audio_pending = np.concatenate((
                            audio_pending,
                            np.frombuffer(pcm, dtype="<i2"),
                        ))
                        last_audio = seq

                    while len(audio_pending) >= 1024:
                        block = audio_pending[:1024]
                        audio_pending = audio_pending[1024:]
                        encode_audio(block)

                    # If client slows down, flushes audio to avoid latency
                    if len(audio_pending) > 16000:
                        audio_pending = audio_pending[-8192:]

            finally:
                try:
                    mux_packets(video.encode(None))
                    if len(audio_pending) >= 1024:
                        encode_audio(audio_pending[:1024])
                    mux_packets(audio.encode(None))
                finally:
                    output.close()

        def do_GET(self):
            # only endpoints that relly need media
            # keep camera active.
            path = self.path.split("?", 1)[0]

            active = (
                path == "/live.ts"
                or path == "/stream.mjpg"
                or self.path == "/stream?raw=1"
                or path == "/snapshot.jpg"
                or path == "/snapshot"
            )

            if not active:
                return self._do_GET_inner()

            state.acquire_client()

            try:
                return self._do_GET_inner()
            finally:
                state.release_client()

        def _do_GET_inner(self):
        
            if self.path == "/live.ts":
                if self._wait_first() is None:
                    self.send_error(503, "no frame yet")
                    return

                self.send_response(200)
                self.send_header(
                    "Content-Type", "video/mp2t"
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()

                try:
                    self.stream_ts()

                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    OSError,
                ):
                    # player disconnected.
                    pass

                except Exception as exc:
                    # PyAV error management.
                    if type(exc).__name__ == "PyAVCallbackError":
                        sys.stderr.write(
                            "[MPEGTS] Client player disconnected.\n"
                        )
                    else:
                        sys.stderr.write(
                            f"[MPEGTS] error: {exc!r}\n"
                        )

                return        
        
            if self.path in ("/", "/index.html", "/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(VIEWER)))
                self.end_headers()
                self.wfile.write(VIEWER)
                return

            if self.path == "/stream?raw=1":
                if self._wait_first() is None:
                    self.send_error(503, "no frame yet")
                    return

                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/octet-stream"
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()

                def send_record(kind, timestamp, payload):
                    header = (
                        b"A9AV"
                        + struct.pack(
                            ">BQI", kind,
                            int(timestamp * 1_000_000),
                            len(payload)
                        )
                    )
                    self.wfile.write(header)
                    self.wfile.write(payload)
                    self.wfile.flush()

                last_video = 0
                with state.audio_cond:
                    last_audio = state.audio_seq

                try:
                    while not state.stop:
                        jpg, last_video = state.wait_newer(
                            last_video, 0.02
                        )

                        if jpg is not None:
                            send_record(1, time.monotonic(), jpg)

                        with state.audio_cond:
                            new_audio = [
                                item for item in state.audio_chunks
                                if item[0] > last_audio
                            ]

                        for seq, timestamp, pcm in new_audio:
                            send_record(2, timestamp, pcm)
                            last_audio = seq

                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

                return

            if self.path.startswith("/status"):
                import json as _json

                with state.cond:
                    st = dict(state.status) if state.status else {}
                    age = (
                        time.time() - state.last_frame_ts
                        if state.last_frame_ts
                        else None
                    )
                    st["connected"] = state.connected
                    st["streaming"] = (
                        age is not None and age < 3
                    )
                    st["last_frame_age_s"] = (
                        round(age, 1) if age is not None else None
                    )

                body = _json.dumps(st, indent=2).encode()
                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json"
                )
                self.send_header(
                    "Content-Length", str(len(body))
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/snapshot"):
                jpg = self._wait_first()

                if not jpg:
                    self.send_error(503, "no frame yet")
                    return

                with state.cond:
                    age = (
                        time.time() - state.last_frame_ts
                        if state.last_frame_ts
                        else 1e9
                    )

                if age > max_age:
                    self.send_error(
                        503, f"snapshot stale ({age:.1f}s old)"
                    )
                    return

                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpg)
                return

            if self.path.startswith("/stream.mjpg"):
                if self._wait_first() is None:
                    self.send_error(503, "no frame yet")
                    return

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=frame",
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                last = 0

                try:
                    while not state.stop:
                        jpg, last = state.wait_newer(last, 5.0)

                        if jpg is None:
                            continue

                        hdr = (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: "
                            + str(len(jpg)).encode()
                            + b"\r\n\r\n"
                        )

                        self.wfile.write(hdr)
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")

                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    OSError,
                ):
                    return

                return

            self.send_error(404)

    return H

def resolve_creds(a):
    return D.resolve_credentials(
        None if a.ip in (None, "auto") else a.ip,
        a.did,
        a.secret,
        a.config,
        a.no_config,
        a.disc_wait,
    )

def main():
    ap = argparse.ArgumentParser(
        description="live MJPEG/snapshot server for an A9 camera"
    )
    ap.add_argument("ip", nargs="?", help="camera IP")
    ap.add_argument("--did")
    ap.add_argument("--secret")
    ap.add_argument("--config", default="a9_camera.json")
    ap.add_argument("--no-config", action="store_true")
    ap.add_argument("--disc-wait", type=float, default=3.0)
    ap.add_argument(
        "--bind",
        default="127.0.0.1",
        help="HTTP bind address",
    )
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--max-age", type=float, default=10.0)
    ap.add_argument("--start-seq", type=int, default=3)

    a = ap.parse_args()

    ip, did, secret = resolve_creds(a)

    if not (ip and did and secret):
        sys.exit(
            "could not resolve camera IP + credentials "
            "(try --did/--secret, a9_camera.json, or discovery)"
        )

    sys.stderr.write(
        f"[*] camera {did.decode()} @ {ip}  (secret hidden)\n"
    )

    state = Latest()

    th = threading.Thread(
        target=camera_loop,
        args=(ip, did, secret, state, a.timeout, a.start_seq),
        daemon=True,
    )
    th.start()

    httpd = ThreadingHTTPServer(
        (a.bind, a.http_port),
        make_handler(state, a.timeout + 4, a.max_age),
    )

    url = f"http://{a.bind}:{a.http_port}/"

    sys.stderr.write(
        f"[*] serving {url}  "
        f"(stream {url}stream.mjpg, "
        f"still {url}snapshot.jpg, status {url}status, "
        f"stream with audio {url}stream, "
        f"vlc stream with audio {url}live.ts)\n"
    )

    if a.bind == "0.0.0.0":
        sys.stderr.write(
            "[!] bound to 0.0.0.0: the feed is UNAUTHENTICATED "
            "and reachable on the LAN.\n"
        )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[shutdown] Ctrl+C received\n")
    finally:
        # 1. Stops server and camera session.
        with state.cond:
            state.stop = True
            state.camera_wanted = False
            state.cond.notify_all()

        # 3. Stops TCP socket
        with state.socket_lock:
            s = state.camera_socket
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

        # 4. Closes HTTP server and waits for camera thread
        httpd.server_close()
        th.join(timeout=5.0)

        if th.is_alive():
            sys.stderr.write(
                "[shutdown] WARNING: camera thread  "
                "didn't stop in 5 seconds\n"
            )
        else:
            sys.stderr.write(
                "[shutdown] connection and camera thread stopped\n"
            )

if __name__ == "__main__":
    main()

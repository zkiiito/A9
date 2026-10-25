import importlib.util
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# These tests exercise the frame state and PCM decoder without invoking PyAV
# or NumPy. Keep the existing stdlib-only CI jobs usable.
spec = importlib.util.spec_from_file_location(
    "server_under_test", Path(__file__).resolve().parents[1] / "a9_mjpeg_server.py"
)
server = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {name: types.ModuleType(name) for name in ("av", "numpy")}):
    spec.loader.exec_module(server)


class ServerRegressionTests(unittest.TestCase):
    def test_alaw_preserves_full_scale_and_quiet_samples(self):
        # G.711 A-law values, including both signs and the loudest samples.
        encoded = bytes([0x00, 0x80, 0x55, 0xD5, 0x2A, 0xAA])
        self.assertEqual(
            struct.unpack("<6h", server.decode_alaw(encoded)),
            (-5504, 5504, -8, 8, -32256, 32256),
        )

    def test_reconnecting_snapshot_waits_for_a_fresh_frame(self):
        state = server.Latest()
        state.acquire_client()
        state.publish(b"old frame")
        state.release_client()
        state.acquire_client()
        handler = object.__new__(server.make_handler(state, 1.0, 10.0))

        # Simulate the publisher waking the condition. A retained sequence must
        # not cause the handler to skip waiting while the JPEG has been cleared.
        with patch.object(state.cond, "wait", side_effect=lambda _: state.publish(b"fresh frame")) as wait:
            self.assertEqual(handler._wait_first(), b"fresh frame")
            wait.assert_called_once()

    def test_first_frame_wait_times_out_without_media(self):
        state = server.Latest()
        handler = object.__new__(server.make_handler(state, 0.01, 10.0))
        with patch.object(state.cond, "wait", wraps=state.cond.wait) as wait:
            self.assertIsNone(handler._wait_first())
            wait.assert_called_once()

    def test_shutdown_does_not_wait_for_media(self):
        state = server.Latest()
        state.stop = True
        handler = object.__new__(server.make_handler(state, 1.0, 10.0))
        with patch.object(state.cond, "wait") as wait:
            self.assertIsNone(handler._wait_first())
            wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()

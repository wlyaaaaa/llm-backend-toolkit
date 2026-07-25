import json
import tempfile
import unittest
from pathlib import Path

from llm_backend_toolkit.media import MediaProcessor


class Completed:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class MediaProcessorTests(unittest.TestCase):
    def test_localocr_reads_the_declared_output_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "image.png"
            source.write_bytes(b"png")
            output = root / "outputs" / "result.txt"
            output.parent.mkdir()
            output.write_text("recognized text", encoding="utf-8")
            seen = []

            def runner(args, **kwargs):
                seen.append(args)
                payload = {"status": "succeeded", "results": [{"output_files": {"txt": "outputs/result.txt"}}]}
                return Completed(json.dumps(payload))

            processor = MediaProcessor(
                localocr_entry=str(root / "ocr_smart.ps1"),
                chineseasr_entry=None,
                runner=runner,
            )
            events = []
            result = processor.process(
                [{"id": "img", "path": str(source), "kind": "image", "route": "specialist"}],
                provider_supports_vision=True,
                progress_callback=events.append,
            )

            self.assertEqual("recognized text", result.supplemental_text[0]["text"])
            self.assertIn("-Engine", seen[0])
            self.assertIn("-StopAfter", seen[0])
            self.assertEqual([], result.native_images)
            self.assertEqual(
                ["media.ocr.started", "media.ocr.completed"],
                [event["public_event"]["kind"] for event in events],
            )

    def test_chineseasr_reads_final_output_and_never_uses_a_shell_string(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "audio.wav"
            source.write_bytes(b"wav")
            output = root / "final.txt"
            output.write_text("transcript", encoding="utf-8")
            seen = []

            def runner(args, **kwargs):
                seen.append((args, kwargs))
                return Completed(json.dumps({"status": "succeeded", "outputs": {"final": str(output)}}))

            processor = MediaProcessor(
                localocr_entry=None,
                chineseasr_entry=str(root / "asr-smart.ps1"),
                runner=runner,
            )
            events = []
            result = processor.process(
                [{"id": "audio", "path": str(source), "kind": "audio", "route": "specialist"}],
                provider_supports_vision=False,
                progress_callback=events.append,
            )

            self.assertEqual("transcript", result.supplemental_text[0]["text"])
            self.assertIsInstance(seen[0][0], list)
            self.assertFalse(seen[0][1].get("shell", False))
            wait_index = seen[0][0].index("-WaitSec")
            self.assertGreaterEqual(int(seen[0][0][wait_index + 1]), 300)
            self.assertEqual(
                ["media.asr.started", "media.asr.completed"],
                [event["public_event"]["kind"] for event in events],
            )

    def test_auto_keeps_general_images_native_and_routes_audio_to_specialist(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.png"
            image.write_bytes(b"png")
            processor = MediaProcessor(localocr_entry=None, chineseasr_entry=None)

            events = []
            result = processor.process(
                [{"id": "img", "path": str(image), "kind": "image"}],
                provider_supports_vision=True,
                progress_callback=events.append,
            )

            self.assertEqual([str(image.resolve())], result.native_images)
            self.assertEqual("native", result.routes[0]["route"])
            self.assertEqual("media.native.prepared", events[0]["public_event"]["kind"])


if __name__ == "__main__":
    unittest.main()

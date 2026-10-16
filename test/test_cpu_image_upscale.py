from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from services.cpu_image_upscale import parse_4k_size, upscale_to_4k
from services.protocol.conversation import ConversationRequest, format_image_result, stream_image_outputs


def make_png(size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(80, 120, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


def dimensions(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        return image.size


class CpuImageUpscaleTests(unittest.TestCase):
    def test_only_explicit_4k_sizes_trigger_upscale(self) -> None:
        self.assertEqual(parse_4k_size("3840x2160"), (3840, 2160))
        self.assertEqual(parse_4k_size("2160x3840"), (2160, 3840))
        self.assertIsNone(parse_4k_size("1024x1024"))
        source = make_png((80, 45))
        self.assertIs(upscale_to_4k(source, "1024x1024"), source)

    def test_landscape_result_is_real_4k_png(self) -> None:
        output = upscale_to_4k(make_png((80, 45)), "3840x2160")
        self.assertEqual(dimensions(output), (3840, 2160))

    def test_portrait_result_is_real_4k_png(self) -> None:
        output = upscale_to_4k(make_png((45, 80)), "2160x3840")
        self.assertEqual(dimensions(output), (2160, 3840))

    def test_existing_4k_png_is_returned_without_reencoding(self) -> None:
        source = make_png((3840, 2160))
        self.assertIs(upscale_to_4k(source, "3840x2160"), source)

    def test_invalid_image_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid image"):
            upscale_to_4k(b"not an image", "3840x2160")

    def test_b64_response_contains_upscaled_image_and_saves_same_bytes(self) -> None:
        source = make_png((80, 45))
        with (
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=False),
            mock.patch("services.protocol.conversation.image_storage_service.save") as save,
        ):
            result = format_image_result([{"_bytes": source}], "test", "b64_json", size="3840x2160")

        saved = save.call_args.args[0]
        self.assertEqual(dimensions(saved), (3840, 2160))
        self.assertEqual(base64.b64decode(result["data"][0]["b64_json"]), saved)

    def test_url_response_saves_upscaled_image(self) -> None:
        source = make_png((45, 80))
        with (
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=False),
            mock.patch("services.protocol.conversation.image_storage_service.save", return_value=SimpleNamespace(url="http://app.test/image.png")) as save,
        ):
            result = format_image_result([{"_bytes": source}], "test", "url", size="2160x3840")

        self.assertEqual(result["data"][0]["url"], "http://app.test/image.png")
        self.assertEqual(dimensions(save.call_args.args[0]), (2160, 3840))

    def test_generation_result_applies_requested_4k_size(self) -> None:
        backend = mock.Mock()
        backend.resolve_conversation_image_urls.return_value = ["https://files.test/generated.png"]
        backend.download_image_bytes.return_value = [make_png((80, 45))]
        event = {
            "type": "conversation.done", "conversation_id": "conv-1", "file_ids": ["file-ready"],
            "sediment_ids": [], "text": "", "turn_use_case": "image gen",
        }
        with (
            mock.patch("services.protocol.conversation.conversation_events", return_value=iter([event])),
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=False),
            mock.patch("services.protocol.conversation.image_storage_service.save"),
        ):
            outputs = list(stream_image_outputs(
                backend, ConversationRequest(model="gpt-image-2", prompt="test", size="3840x2160"),
            ))

        result = next(output for output in outputs if output.kind == "result")
        self.assertEqual(dimensions(base64.b64decode(result.data[0]["b64_json"])), (3840, 2160))

from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from services.image_storage_service import ImageStorageService
from services.protocol.conversation import ImageGenerationError, format_image_result


class ImageResponseFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.encoded = base64.b64encode(b"image-bytes").decode("ascii")

    def test_b64_json_saves_image_without_changing_response(self) -> None:
        with (
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=False),
            mock.patch("services.protocol.conversation.image_storage_service.save") as save,
        ):
            result = format_image_result([{"b64_json": self.encoded}], "cat", "b64_json")

        self.assertEqual(result["data"], [{"b64_json": self.encoded, "revised_prompt": "cat"}])
        save.assert_called_once_with(b"image-bytes", None, storage_mode=None)

    def test_b64_json_appears_in_image_manager_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.BytesIO()
            Image.new("RGB", (2, 2), color=(255, 0, 0)).save(output, format="PNG")
            image_data = output.getvalue()
            images_dir = Path(directory) / "images"
            with mock.patch("services.image_storage_service.config") as config:
                config.images_dir = images_dir
                config.base_url = "http://app.test"
                config.get_image_storage_settings.return_value = {"enabled": False, "mode": "local", "public_base_url": ""}
                storage = ImageStorageService(Path(directory) / "image_index.json")
                with (
                    mock.patch("services.protocol.conversation.image_storage_service", storage),
                    mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=False),
                ):
                    result = format_image_result([{"_bytes": image_data}], "cat", "b64_json", "http://app.test")
                items = storage.list_items("http://app.test")

            self.assertEqual(result["data"][0]["b64_json"], base64.b64encode(image_data).decode("ascii"))
            self.assertEqual(len(items), 1)
            self.assertTrue((images_dir / str(items[0]["rel"])).is_file())

    def test_url_uploads_and_only_returns_url(self) -> None:
        with mock.patch(
            "services.protocol.conversation.save_image_bytes",
            return_value="https://means.test/bucket/image.png",
        ) as save:
            result = format_image_result([{"b64_json": self.encoded}], "cat", "url")

        self.assertEqual(
            result["data"],
            [{"url": "https://means.test/bucket/image.png", "revised_prompt": "cat"}],
        )
        save.assert_called_once_with(b"image-bytes", None)

    def test_means_url_also_keeps_local_gallery_copy(self) -> None:
        with (
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=True),
            mock.patch("services.protocol.conversation.means_storage_service.upload", return_value="https://means.test/image.png") as upload,
            mock.patch("services.protocol.conversation.image_storage_service.save") as save,
        ):
            result = format_image_result([{"b64_json": self.encoded}], "cat", "url")

        self.assertEqual(result["data"][0]["url"], "https://means.test/image.png")
        upload.assert_called_once_with(b"image-bytes")
        save.assert_called_once_with(b"image-bytes", None, storage_mode="local")

    def test_b64_json_does_not_upload_to_means(self) -> None:
        with (
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=True),
            mock.patch("services.protocol.conversation.means_storage_service.upload") as upload,
            mock.patch("services.protocol.conversation.image_storage_service.save") as save,
        ):
            result = format_image_result([{"b64_json": self.encoded}], "cat", "b64_json")

        self.assertEqual(result["data"][0]["b64_json"], self.encoded)
        upload.assert_not_called()
        save.assert_called_once_with(b"image-bytes", None, storage_mode="local")

    def test_invalid_response_format_fails_before_formatting(self) -> None:
        with self.assertRaises(ImageGenerationError) as raised:
            format_image_result([{"b64_json": self.encoded}], "cat", "invalid")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.param, "response_format")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.log_service import LogService, LoggedCall
from services.protocol.conversation import ImageOutput, collect_image_outputs, format_image_result
from services.protocol.openai_v1_chat_complete import stream_image_chat_completion
from services.protocol.openai_v1_response import collect_response, stream_image_response


IMAGE_URL = "http://app.test/images/2026/10/16/generated.png"


class ImageCallLogUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = LogService(Path(self.tmp.name) / "logs.jsonl")
        self.encoded = base64.b64encode(b"image-bytes").decode("ascii")
        self.identity = {"id": "admin", "name": "admin", "role": "admin"}

    def image_result(self) -> dict:
        with (
            mock.patch("services.protocol.conversation.means_storage_service.enabled", return_value=False),
            mock.patch("services.protocol.conversation.image_storage_service.save", return_value=SimpleNamespace(url=IMAGE_URL)),
        ):
            return format_image_result([{"b64_json": self.encoded}], "cat", "b64_json")

    def test_nonstream_log_has_image_but_public_response_does_not(self) -> None:
        result = self.image_result()
        with mock.patch("services.log_service.log_service", self.logs):
            response = asyncio.run(LoggedCall(self.identity, "/v1/images/generations", "gpt-image-2", "文生图")
                                   .run(lambda: result))

        self.assertEqual(response["data"], [{"b64_json": self.encoded, "revised_prompt": "cat"}])
        self.assertNotIn("_log_urls", response)
        self.assertEqual(self.logs.list_page(page=1, page_size=10)["items"][0]["detail"]["urls"], [IMAGE_URL])

    def test_stream_log_has_image_but_public_chunk_does_not(self) -> None:
        result = self.image_result()
        output = ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
                             data=result["data"], log_urls=result["_log_urls"])
        with mock.patch("services.log_service.log_service", self.logs):
            chunks = list(LoggedCall(self.identity, "/v1/images/generations", "gpt-image-2", "文生图")
                          .stream([output.to_chunk()]))

        self.assertEqual(chunks[0]["data"], result["data"])
        self.assertNotIn("_log_urls", chunks[0])
        self.assertEqual(self.logs.list_page(page=1, page_size=10)["items"][0]["detail"]["urls"], [IMAGE_URL])

    def test_wrapped_image_responses_keep_log_url_private(self) -> None:
        result = self.image_result()
        output = ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
                             data=result["data"], log_urls=result["_log_urls"])
        self.assertEqual(collect_image_outputs([output])["_log_urls"], [IMAGE_URL])
        chat_chunk = next(iter(stream_image_chat_completion([output], "gpt-image-2")))
        self.assertEqual(chat_chunk["_log_urls"], [IMAGE_URL])
        response = collect_response(stream_image_response([output], "cat", "gpt-image-2"))
        self.assertEqual(response["_log_urls"], [IMAGE_URL])

        with mock.patch("services.log_service.log_service", self.logs):
            public_response = asyncio.run(LoggedCall(self.identity, "/v1/responses", "gpt-image-2", "Responses")
                                          .run(lambda: response))
        self.assertNotIn("_log_urls", public_response)
        self.assertEqual(self.logs.list_page(page=1, page_size=10)["items"][0]["detail"]["urls"], [IMAGE_URL])

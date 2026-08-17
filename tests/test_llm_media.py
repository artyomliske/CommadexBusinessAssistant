"""Как вложение попадает в запрос к модели.

PDF и картинка передаются по-разному, и у двух провайдеров — тоже
по-разному. Ошибка здесь тихая: формат числится поддерживаемым,
распознавание запускается, а шлюз отвечает отказом на каждый счёт.
"""

from __future__ import annotations

import base64

import pytest

from repairbot.llm.base import PDF_MEDIA_TYPE, LlmInvalidOutput, MediaPart, StructuredRequest
from repairbot.llm.claude import _user_content as claude_content
from repairbot.llm.reserve import _user_content as openai_content

PDF = MediaPart(media_type=PDF_MEDIA_TYPE, data=b"%PDF-1.4 ...", filename="счёт.pdf")
IMAGE = MediaPart(media_type="image/jpeg", data=b"\xff\xd8\xff")


def _request(*media: MediaPart) -> StructuredRequest:
    return StructuredRequest(
        stable_system="s",
        volatile_system="v",
        user_content="Разбери этот документ.",
        json_schema={},
        schema_name="t",
        media=tuple(media),
    )


# --- шлюз, совместимый с OpenAI (OpenRouter) ---


def test_pdf_goes_as_a_document_not_a_picture():
    """Отправленный картинкой PDF шлюз отклоняет — счета не читались бы."""
    blocks = openai_content(_request(PDF))

    assert blocks[0]["type"] == "file"
    assert blocks[0]["file"]["file_data"].startswith(f"data:{PDF_MEDIA_TYPE};base64,")


def test_pdf_carries_a_filename():
    """Без имени шлюз отвечает отказом на разбор вложения."""
    blocks = openai_content(_request(PDF))

    assert blocks[0]["file"]["filename"] == "счёт.pdf"


def test_nameless_pdf_still_gets_a_name():
    """Из мессенджера файл приходит и без имени — это не повод не читать."""
    blocks = openai_content(_request(MediaPart(media_type=PDF_MEDIA_TYPE, data=b"%PDF")))

    assert blocks[0]["file"]["filename"]


def test_image_stays_an_image():
    blocks = openai_content(_request(IMAGE))

    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_instruction_comes_after_the_document():
    """Модель отвечает точнее, когда инструкция читается последней."""
    blocks = openai_content(_request(PDF, IMAGE))

    assert [b["type"] for b in blocks] == ["file", "image_url", "text"]


def test_request_without_media_stays_a_plain_string():
    """Провайдеры, не принимающие списки блоков, не должны сломаться."""
    request = _request()

    assert openai_content(request) == "Разбери этот документ."


# --- родной протокол Anthropic ---


def test_claude_sends_pdf_as_a_document_block():
    blocks = claude_content(_request(PDF))

    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == PDF_MEDIA_TYPE


def test_claude_sends_image_as_an_image_block():
    blocks = claude_content(_request(IMAGE))

    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["data"] == base64.b64encode(IMAGE.data).decode()


# --- общее ---


@pytest.mark.parametrize("render", [openai_content, claude_content])
def test_unsupported_format_is_refused(render):
    """Отказ на понятном месте лучше, чем ответ модели про непонятный файл."""
    request = _request(MediaPart(media_type="application/zip", data=b"PK"))

    with pytest.raises(LlmInvalidOutput):
        render(request)

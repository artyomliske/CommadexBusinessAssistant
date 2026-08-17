"""Агент документов: что на фотографии и что с этого записать.

Раздел 3.2 ТЗ: распознавание чеков и приложенных документов. Один вызов
модели делает обе работы сразу — определяет род документа и, если это чек,
снимает с него суммы. Разделять их на два вызова незачем: картинка уже
передана, и второй проход стоил бы ровно столько же, сколько первый.

Порог достоверности для сумм здесь тот же, что и везде, — 0,8. Он взят не
из осторожности вообще, а из того, что распознавание рукописной или мятой
бумаги ошибается предсказуемо: цифры путаются местами, а нули теряются.
Чек с суммой ниже порога уходит менеджеру на подтверждение.

Персональные данные с чека не снимаем. На кассовом чеке бывает и карта, и
телефон покупателя; нам нужны сумма, дата и что куплено, а хранить чужой
номер карты в журнале — отдельная беда, которой можно не иметь.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repairbot.domain.facts import (
    ExtractionResult,
    Fact,
    FactType,
    WorkStatus,
    api_json_schema,
)
from repairbot.llm.base import (
    IMAGE_MEDIA_TYPES,
    PDF_MEDIA_TYPE,
    LlmInvalidOutput,
    MediaPart,
    StructuredRequest,
    StructuredResponse,
)
from repairbot.llm.router import LlmRouter
from repairbot.observability import get_logger

log = get_logger(__name__)

RECOGNIZABLE_MEDIA_TYPES = IMAGE_MEDIA_TYPES | {PDF_MEDIA_TYPE}

MAX_RECOGNIZED_BYTES = 5 * 1024 * 1024
"""Предел на размер картинки для распознавания.

Не тот же, что у архива: на Диск кладём всё, а в модель посылать
двадцатимегабайтную фотографию незачем — она всё равно ужимается на их
стороне, а платим мы за передачу."""


class DocClass:
    """Род документа. Значения ложатся в `attachments.doc_class`."""

    RECEIPT = "receipt"
    """Чек, накладная, счёт — то, с чего снимаются суммы."""
    MEASUREMENT = "measurement"
    """Замер: рулетка, размеры, план с цифрами."""
    CONTRACT = "contract"
    ACT = "act"
    PHOTO = "photo"
    """Фотоотчёт о работах — самый частый случай."""
    OTHER = "other"

    ALL = (RECEIPT, MEASUREMENT, CONTRACT, ACT, PHOTO, OTHER)


class ReceiptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(description="Наименование позиции как написано в документе")
    qty: float | None = Field(default=None, description="Количество")
    unit: str | None = Field(default=None, description="Единица измерения")
    amount: float | None = Field(default=None, description="Сумма по позиции")


class DocumentReading(BaseModel):
    """Что модель увидела на документе."""

    model_config = ConfigDict(extra="forbid")

    doc_class: str = Field(
        description="Одно из: receipt, measurement, contract, act, photo, other"
    )
    confidence: float = Field(description="Уверенность в прочтении, от 0 до 1")
    summary: str = Field(description="Одной строкой на русском: что это за документ")

    vendor: str | None = Field(default=None, description="Продавец или поставщик")
    doc_date: date | None = Field(default=None, description="Дата документа")
    total: float | None = Field(default=None, description="Итоговая сумма")
    currency: str | None = Field(default=None, description="Код валюты, обычно RUB")
    lines: list[ReceiptLine] = Field(
        default_factory=list, description="Позиции документа, если они читаются"
    )

    stage: str | None = Field(default=None, description="Этап работ, если виден на фото")
    room: str | None = Field(default=None, description="Помещение, если узнаётся")

    unreadable: bool = Field(
        default=False, description="True, если документ прочесть не удалось"
    )
    note: str | None = Field(default=None, description="Пояснение, если что-то неоднозначно")


@dataclass(slots=True)
class Recognition:
    reading: DocumentReading
    response: StructuredResponse
    facts: list[Fact] = field(default_factory=list)
    first_pass: StructuredResponse | None = None
    """Ответ дешёвой модели, если документ перечитывался дорогой.

    Нужен для учёта: платим за оба прохода, и не записать первый значит
    занизить расход ровно там, где его и смотрят."""

    @property
    def degraded(self) -> bool:
        return self.response.degraded


_INSTRUCTION = """\
Ты разбираешь документ или фотографию с объекта ремонта квартиры.

Сначала определи род документа:
* receipt — чек, накладная, счёт, квитанция: всё, где есть суммы;
* measurement — замер: рулетка у стены, размеры, план с цифрами;
* contract — договор; act — акт приёмки или выполненных работ;
* photo — фотоотчёт о ходе работ, самый частый случай;
* other — всё остальное.

Если это чек, сними с него продавца, дату, итоговую сумму и позиции. \
Цифры переписывай ровно так, как видишь. **Не досчитывай и не исправляй**: \
если итог на чеке не сходится с суммой позиций, так и оставь — это работа \
человека, а не твоя.

Если изображение мятое, размытое или обрезано так, что суммы не читаются, \
поставь unreadable = true и не угадывай. Честное «не разобрать» отправит \
чек человеку, и это нормальный исход.

Чего делать нельзя:
1. Не переписывай персональные данные: номера карт, телефоны, адреса \
покупателя. Нужны сумма, дата и что куплено.
2. Не выдумывай позиции, которых не видно.
3. confidence ставь по тому, насколько уверенно читается **сумма**, а не \
по тому, понятно ли, что это чек.

Для фотоотчёта укажи этап работ и помещение, если они узнаются, и оставь \
суммы пустыми.
"""


class DocumentAgent:
    def __init__(
        self,
        router: LlmRouter,
        *,
        max_bytes: int = MAX_RECOGNIZED_BYTES,
        model: str | None = None,
    ) -> None:
        self._router = router
        self._max_bytes = max_bytes
        self._model = model
        """Своя модель для документов. None — общая.

        Цена ошибки здесь другая: неверно прочитанная сумма попадает
        в бюджет объекта, тогда как невнятный разбор сообщения просто
        уйдёт человеку. Поэтому распознавание можно поднять отдельно."""
        self._schema = api_json_schema(DocumentReading)
        self._schema_json = json.dumps(self._schema, ensure_ascii=False, sort_keys=True)

    @property
    def schema_fingerprint(self) -> str:
        return hashlib.sha256(self._schema_json.encode()).hexdigest()[:12]

    async def recognize(
        self,
        content: bytes,
        *,
        media_type: str,
        filename: str | None = None,
        message_text: str | None = None,
        object_code: str | None = None,
        model: str | None = None,
    ) -> Recognition:
        """Прочитать документ и собрать из него факты.

        `model` перекрывает настроенную: дешёвая читает первой, дорогая
        перечитывает только то, где на кону деньги и уверенности нет.
        """
        if media_type not in RECOGNIZABLE_MEDIA_TYPES:
            raise LlmInvalidOutput(f"Формат {media_type} не распознаётся")
        if len(content) > self._max_bytes:
            raise LlmInvalidOutput(
                f"Файл {len(content) // 1024} КБ больше предела распознавания "
                f"{self._max_bytes // 1024} КБ"
            )

        response = await self._router.complete_structured(
            StructuredRequest(
                stable_system=_INSTRUCTION,
                volatile_system=_render_context(filename, message_text, object_code),
                user_content="Разбери этот документ.",
                json_schema=self._schema,
                schema_name="document_reading",
                max_output_tokens=4096,
                media=(
                    MediaPart(media_type=media_type, data=content, filename=filename),
                ),
                model=model or self._model,
            )
        )

        try:
            reading = DocumentReading.model_validate(response.payload)
        except ValidationError as exc:
            raise LlmInvalidOutput(f"Ответ не соответствует схеме: {exc.error_count()}") from exc

        recognition = Recognition(reading=reading, response=response)
        recognition.facts = facts_from(reading)

        log.info(
            "documents.recognized",
            doc_class=reading.doc_class,
            unreadable=reading.unreadable,
            confidence=reading.confidence,
            facts=len(recognition.facts),
            degraded=response.degraded,
        )
        return recognition


def facts_from(reading: DocumentReading) -> list[Fact]:
    """Превратить прочтение в факты для журнала.

    Из чека получается ровно один факт закупки, а не по факту на позицию:
    журнал ведётся о деньгах объекта, и десять строк одной накладной —
    это одна трата, а не десять. Позиции остаются в описании факта.
    """
    if reading.unreadable:
        return []

    confidence = max(0.0, min(1.0, reading.confidence))

    if reading.doc_class == DocClass.RECEIPT:
        if reading.total is None:
            # Чек без итога факта не даёт: сумма — единственное, ради чего
            # его разбирали. Пусть его посмотрит человек.
            return []
        return [
            Fact(
                type=FactType.PURCHASE,
                confidence=confidence,
                summary=_receipt_summary(reading),
                item=_main_item(reading),
                amount=reading.total,
                currency=reading.currency or "RUB",
                due_date=reading.doc_date,
            )
        ]

    if reading.doc_class == DocClass.MEASUREMENT:
        return [
            Fact(
                type=FactType.MEASUREMENT,
                confidence=confidence,
                summary=reading.summary,
                room=reading.room,
                stage=reading.stage,
            )
        ]

    if reading.doc_class == DocClass.PHOTO and (reading.stage or reading.room):
        # Фотоотчёт о ходе работ. Состояние этапа с фотографии не берём:
        # видно, что работа идёт, но не видно, что она закончена.
        return [
            Fact(
                type=FactType.WORK_PROGRESS,
                confidence=confidence,
                summary=reading.summary,
                stage=reading.stage,
                room=reading.room,
                status=WorkStatus.IN_PROGRESS,
            )
        ]

    # Договоры и акты в факты не превращаем: они не о ходе работ, и место
    # им в архиве, а не в журнале. Класс документа при этом сохраняется.
    return []


def as_extraction(recognition: Recognition) -> ExtractionResult:
    """Свести распознавание к тому же виду, что и разбор текста.

    Конвейер записи фактов один на всех, и отдельный путь для документов
    означал бы два места, где надо не забыть про порог достоверности.
    """
    reading = recognition.reading
    needs_human = reading.unreadable or (
        reading.doc_class == DocClass.RECEIPT and reading.total is None
    )
    note = reading.note
    if reading.unreadable:
        note = note or "документ не удалось прочесть"
    elif needs_human:
        note = note or "на чеке не читается итоговая сумма"

    return ExtractionResult(facts=recognition.facts, needs_human=needs_human, note=note)


def _receipt_summary(reading: DocumentReading) -> str:
    parts = ["Закупка"]
    if reading.vendor:
        parts.append(f"у «{reading.vendor}»")
    if reading.total is not None:
        parts.append(f"на {reading.total:.2f} {reading.currency or 'RUB'}")
    items = ", ".join(line.item for line in reading.lines[:3] if line.item)
    if items:
        parts.append(f"— {items}")
        if len(reading.lines) > 3:
            parts.append(f"и ещё {len(reading.lines) - 3}")
    return " ".join(parts)


def _main_item(reading: DocumentReading) -> str | None:
    """Позиция с наибольшей суммой — она задаёт смысл закупки.

    Первая строка чека для этого не годится: там нередко стоит доставка
    или мелочь, пробитая раньше основного товара.
    """
    priced = [line for line in reading.lines if line.amount is not None]
    if priced:
        return max(priced, key=lambda line: line.amount or 0).item
    return reading.lines[0].item if reading.lines else None


def _render_context(
    filename: str | None, message_text: str | None, object_code: str | None
) -> str:
    """Контекст вокруг документа.

    Подпись к фотографии часто содержит то, чего на ней не видно
    («грунтовка на кухню»). Модели она помогает, но верить ей вперёд
    изображения нельзя — об этом сказано прямо.
    """
    lines: list[str] = []
    if object_code:
        lines.append(f"Объект: {object_code}")
    if filename:
        lines.append(f"Имя файла: {filename}")
    if message_text:
        lines.append(f"Подпись к вложению: {message_text.strip()[:500]}")
        lines.append(
            "Подпись — подсказка, а не источник. Суммы и даты бери только "
            "с самого изображения."
        )
    return "\n".join(lines) if lines else "Дополнительных сведений нет."


def guess_media_type(mime_type: str | None, filename: str | None, kind: str) -> str | None:
    """Определить формат вложения для распознавания.

    MAX присылает `mime_type` не всегда, поэтому подстраховываемся именем
    файла и родом вложения. Неизвестный формат — не ошибка: такие
    вложения просто не распознаются.
    """
    if mime_type in RECOGNIZABLE_MEDIA_TYPES:
        return mime_type

    name = (filename or "").lower()
    for suffix, media_type in (
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".png", "image/png"),
        (".webp", "image/webp"),
        (".gif", "image/gif"),
        (".pdf", PDF_MEDIA_TYPE),
    ):
        if name.endswith(suffix):
            return media_type

    # Фотография из мессенджера приходит без имени и без mime.
    return "image/jpeg" if kind == "image" else None


FINANCIAL_DOC_CLASSES: frozenset[str] = frozenset(
    {DocClass.RECEIPT, DocClass.CONTRACT, DocClass.ACT}
)
"""Документы, с которых снимаются деньги.

Фотоотчёт можно прочесть дешёвой моделью: ошибка в слове «санузел»
стоит недоразумения. Ошибка в сумме с чека попадает в бюджет объекта."""

CAREFUL_REREAD_BELOW = 0.9
"""Ниже какой уверенности финансовый документ перечитывается дорогой моделью.

Порог высокий намеренно: перечитать лишний раз стоит шесть центов, а
неверная сумма в бюджете объекта — рабочего дня разбирательств."""


def needs_careful_reading(reading: DocumentReading) -> bool:
    """Стоит ли перечитать документ дорогой моделью.

    Да — если это документ про деньги и дешёвая модель либо не уверена,
    либо не смогла прочесть. Нет — для фотоотчётов и уверенно
    прочитанных чеков: там переплата ничего не покупает.
    """
    if reading.doc_class not in FINANCIAL_DOC_CLASSES:
        return False
    return reading.unreadable or reading.confidence < CAREFUL_REREAD_BELOW


#: Подписи в начале файла. Порядок важен: WebP опознаётся по двум кускам.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF", PDF_MEDIA_TYPE),
)


def sniff_media_type(content: bytes) -> str | None:
    """Определить формат по содержимому файла.

    Нужно потому, что MAX присылает фотографии без имени и без типа, а
    подставленный наугад `image/jpeg` — это ложь, которую модель ловит:
    «указан image/jpeg, но файл похож на image/webp», и документ не
    читается вовсе. Содержимое не врёт, в отличие от догадки по виду
    вложения.
    """
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    for magic, media_type in _MAGIC:
        if content.startswith(magic):
            return media_type
    return None

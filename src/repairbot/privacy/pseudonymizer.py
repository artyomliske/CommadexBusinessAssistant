"""Псевдонимизация персональных данных (раздел 7 ТЗ).

Перед отправкой во внешнюю языковую модель имена, телефоны, адреса и
электронная почта заменяются на условные идентификаторы по детерминированному
словарю; обратная подстановка производится при формировании ответа.

Детерминированность важна по двум причинам: одно и то же имя в разных
сообщениях получает один и тот же идентификатор, поэтому модель видит
связность диалога; и подстановка воспроизводима при разборе инцидентов.

Соответствие требованиям 152-ФЗ подлежит согласованию с юристом заказчика —
этот модуль реализует техническую часть, а не юридическую оценку.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Телефоны РФ в любых бытовых написаниях: +7, 8, скобки, дефисы, пробелы.
_PHONE_RE = re.compile(
    r"(?<![\d\-])(?:\+7|7|8)\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?![\d\-])"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[\s\-]?){3}\d{4}(?!\d)")

# Адрес: «ул. Ленина 5», «улица Мира, д. 12, кв. 3».
_ADDRESS_RE = re.compile(
    r"\b(?:ул\.?|улица|пр-?кт\.?|проспект|пер\.?|переулок|бул\.?|бульвар|ш\.?|шоссе|наб\.?|набережная)"
    r"\s+[А-ЯЁA-Z][\w\-]*(?:\s+[А-ЯЁA-Z]?[\w\-]+)?"
    r"(?:\s*,?\s*(?:д\.?|дом)\s*\d+[а-яё]?)?"
    r"(?:\s*,?\s*(?:к\.?|корп\.?|корпус)\s*\d+)?"
    r"(?:\s*,?\s*(?:кв\.?|квартира)\s*\d+)?",
    re.IGNORECASE,
)

# Отдельно стоящая квартира без улицы: «кв. 12».
_FLAT_RE = re.compile(r"\b(?:кв\.?|квартира)\s*\d+\b", re.IGNORECASE)


@dataclass(slots=True)
class Pseudonymizer:
    """Двусторонний словарь замен для одного вызова модели.

    Экземпляр живёт на протяжении одного обращения к модели: сначала
    `mask` для входа, затем `unmask` для выхода. Словарь имён сотрудников
    и заказчиков передаётся снаружи — так один человек получает
    стабильный идентификатор между вызовами.
    """

    known_names: dict[str, str] = field(default_factory=dict)
    """Реальное имя → условный идентификатор. Пополняется из карточек людей."""

    _forward: dict[str, str] = field(default_factory=dict, init=False)
    """Нормализованный ключ значения → условный идентификатор."""

    _reverse: dict[str, str] = field(default_factory=dict, init=False)
    """Условный идентификатор → значение в том виде, в каком оно встретилось
    впервые. Нормализованный ключ для этого не годится: у телефона он
    содержит только цифры, а вернуть человеку нужно исходное написание."""

    _counters: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for real, alias in self.known_names.items():
            self._forward[self._normalize("NAME", real)] = alias
            self._reverse.setdefault(alias, real)

    # --- прямое направление ---

    def mask(self, text: str) -> str:
        """Заменить персональные данные на условные идентификаторы."""
        if not text:
            return text

        masked = text
        # Порядок важен: карты и телефоны состоят из цифр и могут
        # перекрываться с номерами домов, поэтому идут раньше адресов.
        masked = _CARD_RE.sub(lambda m: self._alias("CARD", m.group(0)), masked)
        masked = _PHONE_RE.sub(lambda m: self._alias("PHONE", m.group(0)), masked)
        masked = _EMAIL_RE.sub(lambda m: self._alias("EMAIL", m.group(0)), masked)
        masked = _ADDRESS_RE.sub(lambda m: self._alias("ADDR", m.group(0)), masked)
        masked = _FLAT_RE.sub(lambda m: self._alias("ADDR", m.group(0)), masked)
        masked = self._mask_known_names(masked)
        return masked

    def _mask_known_names(self, text: str) -> str:
        """Заменить известные имена.

        Свободный поиск имён по тексту не выполняется: в рабочей переписке
        слишком много слов с заглавной буквы (марки материалов, названия
        помещений), и ложные замены искажают смысл сообщения. Заменяются
        только имена из справочника людей.
        """
        for real in sorted(self.known_names, key=len, reverse=True):
            if not real:
                continue
            pattern = re.compile(rf"\b{re.escape(real)}\b", re.IGNORECASE)
            text = pattern.sub(self.known_names[real], text)
        return text

    def _alias(self, kind: str, value: str) -> str:
        normalized = self._normalize(kind, value)
        existing = self._forward.get(normalized)
        if existing is not None:
            return existing

        self._counters[kind] = self._counters.get(kind, 0) + 1
        alias = f"[{kind}_{self._counters[kind]}]"
        self._forward[normalized] = alias
        self._reverse[alias] = value
        return alias

    @staticmethod
    def _normalize(kind: str, value: str) -> str:
        """Свести разные написания одного значения к одному ключу."""
        if kind in ("PHONE", "CARD"):
            digits = re.sub(r"\D", "", value)
            if kind == "PHONE" and len(digits) == 11 and digits[0] == "8":
                digits = "7" + digits[1:]
            return f"{kind}:{digits}"
        return f"{kind}:{' '.join(value.split()).casefold()}"

    # --- обратное направление ---

    def unmask(self, text: str) -> str:
        """Подставить обратно реальные значения.

        Применяется к текстам, которые уйдут человеку. Внутрь журнала и
        таблиц значения попадают уже раскрытыми.
        """
        if not text:
            return text
        # Длинные идентификаторы первыми: [ADDR_10] не должен пострадать от [ADDR_1].
        for alias in sorted(self._reverse, key=len, reverse=True):
            if alias in text:
                text = text.replace(alias, self._reverse[alias])
        return text

    @property
    def replacements(self) -> dict[str, str]:
        """Условный идентификатор → исходное значение. Для журнала аудита."""
        return dict(self._reverse)

    def __len__(self) -> int:
        return len(self._forward)

"""Границы автономности (раздел 6 ТЗ).

Принятая политика: самостоятельные действия разрешены **за исключением
вопросов стоимости и сроков**.

Здесь две линии обороны, и вторая важнее первой.

Первая — намерение: агент сам объявляет, что он делает, и по типу
намерения понятно, нужно ли подтверждение. Но намерение назначает
языковая модель, а она может ошибиться или быть сбита содержимым
переписки.

Вторая — сканер содержания: детерминированная проверка самого текста.
Если в «сообщении о статусе работ» оказалась сумма или дата, решение
поднимается до подтверждения независимо от того, чем его назвали. Раздел 6
ТЗ прямо требует, чтобы ограничения жили на уровне контролёра, а не
системного промпта, — вот это оно и есть.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Intent(StrEnum):
    """Что именно система собирается сделать."""

    # --- разрешено без подтверждения (раздел 6) ---
    STATUS_UPDATE = "status_update"
    """Текущий статус работ, выполненные этапы."""
    SCHEDULE_DIGEST = "schedule_digest"
    """График на ближайшие дни — уже известный, не новый."""
    ORGANIZATIONAL = "organizational"
    """Организационные вопросы: доступ в квартиру, время визита."""
    CLARIFYING_QUESTION = "clarifying_question"
    """Уточняющий вопрос в рабочем чате."""
    INTERNAL_NOTICE = "internal_notice"
    """Внутреннее уведомление сотруднику."""
    MANAGER_DIGEST = "manager_digest"
    """Сводка руководителю."""
    CRM_TASK = "crm_task"
    """Постановка задачи в CRM."""

    # --- только с подтверждением (раздел 6) ---
    FINANCIAL = "financial"
    """Любые сведения о деньгах: цены, сметы, доплаты, скидки."""
    DEADLINE_TO_CLIENT = "deadline_to_client"
    """Сроки и даты, сообщаемые заказчику."""
    COMPLAINT_RESPONSE = "complaint_response"
    """Ответ на претензию или конфликтное обращение."""
    LEGAL = "legal"
    """Договоры, акты, гарантии."""
    EXTERNAL_CORRESPONDENCE = "external_correspondence"
    """Внешняя переписка с заказчиками и поставщиками."""
    DATA_DELETION = "data_deletion"
    """Удаление данных."""


AUTONOMOUS_INTENTS: frozenset[Intent] = frozenset({
    Intent.STATUS_UPDATE,
    Intent.SCHEDULE_DIGEST,
    Intent.ORGANIZATIONAL,
    Intent.CLARIFYING_QUESTION,
    Intent.INTERNAL_NOTICE,
    Intent.MANAGER_DIGEST,
    Intent.CRM_TASK,
})

CONFIRMATION_INTENTS: frozenset[Intent] = frozenset(set(Intent) - AUTONOMOUS_INTENTS)


class Audience(StrEnum):
    CLIENT = "client"
    """Заказчик квартиры. Самая осторожная политика."""
    STAFF = "staff"
    """Сотрудник: прораб, бригада, снабженец."""
    MANAGER = "manager"
    """Менеджер или руководитель."""
    SUPPLIER = "supplier"


class Verdict(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"
    """Черновик менеджеру: «Отправить», «Изменить», «Отклонить»."""
    BLOCK = "block"
    """Не отправлять и не предлагать. Решение окончательное."""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    triggered_by: tuple[str, ...] = ()
    """Что именно в тексте подняло решение — для журнала аудита."""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


# --- сканер содержания ---

_MONEY_RE = re.compile(
    r"\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб|р\.|рубл|тыс|тысяч|k\b)"
    r"|(?:стоим|цена|цену|ценой|смет|доплат|скидк|аванс|предоплат|оплат|платеж|платёж)",
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"
    r"|\b\d{1,2}\s+(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)"
    r"|\b(?:завтра|послезавтра|сегодня|на следующей неделе|через \d+ дн|"
    r"к (?:понедельник|вторник|сред|четверг|пятниц|субботе|воскресен))",
    re.IGNORECASE,
)

_PROMISE_RE = re.compile(
    r"(?:гарантиру|обязу|мы обещаем|точно (?:успе|сдел)|сдадим|закончим к|"
    r"будет готово|не позднее|в срок)",
    re.IGNORECASE,
)

_LEGAL_RE = re.compile(
    r"(?:договор|акт (?:приём|прием|выполн)|гаранти|претензи|неустойк|"
    r"штраф|расторж|юрист|суд)",
    re.IGNORECASE,
)

_COMPLAINT_RE = re.compile(
    r"(?:жалоб|претензи|недовол|возмущ|отвратительн|ужасн|верните деньги|"
    r"некачествен|переделыва|брак|обман)",
    re.IGNORECASE,
)


def looks_like_complaint(text: str) -> bool:
    """Похоже ли **входящее** сообщение на претензию.

    Применяется к тому, что написал заказчик, а не к нашему ответу:
    раздел 6 требует подтверждения для ответов на претензии и конфликтные
    обращения, а распознать их надо до того, как отвечать.
    """
    return bool(text and _COMPLAINT_RE.search(text))


@dataclass(slots=True)
class ContentFlags:
    money: bool = False
    dates: bool = False
    promises: bool = False
    legal: bool = False
    matches: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return self.money or self.dates or self.promises or self.legal


def scan(text: str) -> ContentFlags:
    """Найти в тексте признаки, требующие человека.

    Сканер намеренно грубый и склонен к ложным срабатываниям. Лишний
    черновик менеджеру стоит минуты его времени; неверная сумма или срок,
    ушедшие заказчику, стоят доверия и денег.
    """
    flags = ContentFlags()
    if not text:
        return flags

    for name, pattern in (
        ("money", _MONEY_RE),
        ("dates", _DATE_RE),
        ("promises", _PROMISE_RE),
        ("legal", _LEGAL_RE),
    ):
        found = pattern.search(text)
        if found:
            setattr(flags, name, True)
            flags.matches.append(f"{name}:{found.group(0).strip()[:40]}")
    return flags


def evaluate(
    intent: Intent,
    *,
    audience: Audience,
    text: str,
    is_first_contact: bool = False,
    discloses_automation: bool = False,
) -> PolicyDecision:
    """Решить судьбу исходящего сообщения по разделу 6 ТЗ."""
    reasons: list[str] = []
    triggered: list[str] = []

    if intent in CONFIRMATION_INTENTS:
        reasons.append(f"намерение «{intent.value}» требует подтверждения")

    flags = scan(text)
    triggered.extend(flags.matches)

    # Сканер применяется ко всему, что уходит наружу. Внутренним адресатам
    # сумма и дата не опасны: они их и так знают, а вот заказчику — да.
    external = audience in (Audience.CLIENT, Audience.SUPPLIER)

    if external:
        if flags.money:
            reasons.append("в тексте есть денежные сведения")
        if flags.dates and intent is not Intent.SCHEDULE_DIGEST:
            reasons.append("в тексте есть даты или сроки")
        if flags.promises:
            reasons.append("в тексте есть обещание или гарантия")
        if flags.legal:
            reasons.append("в тексте есть юридически значимые формулировки")

        # График на ближайшие дни разрешён без подтверждения, но только
        # если это пересказ уже известного плана. Обещание внутри такого
        # сообщения превращает его в обязательство перед заказчиком.
        if intent is Intent.SCHEDULE_DIGEST and (flags.promises or flags.money):
            reasons.append("сводка графика содержит обязательство")

    if audience is Audience.CLIENT and is_first_contact and not discloses_automation:
        # Явное представление системы как автоматической при первом
        # контакте — предохранитель из раздела 6.
        reasons.append("первый контакт без представления системы как автоматической")

    verdict = Verdict.HOLD if reasons else Verdict.ALLOW
    return PolicyDecision(
        verdict=verdict,
        reasons=tuple(reasons),
        triggered_by=tuple(triggered),
    )

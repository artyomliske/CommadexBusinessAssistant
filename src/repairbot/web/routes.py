"""Страницы веб-интерфейса.

Серверный рендеринг: страница отдаётся собранной, HTMX подменяет отдельные
фрагменты. Сборки нет, Node не нужен, на VPS это один процесс.

Наружу интерфейс не ходит: у веб-процесса нет клиента канала. Одобренный
черновик ставится в очередь, а отправляет его воркер — и по дороге ещё раз
проходит проверки контролёра.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot import text
from repairbot.agents import payment_calendar
from repairbot.config import Settings, get_settings
from repairbot.db.models import User
from repairbot.db.session import db_session
from repairbot.domain.payments import Category as PaymentCategory
from repairbot.domain.payments import Period as PaymentPeriod
from repairbot.llm import pricing
from repairbot.observability import get_logger
from repairbot.outbound.controller import SWITCH_UNAVAILABLE
from repairbot.web import cash, drafts, knowledge, objects, people, queries, review
from repairbot.web.security import (
    MIN_PASSWORD_LENGTH,
    SESSION_USER_KEY,
    authenticate,
    change_password,
    current_user,
    reviewer,
    safe_next,
)

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    """Версия статики по времени изменения файлов.

    Без неё браузер продолжает отдавать закэшированный CSS после
    выкладки — стиль меняется, а у пользователя остаётся прежний, и
    выглядит это как «ничего не поменялось».
    """
    stamps = [p.stat().st_mtime_ns for p in STATIC_DIR.glob("*") if p.is_file()]
    return format(max(stamps, default=0) % 0xFFFFFFFF, "x")


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["asset_version"] = _asset_version()


def _dt(value: datetime | None) -> str:
    """Дата и время. Год добавляется, только если он не текущий.

    Без этого событие прошлого года показывается как «05.07» и читается
    как позавчерашнее — а на странице объекта рядом лежат записи за
    полгода, и перепутать их легко.
    """
    if not value:
        return "—"
    if value.year != datetime.now(tz=UTC).year:
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m %H:%M")


templates.env.filters["dt"] = _dt
templates.env.filters["day"] = lambda value: value.strftime("%d.%m.%Y") if value else "—"
templates.env.filters["thousands"] = lambda value: f"{int(value):,}".replace(",", " ")
# «1 учётных записей» и «1 чат(ов)» — то, из-за чего интерфейс выглядит
# недоделанным. Склонение вынесено в одно место (см. repairbot.text).
templates.env.filters["count"] = text.count


def _iso_dt(value: object) -> str:
    """Дата-время из строки ISO.

    В `objects.state` время лежит строками (это JSON), и обычный фильтр
    для `datetime` их не берёт — на странице оказывался сырой
    `2026-07-05T17:17:38.788007+00:00`.
    """
    if not value:
        return "—"
    try:
        return _dt(datetime.fromisoformat(str(value)))
    except ValueError:
        return str(value)


templates.env.filters["isodt"] = _iso_dt

router = APIRouter()

CurrentUser = Annotated[User, Depends(current_user)]
Reviewer = Annotated[User, Depends(reviewer)]
Session = Annotated[AsyncSession, Depends(db_session)]
Config = Annotated[Settings, Depends(get_settings)]


def _page(request: Request, name: str, user: User | None, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"user": user, **context},
    )


# --- вход ---


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request, next: Annotated[str | None, Query()] = None
) -> Response:
    if request.session.get(SESSION_USER_KEY):
        return RedirectResponse(safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return _page(request, "login.html", None, next=safe_next(next), error=None)


@router.post("/login")
async def login_submit(
    request: Request,
    session: Session,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    user = await authenticate(session, login, password)
    if user is None:
        # Не уточняем, что именно неверно: логин или пароль.
        return _page(
            request,
            "login.html",
            None,
            next=safe_next(next),
            error="Неверный логин или пароль",
        )

    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse(safe_next(next), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# --- свой пароль ---


@router.get("/password", response_class=HTMLResponse)
async def password_form(request: Request, user: CurrentUser) -> HTMLResponse:
    return _page(
        request, "password.html", user, error=None, done=False, min_length=MIN_PASSWORD_LENGTH
    )


@router.post("/password")
async def password_submit(
    request: Request,
    session: Session,
    user: CurrentUser,
    current: Annotated[str, Form()],
    new: Annotated[str, Form()],
    repeat: Annotated[str, Form()],
) -> Response:
    """Сменить свой пароль. Решение принимает `change_password`."""
    error = change_password(user, current, new, repeat)
    if error is None:
        session.add(user)
        await session.flush()
        log.info("web.password_changed", user_id=user.id)
    else:
        log.warning("web.password_change_failed", user_id=user.id, reason=error)

    return _page(
        request,
        "password.html",
        user,
        error=error,
        done=error is None,
        min_length=MIN_PASSWORD_LENGTH,
    )


# --- обзор ---


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    data = await queries.load_overview(session)
    return _page(request, "overview.html", user, overview=data)


# --- объекты и их названия ---


async def _objects_page(
    request: Request, session: AsyncSession, user: User, message: str | None = None, ok: bool = True
) -> HTMLResponse:
    return _page(
        request,
        "objects.html",
        user,
        objects=await objects.load_objects(session),
        message=message,
        ok=ok,
    )


@router.get("/objects", response_class=HTMLResponse)
async def objects_page(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    return await _objects_page(request, session, user)


@router.post("/objects", response_class=HTMLResponse)
async def objects_create(
    request: Request,
    session: Session,
    user: Reviewer,
    address: Annotated[str, Form()],
    code: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        obj = await objects.create_object(session, address=address, code=code, by=user.login)
    except objects.ObjectsError as exc:
        await session.rollback()
        return await _objects_page(request, session, user, message=str(exc), ok=False)

    await session.commit()
    return await _objects_page(request, session, user, message=f"Объект «{obj.address}» заведён")


@router.post("/objects/{object_id}/aliases", response_class=HTMLResponse)
async def objects_add_alias(
    request: Request,
    session: Session,
    user: Reviewer,
    object_id: int,
    alias: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        record = await objects.add_alias(session, object_id, alias, by=user.login)
    except objects.ObjectsError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()
    return _page(
        request,
        "partials/review_result.html",
        user,
        message=f"«{record.alias}» — теперь это название объекта",
        ok=True,
    )


@router.post("/objects/aliases/{alias_id}/remove", response_class=HTMLResponse)
async def objects_remove_alias(
    request: Request, session: Session, user: Reviewer, alias_id: int
) -> HTMLResponse:
    try:
        await objects.remove_alias(session, alias_id, by=user.login)
    except objects.ObjectsError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()
    return _page(request, "partials/review_result.html", user, message="Название убрано", ok=True)


# --- касса ---


@router.get("/cash", response_class=HTMLResponse)
async def cash_page(
    request: Request,
    session: Session,
    user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> HTMLResponse:
    since = (datetime.now(tz=UTC) - timedelta(days=days)).date()
    return _page(
        request,
        "cash.html",
        user,
        balances=await cash.load_balances(session, since=since),
        days=days,
    )


# --- знания о компании ---


async def _knowledge_page(
    request: Request,
    session: AsyncSession,
    user: User,
    message: str | None = None,
    ok: bool = True,
) -> HTMLResponse:
    return _page(
        request,
        "knowledge.html",
        user,
        notes=await knowledge.load_notes(session),
        used=await knowledge.total_chars(session),
        limit=knowledge.MAX_TOTAL_CHARS,
        body_limit=knowledge.MAX_BODY_CHARS,
        message=message,
        ok=ok,
    )


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    return await _knowledge_page(request, session, user)


@router.post("/knowledge", response_class=HTMLResponse)
async def knowledge_create(
    request: Request,
    session: Session,
    user: Reviewer,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        note = await knowledge.create_note(session, title=title, body=body, by=user.login)
    except knowledge.KnowledgeError as exc:
        await session.rollback()
        return await _knowledge_page(request, session, user, message=str(exc), ok=False)

    await session.commit()
    return await _knowledge_page(request, session, user, message=f"Записал: {note.title}")


@router.post("/knowledge/{note_id}/toggle", response_class=HTMLResponse)
async def knowledge_toggle(
    request: Request, session: Session, user: Reviewer, note_id: int
) -> HTMLResponse:
    try:
        note = await knowledge.toggle_note(session, note_id, by=user.login)
    except knowledge.KnowledgeError as exc:
        await session.rollback()
        return await _knowledge_page(request, session, user, message=str(exc), ok=False)

    await session.commit()
    state = "в работе" if note.active else "выключено"
    return await _knowledge_page(request, session, user, message=f"«{note.title}» — {state}")


@router.post("/knowledge/{note_id}/delete", response_class=HTMLResponse)
async def knowledge_delete(
    request: Request, session: Session, user: Reviewer, note_id: int
) -> HTMLResponse:
    try:
        await knowledge.delete_note(session, note_id, by=user.login)
    except knowledge.KnowledgeError as exc:
        await session.rollback()
        return await _knowledge_page(request, session, user, message=str(exc), ok=False)

    await session.commit()
    return await _knowledge_page(request, session, user, message="Запись удалена")


# --- файлы на Диске ---


@router.get("/files", response_class=HTMLResponse)
async def files_page(
    request: Request, session: Session, user: CurrentUser, settings: Config
) -> HTMLResponse:
    cards = await objects.load_files(session)
    return _page(
        request,
        "files.html",
        user,
        files=cards,
        inbox=sum(1 for c in cards if c.in_inbox),
        doc_titles=objects.DOC_TITLES,
        root_url=objects.folder_url(settings.google_drive_folder_id),
    )


# --- разбор неузнанного ---


@router.get("/unassigned", response_class=HTMLResponse)
async def unassigned_page(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    return _page(
        request,
        "unassigned.html",
        user,
        messages=await objects.load_unassigned(session),
        objects=await objects.load_objects(session),
    )


@router.post("/unassigned/{message_id}/assign", response_class=HTMLResponse)
async def unassigned_assign(
    request: Request,
    session: Session,
    user: Reviewer,
    message_id: int,
    object_id: Annotated[int, Form()],
) -> HTMLResponse:
    try:
        obj = await objects.assign_message(session, message_id, object_id, by=user.login)
    except objects.ObjectsError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()
    return _page(
        request, "partials/review_result.html", user, message=f"Отнесено: {obj.address}", ok=True
    )


# --- объект ---


@router.get("/objects/{object_id}", response_class=HTMLResponse)
async def object_detail(
    request: Request, session: Session, user: CurrentUser, object_id: int
) -> HTMLResponse:
    obj = await queries.load_object(session, object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Объект не найден")

    return _page(
        request,
        "object.html",
        user,
        object=obj,
        state=obj.state or {},
        pending=await queries.load_pending_facts(session, object_id=object_id, limit=50),
        feed=await queries.load_feed(session, object_id=object_id, limit=60),
    )


# --- лента событий ---


@router.get("/feed", response_class=HTMLResponse)
async def feed(
    request: Request,
    session: Session,
    user: CurrentUser,
    object_id: Annotated[int | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    needs_human: Annotated[bool, Query()] = False,
    facts_only: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    items = await queries.load_feed(
        session,
        object_id=object_id,
        event_type=event_type,
        only_needs_human=needs_human,
        only_facts=facts_only,
        limit=80,
    )
    return _page(
        request,
        "feed.html",
        user,
        items=items,
        objects=await queries.load_object_cards(session),
        event_types=await queries.load_event_types(session),
        filters={
            "object_id": object_id,
            "event_type": event_type,
            "needs_human": needs_human,
            "facts_only": facts_only,
        },
    )


@router.get("/feed/more", response_class=HTMLResponse)
async def feed_more(
    request: Request,
    session: Session,
    user: CurrentUser,
    before_id: Annotated[int, Query()],
    object_id: Annotated[int | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    needs_human: Annotated[bool, Query()] = False,
    facts_only: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    """Подгрузка следующей страницы ленты — фрагмент для HTMX."""
    items = await queries.load_feed(
        session,
        object_id=object_id,
        event_type=event_type,
        only_needs_human=needs_human,
        only_facts=facts_only,
        limit=80,
        before_id=before_id,
    )
    return _page(
        request,
        "partials/feed_rows.html",
        user,
        items=items,
        filters={
            "object_id": object_id,
            "event_type": event_type,
            "needs_human": needs_human,
            "facts_only": facts_only,
        },
    )


# --- очередь подтверждения ---


@router.get("/pending", response_class=HTMLResponse)
async def pending(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    return _page(
        request,
        "pending.html",
        user,
        pending=await queries.load_pending_facts(session, limit=200),
    )


@router.post("/pending/{event_id}/confirm", response_class=HTMLResponse)
async def pending_confirm(
    request: Request,
    session: Session,
    user: Reviewer,
    event_id: int,
    correction: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    return await _apply_decision(
        request, session, user, event_id, decision="confirm", note=correction
    )


@router.post("/pending/{event_id}/reject", response_class=HTMLResponse)
async def pending_reject(
    request: Request,
    session: Session,
    user: Reviewer,
    event_id: int,
    reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    return await _apply_decision(
        request, session, user, event_id, decision="reject", note=reason
    )


async def _apply_decision(
    request: Request,
    session: AsyncSession,
    user: User,
    event_id: int,
    *,
    decision: str,
    note: str | None,
) -> HTMLResponse:
    """Обработать решение и вернуть фрагмент на место строки очереди."""
    note = (note or "").strip() or None
    try:
        if decision == "confirm":
            result = await review.confirm_fact(
                session, event_id=event_id, user=user, correction=note
            )
        else:
            result = await review.reject_fact(
                session, event_id=event_id, user=user, reason=note
            )
    except review.ReviewError as exc:
        # Чаще всего — второй менеджер уже решил. Это не ошибка сервера,
        # но незавершённую транзакцию за собой оставлять нельзя.
        await session.rollback()
        return _page(
            request, "partials/review_result.html", user, message=str(exc), ok=False
        )

    await session.commit()
    return _page(
        request,
        "partials/review_result.html",
        user,
        message="Факт применён" if result.applied else "Факт отклонён",
        ok=True,
    )


# --- черновики исходящих ---


@router.get("/drafts", response_class=HTMLResponse)
async def drafts_page(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    halted = await _halt_reason(request)
    return _page(
        request,
        "drafts.html",
        user,
        drafts=await drafts.load_drafts(session),
        audit=await drafts.load_audit(session, limit=50),
        halted=halted,
        halt_is_failure=_halt_is_a_failure(halted),
    )


@router.post("/drafts/{outbound_id}/approve", response_class=HTMLResponse)
async def draft_approve(
    request: Request,
    session: Session,
    user: Reviewer,
    outbound_id: int,
    edited_text: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        decision = await drafts.approve(
            session, outbound_id, user=user, edited_text=edited_text
        )
    except drafts.DraftError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()

    # Отправку выполняет воркер: у веб-процесса нет клиента канала, и это
    # намеренно — интерфейс не должен уметь ходить наружу сам.
    arq = request.app.state.arq
    queued = False
    if arq is not None:
        await arq.enqueue_job("send_outbound", outbound_id)
        queued = True

    message = "Отправлено в очередь" if queued else (
        "Одобрено, но очередь недоступна — уйдёт после восстановления"
    )
    if decision.decision == "edited":
        message += " (с вашей правкой)"
    return _page(request, "partials/review_result.html", user, message=message, ok=True)


@router.post("/drafts/{outbound_id}/reject", response_class=HTMLResponse)
async def draft_reject(
    request: Request,
    session: Session,
    user: Reviewer,
    outbound_id: int,
    reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        await drafts.reject(session, outbound_id, user=user, reason=reason)
    except drafts.DraftError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()
    return _page(
        request,
        "partials/review_result.html",
        user,
        message="Отклонено — пойдёт в доработку промптов",
        ok=True,
    )


# --- аварийная остановка ---


async def _halt_reason(request: Request) -> str | None:
    switch = getattr(request.app.state, "kill_switch", None)
    return await switch.reason() if switch is not None else None


def _halt_is_a_failure(reason: str | None) -> bool:
    """Отличить нажатую кнопку «стоп» от недоступного хранилища.

    Предохранитель fail-closed: когда Redis не отвечает, состояние
    остановки проверить нельзя, и отправка запрещается. Это правильно, но
    показывать это теми же словами, что и сознательную остановку, — нет:
    менеджер жмёт «Возобновить», ничего не меняется, и он решает, что
    сломана панель. Возобновлять тут нечего, надо чинить Redis.
    """
    return reason == SWITCH_UNAVAILABLE


@router.post("/outbound/halt", response_class=HTMLResponse)
async def halt_outbound(
    request: Request,
    user: Reviewer,
    reason: Annotated[str, Form()] = "остановлено вручную",
) -> HTMLResponse:
    """Централизованная остановка всей исходящей активности (раздел 6)."""
    switch = getattr(request.app.state, "kill_switch", None)
    if switch is None:
        return _page(
            request,
            "partials/review_result.html",
            user,
            message="Остановка недоступна: переключатель не настроен",
            ok=False,
        )

    try:
        await switch.engage(f"{reason} ({user.login})")
    except Exception as exc:
        return _page(
            request,
            "partials/review_result.html",
            user,
            message=(
                f"Не удалось записать остановку: {exc}. Отправка при этом всё равно "
                "не идёт — недоступное хранилище считается сработавшим стопом."
            ),
            ok=False,
        )

    return _page(
        request,
        "partials/review_result.html",
        user,
        message="Исходящая активность остановлена",
        ok=True,
    )


@router.post("/outbound/resume", response_class=HTMLResponse)
async def resume_outbound(request: Request, user: Reviewer) -> HTMLResponse:
    switch = getattr(request.app.state, "kill_switch", None)
    if switch is None:
        return _page(
            request, "partials/review_result.html", user,
            message="Переключатель не настроен", ok=False,
        )
    try:
        await switch.release()
    except Exception as exc:
        return _page(
            request, "partials/review_result.html", user,
            message=f"Не удалось снять остановку: {exc}", ok=False,
        )
    return _page(
        request,
        "partials/review_result.html",
        user,
        message="Исходящая активность возобновлена",
        ok=True,
    )


# --- чаты ---


@router.get("/chats", response_class=HTMLResponse)
async def chats(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    cards = await queries.load_chats(session)
    return _page(
        request,
        "chats.html",
        user,
        chats=cards,
        problem_count=sum(1 for c in cards if not c.healthy),
    )


# --- справочник людей ---


@router.get("/people", response_class=HTMLResponse)
async def people_page(request: Request, session: Session, user: CurrentUser) -> HTMLResponse:
    cards = await people.load_identities(session)
    return _page(
        request,
        "people.html",
        user,
        identities=cards,
        roles=people.ROLES,
        role_titles=people.ROLE_TITLES,
        unassigned=sum(1 for c in cards if not c.is_linked and not c.is_bot),
    )


@router.post("/people/{identity_id}/assign", response_class=HTMLResponse)
async def people_assign(
    request: Request,
    session: Session,
    user: Reviewer,
    identity_id: int,
    role: Annotated[str, Form()],
    display_name: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        person = await people.assign(
            session, identity_id, role=role, display_name=display_name, by=user.login
        )
    except people.PeopleError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    title = people.ROLE_TITLES.get(person.role, person.role)
    message = f"{person.display_name} — {title}"
    await session.commit()

    return _page(request, "partials/review_result.html", user, message=message, ok=True)


@router.post("/people/{identity_id}/unlink", response_class=HTMLResponse)
async def people_unlink(
    request: Request, session: Session, user: Reviewer, identity_id: int
) -> HTMLResponse:
    try:
        await people.unlink(session, identity_id, by=user.login)
    except people.PeopleError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()
    return _page(request, "partials/review_result.html", user, message="Роль снята", ok=True)


# --- платёжный календарь ---


@router.get("/payments", response_class=HTMLResponse)
async def payments_page(
    request: Request, session: Session, user: CurrentUser
) -> HTMLResponse:
    items = await payment_calendar.load_calendar(session)
    return _page(
        request,
        "payments.html",
        user,
        items=items,
        periods=[(p.value, p.title) for p in PaymentPeriod],
        categories=[(c.value, c.title) for c in PaymentCategory],
        overdue=sum(1 for i in items if i.is_overdue),
        today=datetime.now(tz=UTC).date(),
    )


@router.post("/payments/new", response_class=HTMLResponse)
async def payment_create(
    request: Request,
    session: Session,
    user: Reviewer,
    title: Annotated[str, Form()],
    next_due_on: Annotated[str, Form()],
    period: Annotated[str, Form()] = "monthly",
    category: Annotated[str, Form()] = "other",
    amount: Annotated[str | None, Form()] = None,
    notify_days_before: Annotated[int, Form()] = 3,
    note: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        due = date.fromisoformat(next_due_on)
        payment = await payment_calendar.create(
            session,
            title=title,
            next_due_on=due,
            period=period,
            category=category,
            amount=Decimal(amount.replace(",", ".")) if amount and amount.strip() else None,
            notify_days_before=notify_days_before,
            note=note,
        )
    except (payment_calendar.PaymentError, ValueError, InvalidOperation) as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    message = f"{payment.title}: следующий платёж {payment.next_due_on:%d.%m.%Y}"
    await session.commit()
    return _page(request, "partials/review_result.html", user, message=message, ok=True)


@router.post("/payments/{payment_id}/paid", response_class=HTMLResponse)
async def payment_paid(
    request: Request, session: Session, user: Reviewer, payment_id: int
) -> HTMLResponse:
    try:
        payment = await payment_calendar.mark_paid(session, payment_id, by=user.login)
    except payment_calendar.PaymentError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    message = (
        f"Оплачено. Следующий — {payment.next_due_on:%d.%m.%Y}"
        if payment.active
        else "Оплачено, разовый платёж снят с календаря"
    )
    await session.commit()
    return _page(request, "partials/review_result.html", user, message=message, ok=True)


@router.post("/payments/{payment_id}/archive", response_class=HTMLResponse)
async def payment_archive(
    request: Request, session: Session, user: Reviewer, payment_id: int
) -> HTMLResponse:
    try:
        await payment_calendar.archive(session, payment_id, by=user.login)
    except payment_calendar.PaymentError as exc:
        await session.rollback()
        return _page(request, "partials/review_result.html", user, message=str(exc), ok=False)

    await session.commit()
    return _page(
        request, "partials/review_result.html", user, message="Снято с календаря", ok=True
    )


# --- здоровье и расход ---


@router.get("/health", response_class=HTMLResponse)
async def health(
    request: Request, session: Session, user: CurrentUser, settings: Config
) -> HTMLResponse:
    spend_24h = await queries.load_spend(session, hours=24)
    spend_30d = await queries.load_spend(session, hours=24 * 30)

    # Стоимость считается по ставке каждой модели отдельно. Прежний
    # расчёт брал одну ставку из настроек на всех и занижал расход на
    # Opus впятеро: страница показывала два доллара там, где провайдер
    # списал девять.
    now = datetime.now(tz=UTC)
    usd_24h = await pricing.spent_usd(session, since=now - timedelta(hours=24))
    usd_30d = await pricing.spent_usd(session, since=now - timedelta(days=30))
    by_model = await queries.load_spend_by_model(session, hours=24 * 30)

    return _page(
        request,
        "health.html",
        user,
        spend_24h=spend_24h,
        spend_30d=spend_30d,
        usd_24h=usd_24h,
        usd_30d=usd_30d,
        cost_24h=usd_24h * settings.usd_rub_rate,
        cost_30d=usd_30d * settings.usd_rub_rate,
        by_model=by_model,
        alert_usd=settings.spend_alert_usd,
        settings=settings,
    )

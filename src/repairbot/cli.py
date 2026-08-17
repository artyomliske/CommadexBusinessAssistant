"""Служебные команды эксплуатации.

Покрывают операции подключения объекта из раздела 2 ТЗ: регистрация
вебхука, проверка прав бота в чате, привязка чата к объекту, догрузка
истории, поиск чатов без объекта.

    python -m repairbot.cli <команда> [аргументы]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from repairbot.channels.max.adapter import MaxAdapter
from repairbot.config import get_settings
from repairbot.db.models import (
    AttachmentRecord,
    ChatRecord,
    Event,
    LlmCall,
    Message,
    ObjectAlias,
    RepairObject,
)
from repairbot.db.session import dispose_engine, init_engine, session_scope
from repairbot.ingest.history import backfill_chat, chats_pending_backfill
from repairbot.llm import pricing
from repairbot.memory.working import WorkingMemory
from repairbot.observability import setup_observability
from repairbot.web import objects


def _dump(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def cmd_bot_info() -> None:
    settings = get_settings()
    adapter = MaxAdapter(settings)
    try:
        _dump(await adapter.client.get_me())
    finally:
        await adapter.aclose()


async def cmd_subscribe() -> None:
    settings = get_settings()
    url = settings.public_base_url.rstrip("/") + settings.max_webhook_path
    if not url.startswith("https://"):
        raise SystemExit(
            "Вебхук MAX принимается только по HTTPS с сертификатом доверенного УЦ "
            f"(с 25.05.2026). Текущий PUBLIC_BASE_URL даёт: {url}"
        )
    adapter = MaxAdapter(settings)
    try:
        await adapter.ensure_subscription()
        _dump(await adapter.client.list_subscriptions())
    finally:
        await adapter.aclose()


async def cmd_subscriptions() -> None:
    adapter = MaxAdapter(get_settings())
    try:
        _dump(await adapter.client.list_subscriptions())
    finally:
        await adapter.aclose()


async def _check_one_chat(adapter: MaxAdapter, chat_id: str) -> dict[str, Any]:
    """Чек-лист одного чата: бот в нём и имеет право читать все сообщения."""
    report: dict[str, Any] = {"chat_id": chat_id}
    try:
        membership = await adapter.client.get_chat_membership(chat_id)
        permissions = membership.get("permissions") or []
        report["bot_is_member"] = True
        report["permissions"] = permissions
        report["read_all_messages"] = "read_all_messages" in permissions
        try:
            chat = await adapter.client.get_chat(chat_id)
            report["title"] = chat.get("title")
            report["chat_type"] = chat.get("type")
        except Exception as exc:
            report["chat_info_error"] = str(exc)
    except Exception as exc:
        report["bot_is_member"] = False
        report["error"] = str(exc)

    report["ready"] = bool(report.get("bot_is_member")) and bool(report.get("read_all_messages"))
    return report


async def cmd_chat_titles() -> None:
    """Дозапросить названия чатов, у которых их нет.

    В обычном сообщении платформа название не присылает, поэтому чат,
    впервые замеченный по сообщению, остаётся безымянным. В работе это
    делает воркер сам; команда нужна, чтобы не ждать.
    """
    from repairbot.ingest.chat_titles import fill_missing_titles

    settings = get_settings()
    init_engine(settings)
    adapter = MaxAdapter(settings)
    try:
        async with session_scope() as session:
            filled = await fill_missing_titles(session, adapter.client)
            rows = await session.execute(
                select(ChatRecord.channel_chat_id, ChatRecord.title)
                .where(ChatRecord.channel == "max")
                .order_by(ChatRecord.id)
            )
            _dump(
                {
                    "заполнено": filled,
                    "чаты": [
                        {"chat_id": cid, "название": title or "—"} for cid, title in rows.all()
                    ],
                }
            )
    finally:
        await adapter.aclose()
        await dispose_engine()


async def cmd_check_chat(chat_ids: list[str]) -> None:
    """Чек-лист подключения по всем чатам сразу.

    Без аргументов берёт все чаты MAX, известные базе. Право
    `read_all_messages` выдаётся руками в каждой группе отдельно, и
    проверять их по одному — верный способ пропустить одну и потом
    гадать, почему из неё ничего не приходит.
    """
    settings = get_settings()
    init_engine(settings)
    try:
        if not chat_ids:
            async with session_scope() as session:
                rows = await session.execute(
                    select(ChatRecord.channel_chat_id)
                    .where(ChatRecord.channel == "max")
                    .order_by(ChatRecord.channel_chat_id)
                )
                chat_ids = list(rows.scalars().all())
            if not chat_ids:
                raise SystemExit(
                    "База не знает ни одного чата MAX. Укажите идентификаторы "
                    "явно или получите их командой listen."
                )

        adapter = MaxAdapter(settings)
        try:
            reports = [await _check_one_chat(adapter, chat_id) for chat_id in chat_ids]
        finally:
            await adapter.aclose()

        async with session_scope() as session:
            for report in reports:
                await session.execute(
                    sql_update(ChatRecord)
                    .where(
                        ChatRecord.channel == "max",
                        ChatRecord.channel_chat_id == report["chat_id"],
                    )
                    .values(
                        can_read_all_messages=report.get("read_all_messages"),
                        # Название платформа в сообщениях не присылает, а
                        # здесь оно уже получено — не сохранить его значит
                        # оставить в панели прочерк вместо имени чата.
                        **({"title": report["title"]} if report.get("title") else {}),
                    )
                )
    finally:
        await dispose_engine()

    _dump(reports[0] if len(reports) == 1 else reports)
    if len(reports) > 1:
        _print_chat_summary(reports)


def _print_chat_summary(reports: list[dict[str, Any]]) -> None:
    """Итог человеку: что осталось сделать руками, а не что вернул API."""
    print()
    for report in reports:
        mark = "готов" if report["ready"] else "НЕ ЧИТАЕТ"
        title = report.get("title") or report.get("error", "")
        print(f"  {mark:<10} {report['chat_id']:<18} {title}")

    blocked = [r for r in reports if not r["ready"]]
    if not blocked:
        print("\nВсе чаты подключены.")
        return
    print(
        f"\nБез права read_all_messages: {len(blocked)}. Бот видит в таком чате "
        "только сообщения,\nадресованные ему, — переписка бригады до системы "
        "не доходит.\nВ MAX: чат → участники → бот → сделать администратором."
    )


async def cmd_create_object(code: str, address: str) -> None:
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            obj = await objects.create_object(session, address=address, code=code)
            _dump({"id": obj.id, "code": obj.code, "address": obj.address})
    finally:
        await dispose_engine()


async def cmd_object_alias(object_code: str, alias: str | None) -> None:
    """Как объект называют в переписке. Без alias — показать заведённые."""
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            obj = (
                await session.execute(select(RepairObject).where(RepairObject.code == object_code))
            ).scalar_one_or_none()
            if obj is None:
                raise SystemExit(f"Объект не найден: {object_code}")

            if alias:
                await objects.add_alias(session, obj.id, alias)

            rows = await session.execute(
                select(ObjectAlias).where(ObjectAlias.object_id == obj.id).order_by(ObjectAlias.id)
            )
            _dump(
                {
                    "object": obj.code,
                    "address": obj.address,
                    "aliases": [a.alias for a in rows.scalars().all()],
                }
            )
    finally:
        await dispose_engine()


async def cmd_unassigned(limit: int) -> None:
    """Сообщения, для которых объект не узнан.

    В функциональных чатах это нормальная часть работы, а не поломка:
    сообщение без адреса и без недавнего разговора разбирает человек.
    Команда показывает очередь такого разбора.
    """
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            rows = await session.execute(
                select(Message, ChatRecord.title)
                .join(ChatRecord, Message.chat_id == ChatRecord.id, isouter=True)
                .where(
                    Message.object_id.is_(None),
                    Message.is_outbound.is_(False),
                    Message.text.is_not(None),
                )
                .order_by(Message.sent_at.desc())
                .limit(limit)
            )
            _dump(
                [
                    {
                        "id": message.id,
                        "чат": title,
                        "когда": message.sent_at,
                        "текст": (message.text or "")[:160],
                    }
                    for message, title in rows.all()
                ]
            )
    finally:
        await dispose_engine()


async def cmd_assign(message_id: int, object_code: str) -> None:
    """Отнести сообщение к объекту руками. То же делает страница «Разбор»."""
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            obj = (
                await session.execute(select(RepairObject).where(RepairObject.code == object_code))
            ).scalar_one_or_none()
            if obj is None:
                raise SystemExit(f"Объект не найден: {object_code}")

            await objects.assign_message(session, message_id, obj.id)
            _dump({"message_id": message_id, "object": obj.code})
    finally:
        await dispose_engine()


async def cmd_link_chat(chat_id: str, object_code: str) -> None:
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            obj = (
                await session.execute(select(RepairObject).where(RepairObject.code == object_code))
            ).scalar_one_or_none()
            if obj is None:
                raise SystemExit(f"Объект не найден: {object_code}")
            result = await session.execute(
                sql_update(ChatRecord)
                .where(ChatRecord.channel == "max", ChatRecord.channel_chat_id == chat_id)
                .values(object_id=obj.id)
                .returning(ChatRecord.id)
            )
            if result.scalar_one_or_none() is None:
                raise SystemExit(
                    f"Чат {chat_id} не найден в реестре. Добавьте бота в чат: "
                    "запись появится по событию bot_added."
                )

            # Историю чата обычно загружают до привязки к объекту, поэтому
            # уже принятые сообщения и события связываем с объектом здесь.
            messages = await session.execute(
                sql_update(Message)
                .where(
                    Message.channel == "max",
                    Message.channel_chat_id == chat_id,
                    Message.object_id.is_(None),
                )
                .values(object_id=obj.id)
            )
            events = await session.execute(
                sql_update(Event)
                .where(
                    Event.channel == "max",
                    Event.channel_chat_id == chat_id,
                    Event.object_id.is_(None),
                )
                .values(object_id=obj.id)
            )
            _dump(
                {
                    "chat_id": chat_id,
                    "object": object_code,
                    "linked": True,
                    "messages_relinked": messages.rowcount,
                    "events_relinked": events.rowcount,
                }
            )
    finally:
        await dispose_engine()


def _ask_password() -> str:
    """Спросить новый пароль дважды и вернуть его хеш.

    Пароль спрашивается интерактивно и не берётся из аргументов: иначе он
    остался бы в истории командной оболочки.
    """
    import getpass

    from repairbot.web.security import MIN_PASSWORD_LENGTH, hash_password

    password = getpass.getpass(f"Пароль (минимум {MIN_PASSWORD_LENGTH} символов): ")
    if password != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают")
    try:
        return hash_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


async def cmd_set_password(login: str) -> None:
    """Задать пароль существующему пользователю.

    Нужна, когда пароль забыт: сменить свой можно в панели, но туда ещё
    надо войти. Права администратора здесь ни при чём — доступ к серверу
    и так означает полный доступ к системе.
    """
    from repairbot.db.models import User

    password_hash = _ask_password()
    normalized = login.strip().casefold()

    init_engine(get_settings())
    try:
        async with session_scope() as session:
            result = await session.execute(
                sql_update(User)
                .where(User.login == normalized)
                .values(password_hash=password_hash)
                .returning(User.id, User.login, User.role, User.is_active)
            )
            row = result.first()
            if row is None:
                raise SystemExit(f"Пользователь не найден: {normalized}")
            _dump(
                {
                    "login": row.login,
                    "role": row.role,
                    "активен": row.is_active,
                    "пароль": "задан",
                }
            )
    finally:
        await dispose_engine()


async def cmd_users() -> None:
    """Кто заведён в панели. Паролей не показывает — их и нет в открытом виде."""
    from repairbot.db.models import User

    init_engine(get_settings())
    try:
        async with session_scope() as session:
            rows = await session.execute(select(User).order_by(User.id))
            _dump(
                [
                    {
                        "логин": u.login,
                        "имя": u.display_name,
                        "роль": u.role,
                        "активен": u.is_active,
                        "последний вход": u.last_login_at,
                    }
                    for u in rows.scalars().all()
                ]
            )
    finally:
        await dispose_engine()


async def cmd_create_user(login: str, display_name: str, role: str) -> None:
    """Создать пользователя веб-интерфейса."""
    from repairbot.db.models import User

    if role not in ("admin", "manager", "viewer"):
        raise SystemExit("Роль должна быть admin, manager или viewer")

    password_hash = _ask_password()

    normalized = login.strip().casefold()
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            existing = (
                await session.execute(select(User).where(User.login == normalized))
            ).scalar_one_or_none()
            if existing is not None:
                raise SystemExit(
                    f"Пользователь {normalized} уже существует. "
                    f"Забыт пароль — задайте новый: set-password {normalized}"
                )

            user = User(
                login=normalized,
                display_name=display_name,
                password_hash=password_hash,
                role=role,
            )
            session.add(user)
            await session.flush()
            _dump({"id": user.id, "login": user.login, "role": user.role})
    finally:
        await dispose_engine()


def cmd_google_authorize() -> None:
    """Разовая авторизация в аккаунте заказчика.

    Запускается **на его машине или при нём**: откроется браузер, и войти
    надо в тот аккаунт, которому должны принадлежать таблицы. Готовый файл
    токена кладётся на сервер в GOOGLE_OAUTH_TOKEN_PATH.
    """
    from repairbot.integrations import google_auth

    settings = get_settings()
    if not settings.google_oauth_client_secrets_path:
        raise SystemExit(
            "Не задан GOOGLE_OAUTH_CLIENT_SECRETS_PATH — файл client_secret из "
            "Google Cloud Console (APIs & Services → Credentials → OAuth client ID, "
            "тип Desktop app)."
        )
    token_path = settings.google_oauth_token_path or "secrets/google_token.json"

    try:
        saved = google_auth.authorize(settings.google_oauth_client_secrets_path, token_path)
    except google_auth.GoogleAuthError as exc:
        raise SystemExit(str(exc)) from exc

    _dump(
        {
            "token_path": saved,
            "next": [
                f"GOOGLE_OAUTH_TOKEN_PATH={saved} в .env на сервере",
                "repairbot check-drive — убедиться, что аккаунт тот самый",
                "перевести приложение в Google Cloud в статус Production, иначе "
                "Google отзовёт доступ через 7 дней",
            ],
        }
    )


async def cmd_google_folder(name: str) -> None:
    """Создать на Диске папку под архив и напечатать её идентификатор.

    Папку нельзя просто сделать руками в браузере и вписать её номер.
    Мы просим область доступа `drive.file` — она даёт доступ только к
    тому, что приложение создало само. Чужая папка для него не
    существует, и проверка вернула бы «не найдено» на папку, которая у
    заказчика прекрасно видна. Поэтому создаём её отсюда.

    Повторный запуск ничего не испортит: папка ищется по имени и
    создаётся, только если её нет.
    """
    from repairbot.integrations import google_auth
    from repairbot.integrations.drive import DriveClient

    settings = get_settings()
    source = google_auth.from_settings(settings)
    if not source.configured:
        raise SystemExit("Доступ к Google не настроен — сначала google-authorize")

    client = DriveClient(source)
    try:
        # «root» — псевдоним корня Моего диска: папку верхнего уровня
        # класть больше некуда.
        folder_id = await client.ensure_folder(name, "root")
    finally:
        await client.aclose()

    _dump(
        {
            "folder_id": folder_id,
            "name": name,
            "next": [
                f"GOOGLE_DRIVE_FOLDER_ID={folder_id} в .env на сервере",
                "repairbot check-drive — проверить, что папка доступна",
            ],
        }
    )


async def cmd_check_drive() -> None:
    """Проверить доступ к Диску заказчика.

    Чек-лист подключения: понятный отказ при настройке лучше, чем сбой на
    первой синхронизации. Проверяется не только доступность папки, но и
    то, чей это аккаунт и кому будут принадлежать созданные файлы, — токен
    легко получить не от того ящика и заметить это через месяц.
    """
    from repairbot.integrations import google_auth
    from repairbot.integrations.sheets import SheetsClient

    settings = get_settings()
    source = google_auth.from_settings(settings)
    if not source.configured:
        raise SystemExit(
            "Доступ к Google не настроен. Личный аккаунт: repairbot google-authorize, "
            "затем GOOGLE_OAUTH_TOKEN_PATH. Workspace: GOOGLE_CREDENTIALS_PATH."
        )
    if not settings.google_drive_folder_id:
        raise SystemExit(
            "Не задан GOOGLE_DRIVE_FOLDER_ID — папка на Диске заказчика, где будут "
            "лежать книги объектов."
        )

    client = SheetsClient(source)
    report: dict[str, Any] = {
        "auth": source.kind,
        "impersonates": source.impersonate_subject or None,
        "folder_id": settings.google_drive_folder_id,
        "required_scopes": list(google_auth.SCOPES),
    }
    try:
        # Чей аккаунт и сколько места. Не критично: если область доступа
        # запрос не пропустит, проверка папки всё равно важнее.
        try:
            about = await client.about()
            report["account"] = (about.get("user") or {}).get("emailAddress")
            quota = about.get("storageQuota") or {}
            if quota.get("limit"):
                gb = 1024**3
                report["storage_gb"] = {
                    "limit": round(int(quota["limit"]) / gb, 1),
                    "used": round(int(quota.get("usage", 0)) / gb, 1),
                }
        except Exception as exc:
            report["account_check_failed"] = str(exc)

        folder = await client.check_folder(settings.google_drive_folder_id)
        report["name"] = folder.get("name")
        report["is_folder"] = folder.get("mimeType") == "application/vnd.google-apps.folder"
        # driveId есть только у файлов на общем диске.
        report["on_shared_drive"] = bool(folder.get("driveId"))
        report["owners"] = [o.get("emailAddress") for o in folder.get("owners") or []]
        report["can_add_children"] = (folder.get("capabilities") or {}).get("canAddChildren")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        await client.aclose()

    # Владелец создаваемых файлов — тот, от чьего имени идёт вызов.
    ownership_ok = source.owns_created_files or bool(report.get("on_shared_drive"))
    report["ready"] = bool(
        report.get("is_folder") and report.get("can_add_children") and ownership_ok
    )

    if "error" not in report and not report["ready"]:
        if not report.get("is_folder"):
            report["hint"] = "Указанный идентификатор — не папка."
        elif not report.get("can_add_children"):
            report["hint"] = "Нет права создавать файлы в папке."
        else:
            report["hint"] = (
                "Сервисный аккаунт без делегирования не может создать файл: своей "
                "квоты хранилища у него нет. Для личного аккаунта пройдите "
                "repairbot google-authorize; для Workspace задайте "
                "GOOGLE_IMPERSONATE_SUBJECT и разрешите делегирование для "
                "перечисленных областей доступа."
            )
    _dump(report)


async def cmd_archive_files(limit: int) -> None:
    """Переложить вложения на Диск заказчика вручную.

    В работе это делает воркер по расписанию. Команда нужна при настройке
    и после долгого простоя, когда ждать очередного захода незачем.
    """
    from repairbot.channels.max.adapter import MaxAdapter
    from repairbot.integrations import google_auth
    from repairbot.integrations.drive import DriveClient, FileArchive

    settings = get_settings()
    source = google_auth.from_settings(settings)
    if not source.configured:
        raise SystemExit("Доступ к Google не настроен — см. repairbot check-drive")
    if not settings.google_drive_folder_id:
        raise SystemExit("Не задан GOOGLE_DRIVE_FOLDER_ID — некуда складывать архив")

    init_engine(settings)
    drive = DriveClient(source)
    adapter = MaxAdapter(settings)
    archive = FileArchive(
        drive,
        adapter.client,
        root_folder_id=settings.google_drive_folder_id,
        max_bytes=settings.archive_max_file_bytes,
    )
    try:
        async with session_scope() as session:
            result = await archive.archive_pending(session, limit=limit)
            refiled = await archive.refile_pending(session, limit=limit)
        _dump({**result.as_dict(), "refiled": refiled})
    finally:
        await drive.aclose()
        await adapter.aclose()
        await dispose_engine()


async def cmd_spend() -> None:
    """Сколько потрачено на модель: за сутки, за неделю, всего.

    Считается по ставкам каждой модели отдельно. Раньше всё считалось по
    одной ставке из настроек, и расход на Opus занижался впятеро.
    """
    from datetime import UTC, datetime, timedelta

    settings = get_settings()
    init_engine(settings)
    try:
        async with session_scope() as session:
            now = datetime.now(tz=UTC)
            day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
            report = {
                "за сутки": f"${await pricing.spent_usd(session, since=day_ago):.2f}",
                "за неделю": f"${await pricing.spent_usd(session, since=week_ago):.2f}",
                "всего": f"${await pricing.spent_usd(session):.2f}",
                "порог предупреждения": f"${settings.spend_alert_usd:.2f} в сутки",
            }
            rows = await session.execute(
                select(
                    LlmCall.purpose,
                    LlmCall.model,
                    func.count(LlmCall.id),
                    func.sum(LlmCall.input_tokens),
                    func.sum(LlmCall.cached_input_tokens),
                    func.sum(LlmCall.output_tokens),
                ).group_by(LlmCall.purpose, LlmCall.model)
            )
            report["по видам"] = [
                {
                    "что": purpose,
                    "модель": pricing.price_for(model).title,
                    "вызовов": int(count),
                    "стоимость": "${:.2f}".format(
                        pricing.cost_usd(
                            model,
                            input_tokens=int(inp or 0),
                            cached_tokens=int(cached or 0),
                            output_tokens=int(out or 0),
                        )
                    ),
                }
                for purpose, model, count, inp, cached, out in rows.all()
            ]
            _dump(report)
    finally:
        await dispose_engine()


def _confirm_spend(estimate: Any, settings: Any, *, assume_yes: bool) -> None:
    """Спросить согласие, если операция дороже порога.

    Узнавать о трате по счёту провайдера поздно. Порог настраивается;
    дешёвые операции не спрашивают ничего и работой не мешают.
    """
    print(f"Оценка расхода: {estimate.render()}, это примерно {estimate.rub:.0f} ₽")
    if assume_yes or estimate.total_usd < settings.spend_confirm_usd:
        return

    if not sys.stdin.isatty():
        # Команду часто запускают без терминала — через `docker compose
        # exec -T` или из скрипта. Спросить там не у кого, а падать на
        # чтении пустого ввода значит превратить осторожность в поломку.
        raise SystemExit(
            f"Операция дороже ${settings.spend_confirm_usd:.2f}, а спросить не у кого: "
            "нет терминала. Добавьте --yes, если согласны на эту трату."
        )

    answer = input("Запускать? (да / нет): ").strip().lower().replace("ё", "е")
    if answer[:2] not in ("да", "ок") and answer[:1] not in ("y", "+"):
        raise SystemExit("Отменено, ничего не потрачено.")


async def cmd_recognize_documents(limit: int, assume_yes: bool = False) -> None:
    """Разобрать сохранённые на Диске документы вручную.

    В работе это делает воркер по расписанию. Команда нужна после
    доработки промптов: сбросьте `doc_class` у нужных вложений, и они
    будут перечитаны заново.
    """
    from repairbot.agents.document_pass import DocumentPass
    from repairbot.agents.documents import DocumentAgent
    from repairbot.integrations import google_auth
    from repairbot.integrations.drive import DriveClient
    from repairbot.llm import build_router

    settings = get_settings()
    source = google_auth.from_settings(settings)

    init_engine(settings)
    # Диск необязателен: без него документ читается прямо из мессенджера.
    # Требовать Google значило бы не разбирать ни одного счёта, пока он
    # не подключён, — а счета приходят уже сейчас.
    drive = DriveClient(source) if source.configured else None
    adapter = MaxAdapter(settings)
    router = build_router(settings)
    document_pass = DocumentPass(
        DocumentAgent(
            router,
            max_bytes=settings.recognize_max_file_bytes,
            model=settings.recognize_model or None,
        ),
        drive,
        max_bytes=settings.recognize_max_file_bytes,
        batch_size=settings.recognize_batch_size,
        channel=adapter.client,
        fast_model=settings.recognize_fast_model or None,
    )
    try:
        async with session_scope() as session:
            waiting = (
                await session.execute(
                    select(func.count(AttachmentRecord.id)).where(
                        AttachmentRecord.doc_class.is_(None),
                        AttachmentRecord.source_url.isnot(None),
                    )
                )
            ).scalar_one()
            estimate = await pricing.estimate_for(session, "document", min(limit, int(waiting)))

        _confirm_spend(estimate, settings, assume_yes=assume_yes)

        async with session_scope() as session:
            result = await document_pass.run(session, limit=limit)
            spent = await pricing.spent_usd(session, purpose="document")
        _dump(
            {
                **result.as_dict(),
                "источник": "Диск" if drive else "мессенджер",
                "потрачено на документы всего": f"${spent:.2f}",
            }
        )
    finally:
        await router.aclose()
        if drive is not None:
            await drive.aclose()
        await adapter.aclose()
        await dispose_engine()


async def cmd_digest(period: str, dry_run: bool) -> None:
    """Собрать сводку руководителю.

    По умолчанию только показывает её: увидеть, что уйдёт, до того как
    оно уйдёт, полезнее, чем разбираться потом по журналу аудита.
    """
    from repairbot.agents.report_delivery import ReportDelivery
    from repairbot.agents.reports import Period, build_digest, render_message
    from repairbot.outbound.controller import Controller, KillSwitch

    settings = get_settings()
    init_engine(settings)
    pricing = {
        "price_input_usd": settings.llm_price_input_usd,
        "price_cached_usd": settings.llm_price_cached_input_usd,
        "price_output_usd": settings.llm_price_output_usd,
        "usd_rub": settings.usd_rub_rate,
    }

    redis = None
    try:
        async with session_scope() as session:
            if dry_run:
                digest = await build_digest(session, Period(period), **pricing)
                print(render_message(digest))
                return

            if not settings.manager_chat_id:
                raise SystemExit("Не задан MANAGER_CHAT_ID — некуда отправлять сводку")

            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            delivery = ReportDelivery(
                rollup_spreadsheet_id=settings.sheets_rollup_spreadsheet_id,
                manager_chat_id=settings.manager_chat_id,
                pricing=pricing,
            )
            result = await delivery.deliver(
                session,
                Period(period),
                controller=Controller(session, kill_switch=KillSwitch(redis)),
            )

        # Вне session_scope: задача отправки не должна начаться раньше,
        # чем запись контролёра доедет до базы.
        queued = await _queue_send(settings, result.outbound_id)
        _dump({**result.as_dict(), "queued": queued})
    finally:
        if redis is not None:
            await redis.aclose()
        await dispose_engine()


async def cmd_send_pending(limit: int) -> None:
    """Дослать одобренное, что осталось лежать неотправленным.

    Такое остаётся после простоя воркера — и оставалось после команд
    `--send`, которые одобряли сообщение, но не ставили задачу отправки.
    Повторной отправки не будет: отправитель пропускает всё, у чего уже
    проставлено `sent_at`.
    """
    from repairbot.db.models import OutboundMessage

    settings = get_settings()
    init_engine(settings)
    try:
        async with session_scope() as session:
            rows = await session.execute(
                select(
                    OutboundMessage.id,
                    OutboundMessage.channel_chat_id,
                    OutboundMessage.created_at,
                )
                .where(
                    OutboundMessage.verdict == "allow",
                    OutboundMessage.sent_at.is_(None),
                )
                .order_by(OutboundMessage.created_at)
                .limit(limit)
            )
            pending = rows.all()

        queued = []
        for row in pending:
            if await _queue_send(settings, row.id):
                queued.append({"id": row.id, "чат": row.channel_chat_id, "создано": row.created_at})
        _dump({"поставлено в очередь": len(queued), "сообщения": queued})
    finally:
        await dispose_engine()


async def _queue_send(settings: Any, outbound_id: int | None) -> bool:
    """Поставить одобренное сообщение в очередь воркера.

    Отправляет наружу только воркер, поэтому без этой постановки
    команда `--send` одобряла сообщение и оставляла его лежать в базе —
    с вердиктом «allow» в выводе и без единой ошибки.

    Пул отдельный от того, что держит рубильник: arq хранит задачи
    в неразобранном виде, а рубильнику нужны строки.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    from repairbot.outbound.queue import queue_send

    if outbound_id is None:
        return False
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        return await queue_send(pool, outbound_id)
    finally:
        await pool.aclose()


async def cmd_people(role: str | None, identity_id: int | None, name: str | None) -> None:
    """Показать учётные записи или назначить роль.

    Без аргументов — список. С `--identity` и `--role` — назначение.
    То же самое делается в панели на странице «Люди»; команда нужна для
    первоначального заполнения, когда записей десятки.
    """
    from repairbot.web import people

    init_engine(get_settings())
    try:
        async with session_scope() as session:
            if identity_id is None:
                cards = await people.load_identities(session)
                _dump(
                    [
                        {
                            "id": c.id,
                            "кто": c.person_name or c.display_name or c.username,
                            "канал": c.channel,
                            # Именно этот идентификатор идёт в список
                            # помощника (ASSISTANT_USER_IDS). Искать его
                            # больше негде, а `listen` печатает чат, что
                            # для личного диалога не одно и то же.
                            "идентификатор": c.channel_user_id,
                            "роль": people.ROLE_TITLES[c.role] if c.is_linked else None,
                            "сообщений": c.messages,
                            "где": ", ".join(c.objects) or ("личный диалог" if c.dialogs else ""),
                            "подсказка": c.hint,
                        }
                        for c in cards
                    ]
                )
                return

            if not role:
                raise SystemExit("Нужна роль: --role " + "|".join(people.ROLES))
            try:
                person = await people.assign(
                    session, identity_id, role=role, display_name=name
                )
            except people.PeopleError as exc:
                raise SystemExit(str(exc)) from exc
            _dump(
                {
                    "identity_id": identity_id,
                    "person_id": person.id,
                    "имя": person.display_name,
                    "роль": person.role,
                    "псевдоним": person.pseudonym,
                }
            )
    finally:
        await dispose_engine()


async def cmd_payments(send: bool) -> None:
    """Показать платёжный календарь или разослать напоминание.

    По умолчанию показывает: увидеть, что уйдёт, до отправки полезнее,
    чем разбираться потом по журналу аудита.
    """
    from repairbot.agents import payment_calendar as pc
    from repairbot.outbound.controller import Controller, KillSwitch

    settings = get_settings()
    init_engine(settings)
    redis = None
    try:
        async with session_scope() as session:
            if not send:
                items = await pc.load_calendar(session)
                _dump(
                    [
                        {
                            "id": i.payment.id,
                            "платёж": i.payment.title,
                            "сумма": str(i.payment.amount) if i.payment.amount else "по счёту",
                            "срок": i.payment.next_due_on,
                            "осталось дней": i.days_left,
                            "просрочен": i.is_overdue,
                        }
                        for i in items
                    ]
                )
                return

            if not settings.manager_chat_id:
                raise SystemExit("Не задан MANAGER_CHAT_ID — некуда отправлять напоминание")

            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            result = await pc.send_reminder(
                session,
                controller=Controller(session, kill_switch=KillSwitch(redis)),
                channel="max",
                chat_id=settings.manager_chat_id,
            )

        # Вне session_scope — см. _queue_send.
        queued = await _queue_send(settings, result.outbound_id)
        _dump({**result.as_dict(), "queued": queued})
    finally:
        if redis is not None:
            await redis.aclose()
        await dispose_engine()


def _explain_max_error(exc: Exception) -> str:
    """Перевести отказ платформы в понятное указание, что делать.

    Команды настройки запускает тот, кто уже застрял; трассировка Python
    ему не помогает, а «сертификат не проверен» без объяснения выглядит
    как поломка нашего кода, хотя дело в отсутствующем корневом сертификате.
    """
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "SSLError" in exc.__class__.__name__:
        return (
            "Не удалось проверить сертификат platform-api2.max.ru.\n"
            "Нужен корневой сертификат Минцифры. Положите его файлом с "
            "расширением .crt в каталог certs/ — он подхватится сам,\n"
            "системные центры при этом сохранятся (см. certs/README.md).\n"
            "  https://www.gosuslugi.ru/crt — «Сертификат для ПК»"
        )
    if "401" in text or "403" in text:
        return f"Платформа отклонила токен: {text}\nПроверьте MAX_ACCESS_TOKEN в .env"
    if "404" in text:
        return (
            f"Метод не найден: {text}\n"
            "Возможно, опрос апдейтов в этой версии API называется иначе — "
            "тогда идентификатор чата придётся взять из вебхука."
        )
    return f"Не удалось обратиться к MAX: {text}"


async def cmd_listen(seconds: int) -> None:
    """Показать, кто и откуда пишет боту. Нужна при настройке.

    Опрашивает платформу напрямую, без вебхука: вебхук требует публичного
    адреса с сертификатом доверенного УЦ, а идентификатор чата нужен
    раньше, чем такой адрес появится.

    Ничего не записывает в базу — только печатает. Напишите боту в MAX
    и посмотрите, какой chat_id он увидел.
    """
    settings = get_settings()
    adapter = MaxAdapter(settings)
    deadline = asyncio.get_running_loop().time() + seconds
    marker: int | None = None
    seen: dict[str, dict[str, Any]] = {}

    if not settings.max_access_token.get_secret_value():
        raise SystemExit(
            "Не задан MAX_ACCESS_TOKEN. Создайте бота в @masterbot (MAX), "
            "скопируйте токен и впишите его в .env"
        )

    print(f"Слушаю {seconds} с. Напишите боту в MAX — сюда, в чат или в личку.")
    print("«чат» идёт в MANAGER_CHAT_ID, «автор» — в ASSISTANT_USER_IDS.")
    print("В личном диалоге это разные числа, и перепутать их легко.")
    try:
        while asyncio.get_running_loop().time() < deadline:
            events, marker = await adapter.client.get_updates(marker=marker, wait_seconds=10)
            for event in events:
                chat_id = event.chat.channel_chat_id
                entry = seen.setdefault(
                    chat_id,
                    {"chat_id": chat_id, "вид": event.chat.kind.value, "сообщений": 0},
                )
                entry["сообщений"] += 1
                if event.chat.title:
                    entry["название"] = event.chat.title
                if event.actor and event.actor.display_name:
                    entry["последний автор"] = event.actor.display_name
                if event.actor:
                    # Идентификатор автора — не то же, что идентификатор
                    # чата, даже в личном диалоге. В список помощника
                    # (ASSISTANT_USER_IDS) идёт именно он.
                    entry["идентификатор автора"] = event.actor.channel_user_id
                if event.text:
                    entry["последнее"] = event.text[:80]
                who = event.actor.display_name if event.actor else "—"
                user_id = event.actor.channel_user_id if event.actor else "—"
                print(
                    f"  чат {chat_id:<18} {event.chat.kind.value:6} "
                    f"автор {user_id:<12} {who[:18]:18} "
                    f"{(event.text or event.event_type.value)[:50]}"
                )
    except KeyboardInterrupt:  # pragma: no cover — ручная остановка
        pass
    except Exception as exc:
        raise SystemExit(_explain_max_error(exc)) from exc
    finally:
        await adapter.aclose()

    if not seen:
        print(
            "\nНичего не пришло. Проверьте: бот создан в @masterbot, MAX_ACCESS_TOKEN "
            "задан, и вы действительно написали именно этому боту."
        )
        return

    print("\nНайденные чаты:")
    _dump(list(seen.values()))
    print(
        "Для сводок и напоминаний возьмите нужный chat_id и впишите его "
        "в MANAGER_CHAT_ID в .env"
    )


async def cmd_telegram(action: str, chat_id: str | None) -> None:
    """Подключение канала Telegram (этап 6).

    `info` — карточка бота и состояние вебхука, `subscribe` — регистрация,
    `check` — увидит ли бот переписку в указанном чате.
    """
    from repairbot.channels.telegram import TelegramAdapter, TelegramApiError

    settings = get_settings()
    if not settings.telegram_enabled:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN. Создайте бота в @BotFather и впишите токен в .env"
        )

    adapter = TelegramAdapter(settings)
    try:
        match action:
            case "info":
                _dump(
                    {
                        "бот": await adapter.client.get_me(),
                        "вебхук": await adapter.client.get_webhook_info(),
                    }
                )
            case "subscribe":
                url = settings.public_base_url.rstrip("/") + settings.telegram_webhook_path
                if not url.startswith("https://"):
                    raise SystemExit(
                        f"Telegram принимает вебхук только по HTTPS. Сейчас: {url}"
                    )
                await adapter.ensure_subscription()
                _dump(await adapter.client.get_webhook_info())
            case "check":
                if not chat_id:
                    raise SystemExit("Нужен --chat-id")
                membership = await adapter.client.get_chat_membership(chat_id)
                report = {
                    "chat_id": chat_id,
                    "статус бота": membership["status"],
                    "видит всю переписку": membership["reads_all_messages"],
                }
                if not membership["reads_all_messages"]:
                    report["что делать"] = (
                        "Сделайте бота администратором чата либо отключите режим "
                        "приватности в @BotFather (/setprivacy → Disable). Иначе он "
                        "видит только команды и ответы себе, и половина переписки "
                        "бригады до системы не дойдёт."
                    )
                _dump(report)
            case _:
                raise SystemExit(f"Неизвестное действие: {action}")
    except TelegramApiError as exc:
        raise SystemExit(f"Telegram отказал: {exc}") from exc
    finally:
        await adapter.aclose()


async def cmd_orphan_chats() -> None:
    """Чаты без объекта — еженедельный контроль из раздела 10 ТЗ."""
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            rows = await session.execute(
                select(
                    ChatRecord.channel_chat_id,
                    ChatRecord.title,
                    ChatRecord.last_event_at,
                ).where(ChatRecord.object_id.is_(None))
            )
            _dump([dict(r._mapping) for r in rows])
    finally:
        await dispose_engine()


async def cmd_backfill(chat_id: str | None, limit: int) -> None:
    settings = get_settings()
    init_engine(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    adapter = MaxAdapter(settings)
    working_memory = WorkingMemory(redis, settings)
    try:
        targets = [chat_id] if chat_id else await chats_pending_backfill("max")
        if not targets:
            _dump({"chats": 0, "note": "нет чатов, ожидающих загрузки истории"})
            return
        results = {
            target: await backfill_chat(
                adapter, target, max_messages=limit, working_memory=working_memory
            )
            for target in targets
        }
        _dump(results)
    finally:
        await adapter.aclose()
        await redis.aclose()
        await dispose_engine()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repairbot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bot-info", help="карточка бота (GET /me)")
    sub.add_parser("subscribe", help="зарегистрировать вебхук")
    sub.add_parser("subscriptions", help="показать текущие подписки")
    sub.add_parser("orphan-chats", help="чаты без привязки к объекту")
    sub.add_parser("check-drive", help="проверить доступ к Диску заказчика")
    sub.add_parser(
        "google-authorize", help="разовый вход в аккаунт заказчика (запускать при нём)"
    )

    p = sub.add_parser("google-folder", help="создать папку архива на Диске")
    p.add_argument("--name", default="Ремонт — архив объектов")

    sub.add_parser("chat-titles", help="дозапросить названия чатов")

    p = sub.add_parser("check-chat", help="проверить права бота в чатах")
    p.add_argument("chat_id", nargs="*", help="без аргументов — все чаты MAX из базы")

    p = sub.add_parser("create-object", help="создать объект")
    p.add_argument("code")
    p.add_argument("address")

    p = sub.add_parser("link-chat", help="привязать чат к объекту")
    p.add_argument("chat_id")
    p.add_argument("object_code")

    p = sub.add_parser("object-alias", help="как объект называют в переписке")
    p.add_argument("object_code")
    p.add_argument("alias", nargs="?", default=None, help="без него — показать заведённые")

    p = sub.add_parser("unassigned", help="сообщения без объекта: очередь разбора")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("assign", help="отнести сообщение к объекту руками")
    p.add_argument("message_id", type=int)
    p.add_argument("object_code")

    p = sub.add_parser("create-user", help="создать пользователя веб-интерфейса")
    p.add_argument("login")
    p.add_argument("display_name")
    p.add_argument("--role", choices=["admin", "manager", "viewer"], default="manager")

    p = sub.add_parser("set-password", help="задать пароль существующему пользователю")
    p.add_argument("login")

    sub.add_parser("users", help="учётные записи панели")

    p = sub.add_parser("backfill", help="догрузить историю чата")
    p.add_argument("chat_id", nargs="?", default=None)
    p.add_argument("--limit", type=int, default=1000)

    p = sub.add_parser("archive-files", help="переложить вложения на Диск заказчика")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("recognize-documents", help="разобрать документы с Диска")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждения расхода")

    sub.add_parser("spend", help="сколько потрачено на модель")

    p = sub.add_parser("people", help="учётные записи и роли")
    p.add_argument("--identity", type=int, default=None, help="id учётной записи")
    p.add_argument("--role", choices=["staff", "client", "supplier", "unknown"], default=None)
    p.add_argument("--name", default=None, help="имя человека, если отличается")

    p = sub.add_parser("telegram", help="подключение канала Telegram")
    p.add_argument("action", choices=["info", "subscribe", "check"])
    p.add_argument("--chat-id", dest="chat_id", default=None)

    p = sub.add_parser("listen", help="показать, кто пишет боту (настройка)")
    p.add_argument("--seconds", type=int, default=60)

    p = sub.add_parser("payments", help="платёжный календарь")
    p.add_argument("--send", action="store_true", help="отправить напоминание, а не показать")

    p = sub.add_parser("digest", help="сводка руководителю")
    p.add_argument("--period", choices=["day", "week"], default="day")
    p.add_argument("--send", action="store_true", help="отправить, а не показать")

    p = sub.add_parser("send-pending", help="дослать одобренное, но неотправленное")
    p.add_argument("--limit", type=int, default=50)

    return parser


def main() -> None:
    setup_observability(get_settings())
    args = build_parser().parse_args()
    try:
        _run(args)
    except objects.ObjectsError as exc:
        # Отказ по существу — это сообщение человеку, а не трассировка.
        raise SystemExit(str(exc)) from exc


def _run(args: argparse.Namespace) -> None:
    match args.command:
        case "bot-info":
            asyncio.run(cmd_bot_info())
        case "subscribe":
            asyncio.run(cmd_subscribe())
        case "subscriptions":
            asyncio.run(cmd_subscriptions())
        case "chat-titles":
            asyncio.run(cmd_chat_titles())
        case "check-chat":
            asyncio.run(cmd_check_chat(args.chat_id))
        case "create-object":
            asyncio.run(cmd_create_object(args.code, args.address))
        case "link-chat":
            asyncio.run(cmd_link_chat(args.chat_id, args.object_code))
        case "object-alias":
            asyncio.run(cmd_object_alias(args.object_code, args.alias))
        case "unassigned":
            asyncio.run(cmd_unassigned(args.limit))
        case "assign":
            asyncio.run(cmd_assign(args.message_id, args.object_code))
        case "create-user":
            asyncio.run(cmd_create_user(args.login, args.display_name, args.role))
        case "set-password":
            asyncio.run(cmd_set_password(args.login))
        case "users":
            asyncio.run(cmd_users())
        case "check-drive":
            asyncio.run(cmd_check_drive())
        case "google-folder":
            asyncio.run(cmd_google_folder(args.name))
        case "google-authorize":
            # Синхронно: поток открывает браузер и ждёт возврата на localhost.
            cmd_google_authorize()
        case "orphan-chats":
            asyncio.run(cmd_orphan_chats())
        case "backfill":
            asyncio.run(cmd_backfill(args.chat_id, args.limit))
        case "archive-files":
            asyncio.run(cmd_archive_files(args.limit))
        case "recognize-documents":
            asyncio.run(cmd_recognize_documents(args.limit, assume_yes=args.yes))
        case "spend":
            asyncio.run(cmd_spend())
        case "people":
            asyncio.run(cmd_people(args.role, args.identity, args.name))
        case "telegram":
            asyncio.run(cmd_telegram(args.action, args.chat_id))
        case "listen":
            asyncio.run(cmd_listen(args.seconds))
        case "payments":
            asyncio.run(cmd_payments(args.send))
        case "digest":
            asyncio.run(cmd_digest(args.period, dry_run=not args.send))
        case "send-pending":
            asyncio.run(cmd_send_pending(args.limit))
        case _:  # pragma: no cover
            raise SystemExit(f"Неизвестная команда: {args.command}")


if __name__ == "__main__":
    main()

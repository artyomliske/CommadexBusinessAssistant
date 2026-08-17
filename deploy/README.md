# Развёртывание

Два сервера с разными ролями:

* **Россия** — приложение целиком: Postgres, Redis, веб, воркер. Здесь же
  персональные данные, поэтому площадка российская.
* **Германия** — только выход наружу. На нём один контейнер с прокси,
  через который идут OpenRouter и Google. Больше ничего.

MAX идёт с российского сервера напрямую, минуя прокси: он и доступен
только оттуда, и это самое нагруженное направление.

## 0. Сначала — ключи

Пароли и ключи, которыми вы делились в переписке или в мессенджере, надо
считать скомпрометированными. До установки:

```bash
# на каждом сервере
passwd root
```

Ключи OpenRouter отзываются в личном кабинете: Keys → удалить старый,
создать новый. Один ключ для потока сообщений, второй для документов —
так видно расход по каждому направлению отдельно и можно отозвать один,
не трогая другой.

## 1. Вход по ключу вместо пароля

Пароль root в открытом виде рано или поздно утекает, а перебор по SSH
идёт на любой публичный адрес круглосуточно. На своей машине:

```bash
ssh-keygen -t ed25519 -C "repairbot"
ssh-copy-id root@РОССИЯ
ssh-copy-id root@ГЕРМАНИЯ
```

Затем на обоих серверах в `/etc/ssh/sshd_config`:

```
PermitRootLogin prohibit-password
PasswordAuthentication no
```

```bash
systemctl restart ssh
```

**Проверьте вход в новом окне, не закрывая текущее.** Если ключ не
подхватился, закрытая сессия оставит вас снаружи.

## 2. Германия: прокси

```bash
apt update && apt install -y docker.io docker-compose-plugin
mkdir -p /opt/proxy && cd /opt/proxy
# скопируйте сюда deploy/proxy/ из репозитория
```

Задайте логин и пароль прокси в `.env` рядом с `docker-compose.yml`:

```
PROXY_USER=repairbot
PROXY_PASSWORD=<длинная случайная строка>
```

Сгенерировать:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

```bash
docker compose up -d
```

Закройте порт для всех, кроме российского сервера:

```bash
ufw allow from РОССИЯ to any port 3128 proto tcp
ufw allow 22/tcp
ufw --force enable
```

Пароль на прокси и ограничение по адресу — оба сразу. Открытый прокси
находят сканерами за часы, и платить за чужой трафик будете вы.

## 3. Россия: приложение

```bash
apt update && apt install -y docker.io docker-compose-plugin git
git clone <репозиторий> /opt/repairbot && cd /opt/repairbot
cp deploy/.env.production.example .env
```

Заполните `.env` — что именно, написано в самом файле. Секреты вводите
прямо там, не пересылайте их через мессенджеры.

Корневой сертификат Минцифры:

```bash
curl -o certs/russian_trusted_root_ca.crt https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
```

Проверьте, что скачался сертификат, а не страница с ошибкой:

```bash
openssl x509 -in certs/russian_trusted_root_ca.crt -noout -subject
```

Запуск:

```bash
docker compose up -d --build
```

## 4. Проверки по порядку

```bash
docker compose exec api python -m repairbot.cli bot-info
```

Карточка бота — значит токен и сертификат на месте. Пока бот на
модерации, здесь будет отказ: это ожидаемо.

```bash
docker compose exec api python -m repairbot.cli check-drive
```

Покажет, чей это аккаунт Google и сколько места. Заодно проверит, что
прокси работает: без него запрос к Google не пройдёт.

```bash
docker compose exec api python -m repairbot.cli digest --period day
```

Соберёт сводку и покажет её, не отправляя. Проверяет базу и агент отчётов.

```bash
docker compose exec api python -m repairbot.cli listen --seconds 60
```

Напишите боту в MAX — команда напечатает `chat_id` для `MANAGER_CHAT_ID`.

## 5. Домен и HTTPS

Вебхуки MAX и Telegram принимаются только по HTTPS с сертификатом
доверенного УЦ. Самоподписанный не подойдёт: до появления домена
система не получает сообщений вообще.

Caddy уже описан в `docker-compose.yml` и сам получает сертификат
Let's Encrypt. Включается профилем `web`, который ставит `configure.sh`,
когда `PUBLIC_BASE_URL` начинается с `https://`.

**1. Запись DNS.** У регистратора добавьте A-запись поддомена на IP
российского сервера. Проверить, что она разошлась:

```bash
dig +short bot.вашдомен.ру A
```

Пока запись не видна, Caddy сертификат не получит: Let's Encrypt
проверяет владение доменом, обращаясь по нему же.

**2. Адрес в конфигурации.**

```bash
ssh root@СЕРВЕР -t 'bash /opt/repairbot/deploy/configure.sh'
```

В поле адреса — `https://bot.вашдомен.ру`. Скрипт сам выведет отсюда
`PUBLIC_DOMAIN`, включит профиль `web` и вернёт `WEB_SESSION_HTTPS_ONLY`
в `true`.

**3. Запуск и подписка.**

```bash
cd /opt/repairbot && docker compose up -d --build
docker compose logs -f caddy          # ждём выдачи сертификата
docker compose exec api python -m repairbot.cli subscribe
```

Порты приложения, базы и Redis опубликованы только на петле
(`127.0.0.1`). Наружу выходит один Caddy. Так сделано намеренно: Docker
правит iptables мимо ufw, и `ufw deny` опубликованный порт не
закрывает — без явной петли база с паролем висела бы в интернете.

Пока домена нет, панель доступна туннелем:

```bash
ssh -L 8000:127.0.0.1:8000 root@СЕРВЕР
```

## 6. Google Диск и таблицы

Нужен, чтобы вложения из чатов ложились в архив заказчика, а сводки
писались в его таблицу. Вход разовый, но с двумя ловушками.

**1. Приложение в Google Cloud.** Консоль → новый проект → включить
**Google Drive API** и **Google Sheets API**. Затем OAuth consent screen:
тип **External**, добавить себя в Test users. Потом Credentials → Create
credentials → **OAuth client ID**, тип **Desktop app** → скачать JSON.

**2. Разовый вход.** Запускается **на машине заказчика или при нём**:
откроется браузер, и войти надо в тот аккаунт, которому должны
принадлежать файлы. На сервере это не работает — Google требует
возврата на localhost, а «скопируйте код» отключён с 2022 года.

```bash
cp ~/Downloads/client_secret_*.json secrets/google_client_secret.json
GOOGLE_OAUTH_CLIENT_SECRETS_PATH=secrets/google_client_secret.json \
  .venv/bin/python -m repairbot.cli google-authorize
```

Готовый `secrets/google_token.json` кладётся на сервер:

```bash
scp secrets/google_token.json root@СЕРВЕР:/opt/repairbot/secrets/
ssh root@СЕРВЕР 'chmod 600 /opt/repairbot/secrets/google_token.json'
```

**3. Папка архива — только командой.** Мы просим область доступа
`drive.file`: она даёт доступ лишь к тому, что приложение создало само.
Полный `drive` не запрашиваем намеренно — на том же аккаунте лежит вся
личная почта и документы заказчика. Из этого следует неочевидное:
**папку нельзя создать руками в браузере и вписать её номер** — для
приложения такой папки не существует, и проверка честно ответит «не
найдено» на папку, которая у заказчика прекрасно видна.

```bash
docker compose exec api python -m repairbot.cli google-folder
docker compose exec api python -m repairbot.cli check-drive
```

Первая печатает `folder_id` — его в `GOOGLE_DRIVE_FOLDER_ID`. Вторая
проверяет, что папка доступна и в неё можно писать. Ждём `"ready": true`.

**4. Production, иначе через 7 дней всё отвалится.** Пока приложение в
Google Cloud числится в статусе **Testing**, Google отзывает разрешение
через неделю — архив просто перестанет работать, без всякого события.
OAuth consent screen → **Publish app**. Проверка внешними экспертами при
областях `drive.file` и `spreadsheets` не требуется.

## Что где лежит

| Файл | Где |
|---|---|
| `.env` | `/opt/repairbot/.env`, права 600 |
| Токен Google | `/opt/repairbot/secrets/google_token.json`, права 600 |
| Сертификат Минцифры | `/opt/repairbot/certs/*.crt` |
| Данные Postgres | том Docker `repairbot_pgdata` |

Резервные копии: том Postgres и каталог `secrets/`. Файлы объектов лежат
на Google Диске заказчика и в копии на сервере не нуждаются.

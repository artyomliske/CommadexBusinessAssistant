#!/usr/bin/env bash
# Задать прокси новый пароль и прописать его на стороне приложения.
# Запускается НА ВАШЕЙ МАШИНЕ:
#
#   bash deploy/sync-proxy-password.sh 144.31.136.147 153.80.184.45
#
# Пароль рождается на зарубежном сервере, туда же и записывается, а на
# российский уезжает по ssh. На экран он не выводится и в историю
# команд не попадает: сюда его никто не вводит руками.
#
# Пароль на обеих сторонах должен совпадать байт в байт, а вводится он
# в двух разных местах — это ровно та операция, которую человек делает
# с опечаткой, а потом полдня видит «connect=407» и ищет причину в сети.

set -euo pipefail

PROXY_HOST="${1:-}"
APP_HOST="${2:-}"
PROXY_PORT="${3:-443}"
USER="${4:-root}"

if [[ -z "$PROXY_HOST" || -z "$APP_HOST" ]]; then
    echo "Использование: bash deploy/sync-proxy-password.sh ЗАРУБЕЖНЫЙ РОССИЙСКИЙ [порт]" >&2
    exit 1
fi

echo "==> Зарубежный сервер: новый пароль"
# Пароль печатается только сюда, в переменную, и дальше не показывается.
PASSWORD="$(ssh "${USER}@${PROXY_HOST}" '
    set -e
    cd /opt/proxy
    command -v htpasswd >/dev/null || apt-get install -y -qq apache2-utils >/dev/null
    P="$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")"
    htpasswd -bc passwd repairbot "$P" >/dev/null 2>&1
    # Squid работает от пользователя proxy (uid 13) и читает этот файл
    # его правами. Оставленный от root файл с правами 600 означает, что
    # программа проверки паролей не может его открыть — и тогда squid
    # отвергает ЛЮБОЙ пароль, отвечая 407. Отличить это от «пароль не
    # тот» снаружи невозможно.
    chown 13:13 passwd
    chmod 640 passwd
    docker compose restart proxy >/dev/null 2>&1
    printf "%s" "$P"
')"

if [[ -z "$PASSWORD" ]]; then
    echo "Не удалось задать пароль на прокси." >&2
    exit 1
fi

echo "==> Зарубежный сервер: проверка новым паролем"
# Именно с паролем. Проверка без него отвечает 407 при любом состоянии
# файла паролей и потому не доказывает ничего: squid просто сообщает,
# что авторизация нужна. Так и вышло в первый раз — «407» приняли за
# признак исправности, а он им не был.
PROXY_PASSWORD="$PASSWORD" ssh "${USER}@${PROXY_HOST}" \
    "PP='${PASSWORD}' PORT='${PROXY_PORT}' bash -s" <<'REMOTE'
sleep 4
# Домен обязательно из списка разрешённых, иначе squid ответит 403 —
# пароль-то принят, но ходить туда нельзя, и проверка соврёт.
code=$(curl -s -o /dev/null -w '%{http_connect}' --max-time 10 \
    -x "http://repairbot:${PP}@127.0.0.1:${PORT}" \
    https://openrouter.ai/api/v1/models || true)
if [ "$code" = "200" ]; then
    echo "  на прокси пароль принят"
else
    echo "  ПРОКСИ НЕ ПРИНЯЛ СВОЙ ЖЕ ПАРОЛЬ: код $code"
    echo "  (200 — принят, 407 — не принят, 000 — не ответил)"
fi
REMOTE

echo "==> Российский сервер: прописываю адрес прокси"
# Пароль уходит в переменную окружения ssh-сессии, а не в аргументы
# команды: аргументы видны в списке процессов всем, кто на сервере.
PROXY_PASSWORD="$PASSWORD" ssh "${USER}@${APP_HOST}" \
    "PROXY_URL='http://repairbot:${PASSWORD}@${PROXY_HOST}:${PROXY_PORT}' bash -s" <<'REMOTE'
set -e
cd /opt/repairbot
python3 - <<'PY'
import os, pathlib
url = os.environ["PROXY_URL"]
path = pathlib.Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in ("HTTPS_PROXY", "HTTP_PROXY"):
        out.append(f"{key}={url}")
        seen.add(key)
    else:
        out.append(line)
for key in ("HTTPS_PROXY", "HTTP_PROXY"):
    if key not in seen:
        out.append(f"{key}={url}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
chmod 600 .env
docker compose up -d api worker >/dev/null 2>&1
REMOTE

echo "==> Проверка пути до модели"
ssh "${USER}@${APP_HOST}" '
    cd /opt/repairbot
    sleep 8
    docker compose exec -T worker curl -s -o /dev/null \
        -w "  через прокси: connect=%{http_connect} code=%{http_code} за %{time_total}с\n" \
        --max-time 25 https://openrouter.ai/api/v1/models
'

echo
echo "connect=200 code=200 — путь до модели открыт, помощник заработает."
echo "connect=407 — пароли всё ещё разошлись, запустите скрипт ещё раз."

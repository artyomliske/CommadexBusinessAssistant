#!/usr/bin/env bash
# Установка прокси на зарубежном сервере.
#
#   scp -r deploy root@ГЕРМАНИЯ:/opt/
#   ssh root@ГЕРМАНИЯ 'bash /opt/deploy/install-proxy.sh <IP российского сервера>'
#
# Скрипт идемпотентный: повторный запуск ничего не ломает.

set -euo pipefail

ALLOWED_IP="${1:-}"
if [[ -z "$ALLOWED_IP" ]]; then
    echo "Укажите IP российского сервера: bash install-proxy.sh 1.2.3.4" >&2
    exit 1
fi

PROXY_DIR=/opt/proxy
PROXY_USER=repairbot

# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

echo "==> Система"
show_system

echo "==> Пакеты"
ensure_docker
apt-get install -y -qq apache2-utils ufw python3 >/dev/null

echo "==> Файлы"
mkdir -p "$PROXY_DIR"
cp "$(dirname "$0")/proxy/docker-compose.yml" "$PROXY_DIR/"
cp "$(dirname "$0")/proxy/squid.conf" "$PROXY_DIR/"

if [[ ! -f "$PROXY_DIR/passwd" ]]; then
    PROXY_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    htpasswd -bc "$PROXY_DIR/passwd" "$PROXY_USER" "$PROXY_PASSWORD" 2>/dev/null
    # Squid работает от пользователя proxy (uid 13 в образе) и читает
    # файл паролей его правами. Файл, оставшийся от root с правами 600,
    # программе проверки недоступен — и squid отвергает ЛЮБОЙ пароль,
    # отвечая 407. Снаружи это неотличимо от «пароль не тот», и ищут
    # потом не там: в настройках приложения, а не в правах файла.
    chown 13:13 "$PROXY_DIR/passwd"
    chmod 640 "$PROXY_DIR/passwd"
    chmod 600 "$PROXY_DIR/passwd"
    # Пароль печатается один раз и нигде не сохраняется: дальше он живёт
    # только в .env российского сервера.
    echo
    echo "  ЗАПИШИТЕ, БОЛЬШЕ НЕ ПОКАЖЕМ:"
    echo "  HTTPS_PROXY=http://${PROXY_USER}:${PROXY_PASSWORD}@$(hostname -I | awk '{print $1}'):3128"
    echo
else
    echo "    файл паролей уже есть — оставляем как есть"
fi

echo "==> Запуск"
cd "$PROXY_DIR"
docker compose up -d

echo "==> Firewall"
# Порт прокси открыт только российскому серверу. Пароль и ограничение по
# адресу нужны оба: открытый прокси находят сканерами за часы.
ufw allow 22/tcp >/dev/null
ufw allow from "$ALLOWED_IP" to any port 3128 proto tcp >/dev/null
ufw --force enable >/dev/null

echo
echo "Готово. Проверьте с российского сервера:"
echo "  curl -x http://${PROXY_USER}:ПАРОЛЬ@$(hostname -I | awk '{print $1}'):3128 \\"
echo "       https://openrouter.ai/api/v1/models -o /dev/null -w '%{http_code}\\n'"
echo "Должно быть 200. А https://example.com должен дать 403 — список закрытый."

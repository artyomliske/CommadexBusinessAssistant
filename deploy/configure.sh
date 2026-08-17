#!/usr/bin/env bash
# Заполнение .env вопросами вместо правки в редакторе.
#
#   ssh root@СЕРВЕР -t 'bash /opt/repairbot/deploy/configure.sh'
#
# Ключи вводятся здесь, на сервере, и никуда больше не уходят: ввод
# скрыт, в историю оболочки не попадает, в вывод не печатается.
#
# Запускать можно сколько угодно раз: пустой ответ оставляет текущее
# значение, так что менять по одному полю тоже можно.

set -euo pipefail

ENV_FILE=/opt/repairbot/.env
[[ -f "$ENV_FILE" ]] || { echo "Нет $ENV_FILE — сначала install-app.sh" >&2; exit 1; }

current() { grep "^$1=" "$ENV_FILE" | cut -d= -f2- || true; }

# Заглушка из шаблона — это не значение. Показывать её как «задан»
# значит подталкивать нажать Enter и оставить всё как было.
is_placeholder() {
    [[ "$1" == *"ЗАПОЛНИТЬ"* || "$1" == *"вашдомен"* || "$1" == *"IP_ГЕРМАНИИ"* \
       || "$1" == *"ПАРОЛЬ"* ]]
}

# Показываем текущее значение, но секреты — только фактом наличия.
show() {
    local value="$1" secret="${2:-}"
    if [[ -z "$value" ]] || is_placeholder "$value"; then echo "НЕ ЗАПОЛНЕНО"
    elif [[ -n "$secret" ]]; then echo "задан (${#value} символов)"
    else echo "$value"; fi
}

ask() {
    local key="$1" prompt="$2" secret="${3:-}" now answer
    now="$(current "$key")"
    echo
    echo "$prompt"
    echo "  сейчас: $(show "$now" "$secret")"
    if [[ -n "$secret" ]]; then
        read -r -s -p "  новое (Enter — оставить): " answer
        echo
    else
        read -r -p "  новое (Enter — оставить): " answer
    fi
    if [[ -n "$answer" ]]; then
        set_value "$key" "$answer"
    elif is_placeholder "$now"; then
        echo "  ! осталась заглушка — установка на этом остановится"
    fi
}

# Подстановка через python: значения содержат @ / : и что угодно ещё,
# и sed на них ломается по-разному в зависимости от символа.
set_value() {
    KEY="$1" VALUE="$2" ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os, pathlib
key, value, path = os.environ["KEY"], os.environ["VALUE"], pathlib.Path(os.environ["ENV_FILE"])
lines = path.read_text(encoding="utf-8").splitlines()
out, replaced = [], False
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={value}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

echo "Заполнение /opt/repairbot/.env"
echo "Пустой ответ оставляет текущее значение."

ask PUBLIC_BASE_URL \
    "Адрес панели. С доменом — https://bot.домен.ру, без домена — http://IP:8000"

ask HTTPS_PROXY \
    "Прокси (строка из установки зарубежного сервера)" secret
# Один и тот же адрес: httpx и requests смотрят на разные переменные.
proxy="$(current HTTPS_PROXY)"
[[ -n "$proxy" ]] && set_value HTTP_PROXY "$proxy"

ask MAX_ACCESS_TOKEN \
    "Токен бота MAX (кабинет организации, Расширенные настройки)" secret

ask MANAGER_CHAT_ID \
    "Чат для сводок и напоминаний. Пока не знаете — оставьте пустым,
  получите командой listen и впишете потом"

ask ASSISTANT_USER_IDS \
    "Кому бот отвечает в личке как помощник (идентификаторы через запятую).
  Пусто — помощник выключен. Он видит деньги и всю картину компании"

ask LLM_API_KEY "Ключ OpenRouter для потока сообщений" secret
ask LLM_MODEL "Модель для сообщений (точное имя из списка OpenRouter)"
ask RECOGNIZE_MODEL "Модель для распознавания документов"

# --- то, что вычисляется само ---

base="$(current PUBLIC_BASE_URL)"
if [[ "$base" == https://* ]]; then
    set_value WEB_SESSION_HTTPS_ONLY true
    # Домен для Caddy — тот же адрес без схемы и без пути. Спрашивать
    # его отдельно значило бы однажды получить панель на одном имени,
    # а сертификат на другом.
    domain="${base#https://}"
    set_value PUBLIC_DOMAIN "${domain%%/*}"
    set_value COMPOSE_PROFILES web
else
    # По http cookie с флагом Secure не сохраняется, и вход бесконечно
    # возвращает на форму — без единой ошибки в логах. Выставляем сами,
    # чтобы на эти грабли нельзя было наступить.
    set_value WEB_SESSION_HTTPS_ONLY false
    # Без домена Caddy поднимать нечего: сертификат он получить не
    # сможет, а 80 и 443 займёт.
    set_value COMPOSE_PROFILES ""
    set_value PUBLIC_DOMAIN ""
    echo
    echo "  Адрес без https — выключил WEB_SESSION_HTTPS_ONLY и Caddy."
    echo "  Панель снаружи при этом недоступна: приложение слушает"
    echo "  только петлю. Попасть в неё можно туннелем:"
    echo "    ssh -L 8000:127.0.0.1:8000 root@СЕРВЕР"
fi

chmod 600 "$ENV_FILE"

echo
echo "==> Что получилось"
for key in PUBLIC_BASE_URL PUBLIC_DOMAIN COMPOSE_PROFILES LLM_MODEL RECOGNIZE_MODEL \
           ASSISTANT_USER_IDS \
           MANAGER_CHAT_ID WEB_SESSION_HTTPS_ONLY; do
    printf "  %-24s %s\n" "$key" "$(current "$key")"
done
for key in HTTPS_PROXY LLM_API_KEY MAX_ACCESS_TOKEN POSTGRES_PASSWORD WEB_SESSION_SECRET; do
    printf "  %-24s %s\n" "$key" "$(show "$(current "$key")" secret)"
done

echo
echo "Дальше: bash /opt/repairbot/deploy/install-app.sh"

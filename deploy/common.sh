#!/usr/bin/env bash
# Общее для обоих серверов.

# Установка Docker и Compose так, чтобы работало на любом Debian/Ubuntu.
#
# `docker-compose-plugin` лежит в репозитории Docker, а не дистрибутива:
# на чистой Ubuntu apt его не находит. В новых Ubuntu есть свой пакет
# `docker-compose-v2`, в старых — нет вовсе. Поэтому три попытки подряд,
# от самой простой к самой надёжной.
ensure_docker() {
    export DEBIAN_FRONTEND=noninteractive
    # На Ubuntu 22.04 needrestart открывает диалог «какие службы
    # перезапустить» и ждёт ответа. По SSH в скрипте отвечать некому,
    # и установка висит молча. DEBIAN_FRONTEND его не глушит — нужен
    # свой переключатель.
    export NEEDRESTART_MODE=a
    export NEEDRESTART_SUSPEND=1
    apt-get update -qq
    apt-get install -y -qq docker.io curl ca-certificates >/dev/null

    systemctl enable --now docker >/dev/null 2>&1 || true

    if docker compose version >/dev/null 2>&1; then
        echo "    compose уже есть: $(docker compose version --short 2>/dev/null)"
        return 0
    fi

    # Попытка 1: пакет дистрибутива (Ubuntu 23.04+).
    if apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 \
        && docker compose version >/dev/null 2>&1; then
        echo "    compose из docker-compose-v2"
        return 0
    fi

    # Попытка 2: пакет из репозитория Docker, если он подключён.
    if apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 \
        && docker compose version >/dev/null 2>&1; then
        echo "    compose из docker-compose-plugin"
        return 0
    fi

    # Попытка 3: официальный бинарник. Работает всегда и ни от чего
    # не зависит, кроме доступа в сеть.
    echo "    ставлю compose бинарником с github"
    local arch plugin_dir
    case "$(uname -m)" in
        x86_64)  arch=x86_64 ;;
        aarch64) arch=aarch64 ;;
        armv7l)  arch=armv7 ;;
        *) echo "Неизвестная архитектура: $(uname -m)" >&2; return 1 ;;
    esac

    plugin_dir=/usr/local/lib/docker/cli-plugins
    mkdir -p "$plugin_dir"
    curl -fsSL -o "$plugin_dir/docker-compose" \
        "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}"
    chmod +x "$plugin_dir/docker-compose"

    if ! docker compose version >/dev/null 2>&1; then
        echo "Не удалось поставить docker compose" >&2
        return 1
    fi
    echo "    compose: $(docker compose version --short 2>/dev/null)"
}

# Что за система — пригодится, если что-то пойдёт не так.
show_system() {
    echo "    $(. /etc/os-release && echo "$PRETTY_NAME") · $(uname -m)"
}

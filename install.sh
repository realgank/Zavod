#!/usr/bin/env bash
set -euo pipefail

#############################
# Helper functions
#############################

error() {
    echo "[Ошибка] $*" >&2
}

info() {
    echo "[Инфо] $*"
}

prompt_with_default() {
    local prompt_text="$1"
    local default_value="$2"
    local input
    read -r -p "${prompt_text} [${default_value}]: " input || true
    if [[ -z "${input}" ]]; then
        echo "${default_value}"
    else
        echo "${input}"
    fi
}

prompt_required() {
    local prompt_text="$1"
    local default_value="${2:-}"
    local input

    while true; do
        if [[ -n "${default_value}" ]]; then
            read -r -p "${prompt_text} [${default_value}]: " input || true
            input=${input:-${default_value}}
        else
            read -r -p "${prompt_text}: " input || true
        fi

        if [[ -n "${input}" ]]; then
            echo "${input}"
            return 0
        fi

        echo "Значение не может быть пустым." >&2
    done
}

prompt_secret() {
    local prompt_text="$1"
    local default_value="${2:-}"
    local allow_empty="${3:-false}"
    local input

    while true; do
        if [[ -n "${default_value}" ]]; then
            read -r -s -p "${prompt_text} (оставьте пустым, чтобы сохранить текущее значение): " input || true
            echo
            if [[ -z "${input}" ]]; then
                echo "${default_value}"
                return 0
            fi
        else
            read -r -s -p "${prompt_text}: " input || true
            echo
        fi

        if [[ -n "${input}" ]]; then
            echo "${input}"
            return 0
        fi

        if [[ "${allow_empty}" == "true" ]]; then
            echo ""
            return 0
        fi

        echo "Значение не может быть пустым." >&2
    done
}

confirm() {
    local prompt_text="$1"
    local default_answer="$2"
    local input
    local default_hint

    if [[ "${default_answer}" == "y" ]]; then
        default_hint="Y/n"
    else
        default_hint="y/N"
    fi

    read -r -p "${prompt_text} (${default_hint}): " input || true
    input=${input:-${default_answer}}
    case "${input}" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

#############################
# Root / sudo detection
#############################

if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *"ubuntu"* ]]; then
        error "Скрипт предназначен для Ubuntu. Обнаружена система: ${PRETTY_NAME:-неизвестно}."
        exit 1
    fi
else
    error "Не удалось определить операционную систему."
    exit 1
fi

if command -v sudo >/dev/null 2>&1; then
    if [[ ${EUID} -eq 0 ]]; then
        SUDO=""
    else
        SUDO="sudo"
    fi
else
    if [[ ${EUID} -ne 0 ]]; then
        error "Скрипт должен запускаться из-под root или при наличии sudo."
        exit 1
    fi
    SUDO=""
fi

#############################
# Install base packages
#############################

info "Обновление списка пакетов..."
${SUDO:-} apt-get update

${SUDO:-} apt-get install -y python3 python3-venv python3-pip git

#############################
# Installation directory setup
#############################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$HOME/zavod-bot"
INSTALL_DIR=$(prompt_with_default "Введите директорию установки" "${DEFAULT_INSTALL_DIR}")
INSTALL_DIR="${INSTALL_DIR%/}"

if [[ -d "${INSTALL_DIR}" ]]; then
    if [[ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]]; then
        if confirm "Директория ${INSTALL_DIR} не пуста. Очистить её перед установкой?" "n"; then
            info "Очистка директории ${INSTALL_DIR}..."
            rm -rf "${INSTALL_DIR}"
            mkdir -p "${INSTALL_DIR}"
        else
            error "Установка отменена пользователем."
            exit 1
        fi
    fi
else
    info "Создание директории ${INSTALL_DIR}..."
    mkdir -p "${INSTALL_DIR}"
fi

#############################
# Copy project files
#############################

if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" ]]; then
    info "Копирование файлов проекта..."
    shopt -s dotglob nullglob
    for item in "${SCRIPT_DIR}"/*; do
        name="$(basename "${item}")"
        case "${name}" in
            .venv|__pycache__|install.sh)
                continue
                ;;
        esac
        if [[ -d "${item}" ]]; then
            rm -rf "${INSTALL_DIR}/${name}"
            cp -R "${item}" "${INSTALL_DIR}/${name}"
        else
            cp "${item}" "${INSTALL_DIR}/${name}"
        fi
    done
    shopt -u dotglob nullglob
else
    info "Исходная и целевая директории совпадают, копирование пропущено."
fi

#############################
# Python virtual environment
#############################

info "Настройка виртуального окружения..."
python3 -m venv "${INSTALL_DIR}/.venv"
# shellcheck disable=SC1090
source "${INSTALL_DIR}/.venv/bin/activate"
python -m pip install --upgrade pip
if [[ ! -f "${INSTALL_DIR}/requirements.txt" ]]; then
    error "Файл requirements.txt не найден в директории установки (${INSTALL_DIR})."
    exit 1
fi
python -m pip install -r "${INSTALL_DIR}/requirements.txt"

deactivate

#############################
# Environment variables
#############################

ENV_FILE="${INSTALL_DIR}/.env"
info "Настройка переменных окружения (файл ${ENV_FILE})"

declare -A EXISTING_ENV=()
if [[ -f "${ENV_FILE}" ]]; then
    info "Обнаружен существующий файл .env. Значения будут использованы по умолчанию."
    while IFS='=' read -r key value; do
        [[ -z "${key}" || "${key}" == \#* ]] && continue
        EXISTING_ENV["${key}"]="${value}"
    done < "${ENV_FILE}"
fi

DISCORD_TOKEN_DEFAULT="${EXISTING_ENV[DISCORD_TOKEN]:-}"
DISCORD_TOKEN=$(prompt_secret "Введите Discord токен" "${DISCORD_TOKEN_DEFAULT}" "false")
if [[ -z "${DISCORD_TOKEN}" ]]; then
    error "Discord токен не может быть пустым."
    exit 1
fi

GITHUB_USERNAME_DEFAULT="${EXISTING_ENV[GITHUB_USERNAME]:-}"
GITHUB_USERNAME=$(prompt_with_default "GitHub имя пользователя (для приватного репозитория, можно оставить пустым)" "${GITHUB_USERNAME_DEFAULT}")

GITHUB_TOKEN_DEFAULT="${EXISTING_ENV[GITHUB_TOKEN]:-}"
GITHUB_TOKEN=$(prompt_secret "GitHub токен (Personal Access Token, можно оставить пустым)" "${GITHUB_TOKEN_DEFAULT}" "true")

if { [[ -n "${GITHUB_USERNAME}" ]] && [[ -z "${GITHUB_TOKEN}" ]]; } || \
   { [[ -z "${GITHUB_USERNAME}" ]] && [[ -n "${GITHUB_TOKEN}" ]]; }; then
    info "Указаны не все данные для GitHub. Значения будут проигнорированы."
    GITHUB_USERNAME=""
    GITHUB_TOKEN=""
fi

cat > "${ENV_FILE}" <<EOF_ENV
DISCORD_TOKEN=${DISCORD_TOKEN}
EOF_ENV

if [[ -n "${GITHUB_USERNAME}" ]]; then
    {
        echo "GITHUB_USERNAME=${GITHUB_USERNAME}"
        echo "GITHUB_TOKEN=${GITHUB_TOKEN}"
    } >> "${ENV_FILE}"
fi

chmod 600 "${ENV_FILE}"

#############################
# systemd service (optional)
#############################

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    ORIGIN_URL=$(git -C "${INSTALL_DIR}" remote get-url origin 2>/dev/null || true)
    if [[ -n "${ORIGIN_URL}" ]]; then
        if [[ "${ORIGIN_URL}" == git@github.com:* ]]; then
            HTTPS_URL="https://github.com/${ORIGIN_URL#git@github.com:}"
            info "Настройка origin на использование HTTPS: ${HTTPS_URL}"
            git -C "${INSTALL_DIR}" remote set-url origin "${HTTPS_URL}"
            ORIGIN_URL="${HTTPS_URL}"
        fi

        if [[ -n "${GITHUB_USERNAME}" ]]; then
            if [[ "${ORIGIN_URL}" == https://* ]]; then
                HOST_WITH_PATH="${ORIGIN_URL#https://}"
                HOST_WITH_PATH="${HOST_WITH_PATH#${GITHUB_USERNAME}@}"
                NEW_URL="https://${GITHUB_USERNAME}@${HOST_WITH_PATH}"
                if [[ "${ORIGIN_URL}" != "${NEW_URL}" ]]; then
                    info "Добавление имени пользователя в origin: ${NEW_URL}"
                    git -C "${INSTALL_DIR}" remote set-url origin "${NEW_URL}"
                fi
            else
                info "Не удалось автоматически настроить HTTPS origin. Проверьте настройки Git вручную."
            fi
        fi
    else
        info "Не найден удалённый репозиторий origin. Команда !update_bot может быть недоступна."
    fi
else
    info "Директория не является Git-репозиторием. Команда !update_bot работать не будет."
fi

if confirm "Создать systemd сервис для автозапуска?" "n"; then
    SERVICE_NAME=$(prompt_with_default "Имя сервиса" "zavod-bot")
    DEFAULT_SERVICE_USER=$(id -un)
    SERVICE_USER=$(prompt_with_default "Пользователь для запуска" "${DEFAULT_SERVICE_USER}")
    if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
        error "Пользователь '${SERVICE_USER}' не существует."
        exit 1
    fi
    SERVICE_GROUP=$(id -gn "${SERVICE_USER}")

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    CURRENT_OWNER=$(stat -c %U "${INSTALL_DIR}" 2>/dev/null || echo "")
    if [[ -n "${CURRENT_OWNER}" && "${SERVICE_USER}" != "${CURRENT_OWNER}" ]]; then
        info "Изменение владельца ${INSTALL_DIR} на ${SERVICE_USER}:${SERVICE_GROUP}"
        ${SUDO:-} chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}"
    fi

    info "Создание сервиса ${SERVICE_NAME}..."
    SERVICE_CONTENT="[Unit]\nDescription=Zavod Discord Bot\nAfter=network.target\n\n[Service]\nType=simple\nWorkingDirectory=${INSTALL_DIR}\nEnvironmentFile=${ENV_FILE}\nExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/bot.py\nRestart=on-failure\nUser=${SERVICE_USER}\nGroup=${SERVICE_GROUP}\n\n[Install]\nWantedBy=multi-user.target\n"

    printf '%b' "${SERVICE_CONTENT}" | ${SUDO:-} tee "${SERVICE_FILE}" >/dev/null
    ${SUDO:-} systemctl daemon-reload

    if confirm "Запустить сервис сейчас?" "y"; then
        ${SUDO:-} systemctl enable "${SERVICE_NAME}"
        ${SUDO:-} systemctl restart "${SERVICE_NAME}"
        info "Сервис ${SERVICE_NAME} запущен."
    else
        info "Вы можете запустить сервис командой: sudo systemctl start ${SERVICE_NAME}"
    fi
fi

info "Установка завершена. Для запуска бота выполните:\nsource ${INSTALL_DIR}/.venv/bin/activate && python ${INSTALL_DIR}/bot.py"

#!/usr/bin/env bash
set -euo pipefail

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

set_env_var() {
    local key="$1"
    local value="$2"
    local env_file="$3"

    if [[ -f "${env_file}" ]] && grep -q "^${key}=" "${env_file}"; then
        sed -i "s#^${key}=.*#${key}=${value}#" "${env_file}"
    else
        echo "${key}=${value}" >> "${env_file}"
    fi
}

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

DEFAULT_INSTALL_DIR="$(pwd)"
INSTALL_DIR=$(prompt_with_default "Укажите директорию установки Zavod" "${DEFAULT_INSTALL_DIR}")
INSTALL_DIR="${INSTALL_DIR%/}"

if [[ ! -d "${INSTALL_DIR}" ]]; then
    error "Директория ${INSTALL_DIR} не существует."
    exit 1
fi

ENV_FILE="${INSTALL_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    error "Файл ${ENV_FILE} не найден. Убедитесь, что бот установлен и файл .env создан."
    exit 1
fi

DEFAULT_SERVICE_NAME="zavod-bot"
SERVICE_NAME=$(prompt_with_default "Имя systemd сервиса" "${DEFAULT_SERVICE_NAME}")
DEFAULT_SERVICE_USER=$(id -un)
SERVICE_USER=$(prompt_with_default "Пользователь, от имени которого запускать сервис" "${DEFAULT_SERVICE_USER}")

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    error "Пользователь '${SERVICE_USER}' не существует."
    exit 1
fi

SERVICE_GROUP=$(id -gn "${SERVICE_USER}")
PYTHON_BIN="${INSTALL_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    if [[ -x "${INSTALL_DIR}/.venv/bin/python3" ]]; then
        PYTHON_BIN="${INSTALL_DIR}/.venv/bin/python3"
    else
        error "Не удалось найти Python в виртуальном окружении (${INSTALL_DIR}/.venv)."
        exit 1
    fi
fi

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

CURRENT_OWNER=$(stat -c %U "${INSTALL_DIR}" 2>/dev/null || echo "")
if [[ -n "${CURRENT_OWNER}" && "${SERVICE_USER}" != "${CURRENT_OWNER}" ]]; then
    if confirm "Изменить владельца ${INSTALL_DIR} на ${SERVICE_USER}?" "y"; then
        info "Изменение владельца ${INSTALL_DIR} на ${SERVICE_USER}:${SERVICE_GROUP}"
        ${SUDO:-} chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}"
    else
        info "Владелец директории не изменён. Убедитесь, что у сервиса достаточно прав."
    fi
fi

info "Создание файла сервиса ${SERVICE_FILE}..."
SERVICE_CONTENT="[Unit]\\nDescription=Zavod Discord Bot\\nAfter=network.target\\n\\n[Service]\\nType=simple\\nWorkingDirectory=${INSTALL_DIR}\\nEnvironmentFile=${ENV_FILE}\\nExecStart=${PYTHON_BIN} ${INSTALL_DIR}/bot.py\\nRestart=on-failure\\nUser=${SERVICE_USER}\\nGroup=${SERVICE_GROUP}\\n\\n[Install]\\nWantedBy=multi-user.target\\n"

printf '%b' "${SERVICE_CONTENT}" | ${SUDO:-} tee "${SERVICE_FILE}" >/dev/null
${SUDO:-} systemctl daemon-reload

set_env_var "BOT_AUTO_RESTART" "1" "${ENV_FILE}"

if confirm "Включить и запустить сервис сейчас?" "y"; then
    ${SUDO:-} systemctl enable "${SERVICE_NAME}"
    ${SUDO:-} systemctl restart "${SERVICE_NAME}"
    info "Сервис ${SERVICE_NAME} запущен."
else
    info "Сервис создан. Для запуска выполните: sudo systemctl start ${SERVICE_NAME}"
fi

info "Готово."

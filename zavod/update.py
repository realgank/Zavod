from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from asyncio import subprocess
from typing import Optional

from .env import env_flag

logger = logging.getLogger(__name__)


async def pull_latest_code() -> str:
    logger.info("Запускаю обновление кода из GitHub")
    env = os.environ.copy()
    github_username = env.get("GITHUB_USERNAME")
    github_token = env.get("GITHUB_TOKEN")
    askpass_path: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None

    try:
        if github_token and github_username:
            fd, askpass_path = tempfile.mkstemp(prefix="git-askpass-", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as askpass_file:
                askpass_file.write("#!/usr/bin/env bash\n")
                askpass_file.write("case \"$1\" in\n")
                askpass_file.write("    *'Username'*|*'username'*)\n")
                askpass_file.write(f"        echo {shlex.quote(github_username)}\n")
                askpass_file.write("        ;;\n")
                askpass_file.write("    *)\n")
                askpass_file.write(f"        echo {shlex.quote(github_token)}\n")
                askpass_file.write("        ;;\n")
                askpass_file.write("esac\n")
            os.chmod(askpass_path, 0o700)
            env["GIT_ASKPASS"] = askpass_path
            env["SSH_ASKPASS"] = askpass_path
            env["GIT_TERMINAL_PROMPT"] = "0"

        process = await asyncio.create_subprocess_exec(
            "git",
            "pull",
            "--ff-only",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except Exception:
        if askpass_path:
            try:
                os.remove(askpass_path)
            except FileNotFoundError:
                pass
        raise

    if process is None:
        raise RuntimeError("Не удалось запустить git pull")

    stdout, stderr = await process.communicate()
    if askpass_path:
        try:
            os.remove(askpass_path)
        except FileNotFoundError:
            pass
    if process.returncode != 0:
        error_output = stderr.decode().strip() or stdout.decode().strip()
        logger.error(
            "Команда git pull завершилась с ошибкой %s: %s",
            process.returncode,
            error_output,
        )
        raise RuntimeError(error_output or "Не удалось выполнить git pull")
    output = stdout.decode().strip()
    if not output:
        output = "Изменений нет"
    logger.info("Обновление кода завершено: %s", output)
    return output


async def schedule_process_restart(delay: float) -> None:
    """Завершить текущий процесс после указанной задержки."""

    await asyncio.sleep(delay)
    logger.info("Завершение процесса для автоматического перезапуска")
    os._exit(1)


async def restart_service_if_configured() -> Optional[str]:
    """Перезапустить сервис, если настроены соответствующие переменные окружения."""

    restart_command = os.getenv("BOT_RESTART_COMMAND")
    if restart_command:
        logger.info("Перезапуск бота командой: %s", restart_command)
        process = await asyncio.create_subprocess_shell(
            restart_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            error_output = stderr.decode().strip() or stdout.decode().strip()
            logger.error(
                "Команда перезапуска завершилась с ошибкой %s: %s",
                process.returncode,
                error_output,
            )
            raise RuntimeError(
                error_output
                or "Команда перезапуска завершилась с ненулевым кодом возврата"
            )
        command_output = stdout.decode().strip()
        if not command_output:
            command_output = "Команда перезапуска выполнена успешно."
        logger.info("Перезапуск с помощью команды завершён успешно")
        return command_output

    if env_flag("BOT_AUTO_RESTART", default=False):
        try:
            delay = float(os.getenv("BOT_AUTO_RESTART_DELAY", "5"))
        except ValueError:
            delay = 5.0
        if delay < 0:
            delay = 0
        logger.info(
            "Настроен автоматический перезапуск после обновления через %s секунд",
            delay,
        )
        asyncio.create_task(schedule_process_restart(delay))
        return "Запланирован автоматический перезапуск бота."

    logger.info(
        "Переменные BOT_RESTART_COMMAND и BOT_AUTO_RESTART не заданы, перезапуск пропущен"
    )
    return None

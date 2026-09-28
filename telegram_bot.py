"""Small Telegram Bot API helper for discovering chat and forum topic IDs.

No Telegram credentials are stored in this source file. The token is read from
the local .env file or TELEGRAM_BOT_TOKEN environment variable.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from vacancy_automation import demo_post, scheduler_tick
from work_russia_automation import caption_for_telegram, generate_vacancy_card, refresh_work_russia, work_russia_tick


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", ROOT)).expanduser()
STATE_PATH = DATA_DIR / "bot_state.json"
LOG_PATH = DATA_DIR / "telegram_bot.log"
CONFIG_PATH = ROOT / "config.json"
API_ROOT = "https://api.telegram.org"


class TelegramAPIError(RuntimeError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def load_local_env() -> None:
    """Load simple KEY=value lines from .env without overriding process env."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def get_token() -> str:
    load_local_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "replace_with_botfather_token":
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and add "
            "the token from @BotFather."
        )
    return token


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def _multipart_body(payload: dict[str, Any], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = "----FoodJobs" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            field_value = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            field_value = "true" if value else "false"
        else:
            field_value = str(value)
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            field_value.encode("utf-8"),
            b"\r\n",
        ])
    for field_name, file_path in files.items():
        file_path = Path(file_path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n",
        ])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def api_call(
    method: str,
    payload: dict[str, Any] | None = None,
    files: dict[str, Path] | None = None,
) -> Any:
    token = get_token()
    if files:
        data, content_type = _multipart_body(payload or {}, files)
    else:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    request = Request(
        f"{API_ROOT}/bot{token}/{method}",
        data=data,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urlopen(request, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read(8192).decode("utf-8", errors="replace")
        try:
            error_data = json.loads(body)
        except json.JSONDecodeError:
            error_data = {}
        description = error_data.get("description") or f"HTTP {exc.code}"
        retry_after = (error_data.get("parameters") or {}).get("retry_after")
        raise TelegramAPIError(f"Telegram API {method}: {description}", retry_after) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise TelegramAPIError(f"Telegram API {method}: connection failed ({type(exc).__name__})") from None
    except json.JSONDecodeError:
        raise TelegramAPIError(f"Telegram API {method}: invalid JSON response") from None

    if not result.get("ok"):
        retry_after = (result.get("parameters") or {}).get("retry_after")
        raise TelegramAPIError(
            f"Telegram API {method}: {result.get('description', 'unknown error')}",
            retry_after,
        )
    return result.get("result")


def send_message(chat_id: int | str, text: str, thread_id: int | None = None) -> Any:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return api_call("sendMessage", payload)


def send_photo(chat_id: int | str, photo_path: Path, thread_id: int | None = None) -> Any:
    payload: dict[str, Any] = {"chat_id": chat_id}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return api_call("sendPhoto", payload, files={"photo": photo_path})


def load_offset() -> int | None:
    if not STATE_PATH.exists():
        return None
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        value = state.get("offset")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logging.exception("Could not read bot state; starting without an offset")
        return None


def save_offset(offset: int) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    temporary.replace(STATE_PATH)


def command_from(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].lower()


def handle_update(update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("channel_post")
    if not message:
        return

    text = message.get("text", "")
    command = command_from(text)
    if command not in {"/start", "/whereami", "/id", "/ping", "/status"}:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return

    thread_id = message.get("message_thread_id")
    if command == "/start":
        answer = (
            "Бот подключён. Чтобы узнать ID этого чата и темы, отправьте "
            "/whereami."
        )
    elif command == "/ping":
        answer = "Бот на связи."
    elif command == "/status":
        config = load_config()
        answer = (
            f"Режим: {config.get('mode', 'не задан')}\n"
            f"Автопубликация: {'включена' if config.get('automation_enabled') else 'выключена'}\n"
            f"Источник: {config.get('source', 'не задан')}\n"
            f"Проверка вакансий: каждые {config.get('source_poll_interval_minutes', 10)} минут"
        )
    else:
        answer = (
            f"chat_id: {chat_id}\n"
            f"chat_type: {chat.get('type', 'unknown')}\n"
            f"message_thread_id: {thread_id if thread_id is not None else 'не передан'}"
        )

    reply_payload: dict[str, Any] = {"chat_id": chat_id, "text": answer}
    if thread_id is not None:
        reply_payload["message_thread_id"] = thread_id
    api_call("sendMessage", reply_payload)
    logging.info("Handled %s in chat %s, thread %s", command, chat_id, thread_id)


def run() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )
    get_token()
    config = load_config()
    offset = load_offset()
    logging.info("Telegram bot started in %s mode.", config.get("mode", "unknown"))
    if config.get("source") == "work_russia":
        try:
            from work_russia_automation import WorkRussiaStore
            reset = WorkRussiaStore().reset_interrupted_photo()
            if reset:
                logging.warning("Re-queued %s interrupted image uploads", reset)
        except Exception:
            logging.exception("Could not recover the local vacancy queue")

    while True:
        try:
            if config.get("source") == "work_russia":
                work_russia_tick(config, api_call)
            else:
                scheduler_tick(config, api_call)
            payload: dict[str, Any] = {"timeout": 5, "allowed_updates": ["message", "channel_post"]}
            if offset is not None:
                payload["offset"] = offset
            updates = api_call("getUpdates", payload)
            for update in updates:
                handle_update(update)
                offset = int(update["update_id"]) + 1
                save_offset(offset)
            if config.get("source") == "work_russia":
                work_russia_tick(config, api_call)
            else:
                scheduler_tick(config, api_call)
        except KeyboardInterrupt:
            logging.info("Telegram helper stopped by user")
            return
        except Exception:
            logging.exception("Telegram helper encountered an error; retrying in 10 seconds")
            time.sleep(10)


def run_scheduled_once() -> None:
    """Run only the slot named by a GitHub Actions schedule event."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )
    config = load_config()
    cron = os.environ.get("GITHUB_EVENT_SCHEDULE", "").strip()
    cron_fields = cron.split()
    if len(cron_fields) != 5 or not cron_fields[1].isdigit():
        logging.info("Manual or unrecognized workflow run; scheduled publication skipped")
        return

    timezone = ZoneInfo(config.get("timezone", "Europe/Moscow"))
    now = datetime.now(timezone)
    scheduled_hour = int(cron_fields[1])
    slot_time = next(
        (value for value in config.get("schedule_times", [])
         if int(value.split(":", 1)[0]) == scheduled_hour),
        None,
    )
    if slot_time is None:
        logging.error("Workflow schedule hour %s is not configured", scheduled_hour)
        raise SystemExit(1)

    hour, minute = (int(part) for part in slot_time.split(":"))
    scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    lateness = (now - scheduled_at).total_seconds() / 60
    grace = int(config.get("schedule_grace_minutes", 120))
    if lateness < 0 or lateness > grace:
        logging.warning("Scheduled run is outside the %s-minute delivery window; skipped", grace)
        return

    if not (
        config.get("automation_enabled")
        and config.get("hh_source_enabled")
        and config.get("hh_redistribution_confirmed")
    ):
        logging.info("Publishing gates are closed; no post sent")
        return

    result = scheduler_tick(config, api_call, now=scheduled_at)
    logging.info("Scheduled slot result: %s", result or "no action")
    if result in {"error", "blocked", "captcha_required"}:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restaurant jobs Telegram bot")
    parser.add_argument(
        "--demo-topic",
        action="append",
        help="Send a clearly labeled demo post to a configured test topic",
    )
    parser.add_argument(
        "--scheduled-once",
        action="store_true",
        help="Run one GitHub Actions schedule slot, then exit",
    )
    parser.add_argument(
        "--check-work-russia",
        action="store_true",
        help="Read and queue matching Work Russia vacancies without publishing them",
    )
    args = parser.parse_args()
    try:
        if args.scheduled_once:
            run_scheduled_once()
        elif args.check_work_russia:
            config = load_config()
            from work_russia_automation import WorkRussiaStore

            queued = refresh_work_russia(config, force=True)
            counts = WorkRussiaStore().queued_counts()
            print(
                f"Проверка завершена; добавлено новых вакансий: {queued}. "
                f"В очереди: повар — {counts.get('Повар', 0)}, "
                f"кондитер — {counts.get('Кондитер', 0)}."
            )
        elif args.demo_topic:
            config = load_config()
            if config.get("mode") != "test":
                raise RuntimeError("Demo posts are available only in test mode")
            target = config["target"]
            for topic_name in args.demo_topic:
                topic = next(
                    (item for item in target["topics"] if item["name"].casefold() == topic_name.casefold()),
                    None,
                )
                if not topic or topic.get("thread_id") is None:
                    raise RuntimeError(f"Topic is not configured with a thread_id: {topic_name}")
                demo_title = f"ДЕМО — вакансия {topic['name'].casefold()}"
                image_path = generate_vacancy_card(
                    f"demo-{topic['name']}", demo_title, "90 000–120 000 ₽", "Москва", topic["name"]
                )
                result = api_call(
                    "sendPhoto",
                    {
                        "chat_id": target["chat_id"],
                        "message_thread_id": topic["thread_id"],
                        "caption": caption_for_telegram(demo_post(topic["name"])),
                    },
                    files={"photo": image_path},
                )
                print(f"Demo photo-with-caption message {result['message_id']} sent to test topic {topic['name']}.")
        else:
            run()
    except Exception as exc:
        print(f"Не удалось запустить бота: {exc}")
        raise SystemExit(1)

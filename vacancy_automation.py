"""HH vacancy formatting and scheduled publication, guarded behind explicit flags."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", ROOT)).expanduser()
DB_PATH = DATA_DIR / "vacancies.sqlite3"
HH_API = "https://api.hh.ru"
TEMPLATE_PATH = ROOT / "vacancy_template.txt"


class HHAccessBlocked(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean_html(value: str, limit: int | None = None) -> str:
    class TextOnly(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {"br", "p", "div", "li"}:
                self.parts.append("\n")

    parser = TextOnly()
    parser.feed(value or "")
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def contact_lines(vacancy: dict[str, Any]) -> list[str]:
    contacts = vacancy.get("contacts") or {}
    lines: list[str] = []
    name = contacts.get("name")
    if name:
        lines.append(str(name))
    if contacts.get("email"):
        lines.append(str(contacts["email"]))
    for phone in contacts.get("phones") or []:
        formatted = phone.get("formatted")
        if not formatted:
            formatted = "".join(
                str(phone.get(part, ""))
                for part in ("country", "city", "number")
            )
        if formatted:
            comment = phone.get("comment")
            lines.append(str(formatted) + (f" ({comment})" if comment else ""))
    return lines


def has_contact(vacancy: dict[str, Any]) -> bool:
    contacts = vacancy.get("contacts") or {}
    return bool(contacts.get("email") or contacts.get("phones"))


def _salary_text(salary: dict[str, Any] | None) -> str | None:
    if not salary:
        return None
    currency = salary.get("currency") or "RUR"
    currency_name = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    low, high = salary.get("from"), salary.get("to")
    if low is not None and high is not None:
        amount = f"{low:,}–{high:,}".replace(",", " ")
    elif low is not None:
        amount = f"от {low:,}".replace(",", " ")
    elif high is not None:
        amount = f"до {high:,}".replace(",", " ")
    else:
        return None
    parts = [f"{amount} {currency_name}"]
    mode = (salary.get("mode") or {}).get("name")
    frequency = (salary.get("frequency") or {}).get("name")
    if mode:
        parts.append(mode.lower())
    if salary.get("gross") is True:
        parts.append("до вычета налогов")
    elif salary.get("gross") is False:
        parts.append("на руки")
    if frequency:
        parts.append(f"выплаты: {frequency.lower()}")
    return " · ".join(parts)


SECTION_ALIASES = {
    "conditions": {
        "условия", "условия работы", "мы предлагаем", "мы предлагаем вам",
        "что предлагаем", "предлагаем",
    },
    "responsibilities": {
        "обязанности", "должностные обязанности", "ваши задачи",
        "наши задачи", "чем предстоит заниматься", "что нужно делать",
    },
    "requirements": {
        "требования", "наши ожидания", "кого мы ищем", "кого ищем",
        "что важно", "ожидания от кандидата",
    },
    "money": {"деньги", "зарплата", "заработная плата", "оплата", "доход"},
    "schedule": {"график", "график работы", "режим работы", "рабочее время", "смены"},
}


def _split_description(description: str) -> dict[str, list[str]]:
    result = {key: [] for key in (*SECTION_ALIASES.keys(), "other")}
    current = "other"
    for raw_line in _clean_html(description).splitlines():
        line = raw_line.strip()
        if not line:
            if result[current] and result[current][-1] != "":
                result[current].append("")
            continue

        candidate = re.sub(r"^[#>*•▪\-–—\s]+", "", line).strip()
        heading = candidate
        inline_text = ""
        if ":" in candidate:
            possible_heading, inline_text = candidate.split(":", 1)
            if possible_heading.strip().casefold() in {
                alias for aliases in SECTION_ALIASES.values() for alias in aliases
            }:
                heading = possible_heading.strip()
                inline_text = inline_text.strip()
        heading = re.sub(r"[:\s]+$", "", heading).casefold()
        matched = next(
            (key for key, aliases in SECTION_ALIASES.items() if heading in aliases),
            None,
        )
        if matched:
            current = matched
            if inline_text:
                result[current].append(inline_text)
            continue
        result[current].append(line)

    for key in result:
        result[key] = re.sub(r"\n{3,}", "\n\n", "\n".join(result[key])).strip().splitlines()
    return result


def _format_block(heading: str, lines: list[str]) -> str:
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return f"{heading}\n{body}" if body else ""


def _render_template(values: dict[str, str]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8-sig")
    rendered = template.format_map({key: value or "" for key, value in values.items()})
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    return re.sub(r"\n{3,}", "\n\n", rendered).strip()


def _split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    parts: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def format_vacancy(vacancy: dict[str, Any], topic_name: str) -> str:
    title = vacancy.get("name") or "Вакансия"
    employer = (vacancy.get("employer") or {}).get("name")
    area = (vacancy.get("area") or {}).get("name")
    address_object = vacancy.get("address") or {}
    address = address_object.get("raw")
    metro = ((address_object.get("metro") or {}).get("station") or {}).get("name")
    description_sections = _split_description(vacancy.get("description", ""))
    salary = _salary_text(vacancy.get("salary_range") or vacancy.get("salary"))
    contacts = contact_lines(vacancy)
    location = ", ".join(part for part in (area, address, metro) if part)
    pay_lines = [salary] if salary else []
    pay_lines.extend(description_sections["money"])
    schedule_lines: list[str] = list(description_sections["schedule"])
    content_sections = {
        "conditions": list(description_sections["conditions"]),
        "responsibilities": list(description_sections["responsibilities"]),
        "requirements": list(description_sections["requirements"]),
    }
    unlabelled = list(description_sections["other"])
    description_lines: list[str] = []
    pay_pattern = re.compile(r"(?:зарплат|ставк|оплат|выплат|доход|смен[аы]|руб\.?|₽|тыс\.).*\d|\d.*(?:зарплат|ставк|оплат|выплат|доход|смен[аы]|руб\.?|₽|тыс\.)", re.I)
    schedule_pattern = re.compile(r"график|смен|рабоч(?:ее|ие) время|часы работы|выходн|ночн", re.I)

    def concise_fact(line: str, pattern: re.Pattern[str]) -> str:
        if len(line) <= 180:
            return line
        match = pattern.search(line)
        if not match:
            return line[:179].rstrip() + "…"
        start = max(0, match.start() - 55)
        end = min(len(line), match.end() + 95)
        excerpt = line[start:end].strip()
        return ("…" if start else "") + excerpt + ("…" if end < len(line) else "")

    for key, lines in content_sections.items():
        retained: list[str] = []
        for line in lines:
            if pay_pattern.search(line):
                pay_lines.append(concise_fact(line, pay_pattern))
                retained.append(line)
            elif schedule_pattern.search(line):
                schedule_lines.append(concise_fact(line, schedule_pattern))
                retained.append(line)
            else:
                retained.append(line)
        content_sections[key] = retained

    for line in unlabelled:
        if pay_pattern.search(line):
            pay_lines.append(concise_fact(line, pay_pattern))
            description_lines.append(line)
        elif schedule_pattern.search(line):
            schedule_lines.append(concise_fact(line, schedule_pattern))
            description_lines.append(line)
        else:
            description_lines.append(line)

    employment = (
        (vacancy.get("employment_form") or {}).get("name")
        or (vacancy.get("employment") or {}).get("name")
    )
    structured_schedule: list[str] = []
    single_schedule = (vacancy.get("schedule") or {}).get("name")
    if single_schedule:
        structured_schedule.append(single_schedule)
    for field in (
        "work_schedule_by_days", "working_hours", "working_days",
        "working_time_modes", "working_time_intervals", "work_format",
    ):
        structured_schedule.extend(
            item.get("name") for item in (vacancy.get(field) or [])
            if isinstance(item, dict) and item.get("name")
        )
    if not schedule_lines:
        schedule_lines.extend(structured_schedule)
    if employment and not any(employment.casefold() in line.casefold() for line in schedule_lines):
        content_sections["conditions"].insert(0, employment)
    experience = (vacancy.get("experience") or {}).get("name")
    if experience and not any("опыт" in line.casefold() for line in content_sections["requirements"]):
        content_sections["requirements"].insert(0, f"Опыт: {experience}")

    values = {
        "title": f"{title}\n#{topic_name}",
        "money_block": _format_block("#Деньги", list(dict.fromkeys(pay_lines))),
        "schedule_block": _format_block("#График", list(dict.fromkeys(schedule_lines))),
        "employer_block": f"#Заведение\n{employer}" if employer else "",
        "location_block": f"📍 #Район #Метро\n{location}" if location else "",
        "description_block": _format_block("#Описание", description_lines),
        "conditions_block": _format_block("#Условия", content_sections["conditions"]),
        "responsibilities_block": _format_block("#Обязанности", content_sections["responsibilities"]),
        "requirements_block": _format_block("#Требования", content_sections["requirements"]),
        "contacts_block": _format_block("#Контакты", contacts),
        "source_block": (
            f"Источник: Работа России\n{vacancy.get('source_url')}"
            if vacancy.get("source_url")
            else ""
        ),
    }
    return _render_template(values)


class HHClient:
    """Read-only HH API client. It is only called when config gates are enabled."""

    def __init__(self, app_token: str, user_agent: str) -> None:
        if not app_token:
            raise RuntimeError("HH_APP_TOKEN is not set")
        if not user_agent or "@" not in user_agent:
            raise RuntimeError("HH_USER_AGENT must include the app name and contact email")
        self.headers = {
            "Authorization": f"Bearer {app_token}",
            "HH-User-Agent": user_agent,
            "Accept": "application/json",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{HH_API}{path}"
        if params:
            url += "?" + urlencode(params, doseq=True)
        request = Request(url, headers=self.headers)
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Inspect only the error type; never log bodies with vacancy data.
            body = exc.read(8192).decode("utf-8", errors="ignore")
            if exc.code == 403 and "captcha_required" in body:
                raise HHAccessBlocked("captcha_required") from None
            if exc.code in {401, 403}:
                raise HHAccessBlocked(f"http_{exc.code}") from None
            raise RuntimeError(f"HH API returned HTTP {exc.code}") from None
        except URLError as exc:
            raise RuntimeError(f"HH API connection failed: {exc.reason}") from None

    def search_topic(self, topic: dict[str, Any], area_id: str, period_days: int = 7) -> list[dict[str, Any]]:
        ids: list[str] = []
        seen: set[str] = set()
        queries = topic.get("search_queries") or [topic["name"]]
        for query in queries:
            result = self.get(
                "/vacancies",
                {
                    "area": area_id,
                    "text": query,
                    "search_field": "name",
                    "period": period_days,
                    "per_page": 20,
                    "order_by": "publication_time",
                },
            )
            for item in result.get("items", []):
                vacancy_id = str(item.get("id", ""))
                if vacancy_id and vacancy_id not in seen:
                    seen.add(vacancy_id)
                    ids.append(vacancy_id)
            time.sleep(0.6)

        vacancies: list[dict[str, Any]] = []
        for vacancy_id in ids[:12]:
            vacancy = self.get(f"/vacancies/{vacancy_id}")
            if not vacancy.get("archived", False) and has_contact(vacancy):
                vacancies.append(vacancy)
            time.sleep(0.6)
        return vacancies


class VacancyStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS vacancies (
                    vacancy_id TEXT PRIMARY KEY,
                    topic_name TEXT NOT NULL,
                    post_text TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_posted_at TEXT,
                    post_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS schedule_slots (
                    slot_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def save_candidate(self, vacancy: dict[str, Any], topic_name: str) -> None:
        vacancy_id = str(vacancy["id"])
        text = format_vacancy(vacancy, topic_name)
        link = vacancy.get("alternate_url") or vacancy.get("url") or ""
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                """INSERT INTO vacancies
                   (vacancy_id, topic_name, post_text, source_url, first_seen_at, active)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(vacancy_id) DO UPDATE SET
                     post_text=excluded.post_text,
                     source_url=excluded.source_url,
                     active=1
                   WHERE vacancies.topic_name=excluded.topic_name""",
                (vacancy_id, topic_name, text, link, now),
            )

    def candidate_for_topic(self, topic_name: str, repeat_after_days: int) -> sqlite3.Row | None:
        with self.connect() as db:
            fresh_cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
            row = db.execute(
                "SELECT * FROM vacancies WHERE active=1 AND last_posted_at IS NULL AND topic_name=? AND first_seen_at>=? ORDER BY first_seen_at DESC LIMIT 1",
                (topic_name, fresh_cutoff),
            ).fetchone()
            if row:
                return row
            cutoff = (datetime.now() - timedelta(days=repeat_after_days)).isoformat(timespec="seconds")
            return db.execute(
                "SELECT * FROM vacancies WHERE active=1 AND topic_name=? AND last_posted_at IS NOT NULL AND last_posted_at<=? ORDER BY last_posted_at ASC LIMIT 1",
                (topic_name, cutoff),
            ).fetchone()

    def mark_posted(self, vacancy_id: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "UPDATE vacancies SET last_posted_at=?, post_count=post_count+1 WHERE vacancy_id=?",
                (now, vacancy_id),
            )

    def mark_inactive(self, vacancy_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE vacancies SET active=0 WHERE vacancy_id=?", (vacancy_id,))

    def slot_status(self, slot_key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM schedule_slots WHERE slot_key=?", (slot_key,)).fetchone()
            return row["status"] if row else None

    def posted_today(self, today: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS amount FROM vacancies WHERE last_posted_at LIKE ?",
                (today + "%",),
            ).fetchone()
            return int(row["amount"] or 0)

    def posted_by_topic_today(self, topic_name: str, today: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS amount FROM vacancies WHERE topic_name=? AND last_posted_at LIKE ?",
                (topic_name, today + "%"),
            ).fetchone()
            return int(row["amount"] or 0)

    def mark_slot(self, slot_key: str, status: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "INSERT INTO schedule_slots(slot_key,status,updated_at) VALUES(?,?,?) ON CONFLICT(slot_key) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",
                (slot_key, status, now),
            )


def find_due_slot(config: dict[str, Any], now: datetime | None = None) -> tuple[str, datetime] | None:
    tz = ZoneInfo(config.get("timezone", "Europe/Moscow"))
    current = now.astimezone(tz) if now else datetime.now(tz)
    grace = int(config.get("schedule_grace_minutes", 20))
    for value in config.get("schedule_times", []):
        hour, minute = (int(part) for part in value.split(":"))
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled <= current <= scheduled + timedelta(minutes=grace):
            return f"{current.date().isoformat()}T{value}", scheduled
    return None


def scheduler_tick(
    config: dict[str, Any],
    telegram_api: Callable[[str, dict[str, Any]], Any],
    now: datetime | None = None,
) -> str | None:
    """Run one due publication slot. Disabled by default until HH approval/terms are clear."""
    if not config.get("automation_enabled") or not config.get("hh_source_enabled"):
        return None
    if not config.get("hh_redistribution_confirmed"):
        logging.warning("HH publishing gate is closed: redistribution has not been confirmed")
        return None
    due = find_due_slot(config, now)
    if not due:
        return None
    slot_key, _ = due
    store = VacancyStore()

    target = config["target"]
    topics = [topic for topic in target.get("topics", []) if topic.get("thread_id") is not None and topic.get("search_queries")]
    if not topics:
        logging.error("No configured searchable topics")
        return None

    terminal = {
        "sent", "no_candidate", "sending", "uncertain", "captcha_required", "blocked", "daily_limit", "error"
    }
    today = datetime.now().date().isoformat()
    outcomes: list[str] = []
    source: HHClient | None = None
    active_slot_key = ""

    try:
        source = HHClient(os.environ.get("HH_APP_TOKEN", ""), os.environ.get("HH_USER_AGENT", ""))
        for topic in topics:
            active_slot_key = f"{slot_key}|topic={topic['thread_id']}"
            if store.slot_status(active_slot_key) in terminal:
                continue

            if store.posted_by_topic_today(topic["name"], today) >= int(
                config.get("max_posts_per_topic_per_day", 3)
            ):
                store.mark_slot(active_slot_key, "daily_limit")
                outcomes.append("daily_limit")
                continue

            for vacancy in source.search_topic(topic, str(config.get("area_id", "1"))):
                store.save_candidate(vacancy, topic["name"])

            row = None
            post_text = ""
            for _ in range(20):
                candidate = store.candidate_for_topic(
                    topic["name"], int(config.get("repeat_after_days", 7))
                )
                if not candidate:
                    break
                post_text = candidate["post_text"]
                if candidate["last_posted_at"] is not None:
                    current = source.get(f"/vacancies/{candidate['vacancy_id']}")
                    if current.get("archived", False) or not has_contact(current):
                        store.mark_inactive(candidate["vacancy_id"])
                        continue
                    post_text = format_vacancy(current, topic["name"])
                    store.save_candidate(current, topic["name"])
                row = candidate
                break

            if not row:
                store.mark_slot(active_slot_key, "no_candidate")
                outcomes.append("no_candidate")
                logging.info("No eligible HH vacancy with contact details for topic %s", topic["name"])
                continue

            store.mark_slot(active_slot_key, "sending")
            for text_part in _split_telegram_text(post_text):
                telegram_api("sendMessage", {
                    "chat_id": target["chat_id"],
                    "message_thread_id": topic["thread_id"],
                    "text": text_part,
                    "disable_web_page_preview": True,
                })
            store.mark_posted(row["vacancy_id"])
            store.mark_slot(active_slot_key, "sent")
            outcomes.append("sent")
            logging.info("Published vacancy %s to topic %s", row["vacancy_id"], topic["name"])

        if "sent" in outcomes:
            return "sent"
        if "error" in outcomes:
            return "error"
        if outcomes:
            return outcomes[-1]
        return None
    except HHAccessBlocked as exc:
        status = "captcha_required" if exc.reason == "captcha_required" else "blocked"
        if active_slot_key:
            store.mark_slot(active_slot_key, status)
        logging.warning("HH access blocked for slot %s (%s); waiting for owner action", slot_key, exc.reason)
        return status
    except Exception:
        # Do not include vacancy bodies, contact data, or credentials in logs.
        logging.exception("Scheduled publication failed for slot %s", slot_key)
        if active_slot_key:
            if store.slot_status(active_slot_key) == "sending":
                store.mark_slot(active_slot_key, "uncertain")
            else:
                store.mark_slot(active_slot_key, "error")
        return "error"


def demo_post(topic_name: str) -> str:
    sample = {
        "name": f"🧪 ДЕМО — вакансия для проверки темы «{topic_name}»",
        "employer": {"name": "Тестовый работодатель"},
        "area": {"name": "Москва"},
        "salary": {"from": 90000, "to": 120000, "currency": "RUR", "gross": False},
        "employment_form": {"name": "Полная занятость"},
        "experience": {"name": "Не требуется"},
        "published_at": "2026-09-26T10:00:00+03:00",
        "work_schedule_by_days": [{"name": "5/2"}],
        "working_hours": [{"name": "12 часов"}],
        "work_format": [{"name": "На месте работодателя"}],
        "contacts": {"name": "Демо-контакт", "email": "demo@example.com"},
        "description": (
            "Условия: Это демонстрационный текст.\n"
            "Обязанности: Проверка форматирования.\n"
            "Требования: Реальная вакансия не используется."
        ),
    }
    return format_vacancy(sample, topic_name)

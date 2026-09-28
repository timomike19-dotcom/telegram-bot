"""Local, token-free publisher for public Work Russia vacancy data.

The Work Russia open-data API is polled from this Windows computer. New and
currently active matching vacancies are stored in SQLite before delivery, so
temporary network/computer outages do not discard the queue.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from vacancy_automation import _clean_html, format_vacancy


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", ROOT)).expanduser()
DB_PATH = DATA_DIR / "vacancies.sqlite3"
IMAGES_DIR = DATA_DIR / "data" / "vacancy_images"
API_ROOT = "http://opendata.trudvsem.ru/api/v1/vacancies"
PORTAL_ROOT = "https://trudvsem.ru"
MOSCOW_REGION_CODE = "7700000000000"
IMAGE_SIZE = (1080, 1350)


class WorkRussiaAPIError(RuntimeError):
    pass


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9а-я]", "", str(value).casefold())


def _field(data: Any, *names: str) -> Any:
    if not isinstance(data, dict):
        return None
    wanted = {_key(name) for name in names}
    for name, value in data.items():
        if _key(name) in wanted and value not in (None, ""):
            return value
    return None


def _plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for name in ("name", "value", "text", "title", "label"):
            found = _field(value, name)
            if found not in (None, ""):
                return _plain(found)
        return ""
    if isinstance(value, list):
        return ", ".join(part for item in value if (part := _plain(item)))
    return _clean_html(str(value)).strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _money(value: Any, vacancy: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        lower = _field(value, "from", "min", "minimum", "salary_min", "salary_from")
        upper = _field(value, "to", "max", "maximum", "salary_max", "salary_to")
        if lower is None and upper is None:
            return None
        return {"from": lower, "to": upper, "currency": "RUR", "gross": None}
    if isinstance(value, (int, float)):
        lower = _field(vacancy, "salary_min", "salary_from", "min_salary", "salarymin")
        upper = _field(vacancy, "salary_max", "salary_to", "max_salary", "salarymax")
        return {"from": lower if lower is not None else value, "to": upper, "currency": "RUR", "gross": None}
    raw = _plain(value)
    lower = _field(vacancy, "salary_min", "salary_from", "min_salary", "salarymin")
    upper = _field(vacancy, "salary_max", "salary_to", "max_salary", "salarymax")
    if lower is not None or upper is not None:
        return {"from": lower, "to": upper, "currency": "RUR", "gross": None}
    if raw:
        digits = re.findall(r"\d[\d\s.,]*", raw)
        amounts: list[int] = []
        for digit in digits[:2]:
            parsed = re.sub(r"\D", "", digit)
            if parsed:
                amounts.append(int(parsed))
        if amounts:
            return {
                "from": amounts[0],
                "to": amounts[1] if len(amounts) > 1 else None,
                "currency": "RUR",
                "gross": None,
            }
        return None
    return None


def _source_link(raw: dict[str, Any], vacancy_id: str) -> str:
    for name in ("vacancy_url", "vac_url", "url", "href", "uri", "vacancy_link"):
        candidate = _plain(_field(raw, name))
        if not candidate:
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme == "https" and parsed.hostname in {"trudvsem.ru", "www.trudvsem.ru"}:
            return candidate
    company = _as_dict(_field(raw, "company", "employer", "organization"))
    company_code = _plain(_field(company, "companycode", "company_code", "code", "id"))
    if company_code and vacancy_id and ":" in vacancy_id:
        vacancy_part = vacancy_id.rsplit(":", 1)[-1]
        return f"{PORTAL_ROOT}/vacancy/card/{company_code}/{vacancy_part}"
    return f"{PORTAL_ROOT}/opendata"


def normalize_vacancy(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert the open-data record into the existing approved post format."""
    record = _as_dict(_field(raw, "vacancy")) or raw
    company = _as_dict(_field(record, "company", "employer", "organization"))
    region = _as_dict(_field(record, "region", "area", "location"))
    address = _as_dict(_field(record, "address", "job_address", "work_address"))
    address_list = _field(_as_dict(_field(record, "addresses")), "address")
    contacts_obj = _as_dict(_field(record, "contacts", "contact", "contact_info"))

    title = _plain(_field(record, "job-name", "jobname", "vacancy_name", "profession", "title", "name"))
    if not title:
        return None
    raw_id = _plain(_field(record, "id", "vacancy_id", "vacancyid", "uuid", "guid", "vacancy_code"))
    company_code = _plain(_field(company, "companycode", "company_code", "code", "id"))
    if not raw_id:
        raw_id = _plain(_field(record, "uri", "url", "vacancy_url", "vac_url"))
    if not raw_id:
        stable = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        raw_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
    vacancy_id = f"{company_code}:{raw_id}" if company_code and ":" not in raw_id else raw_id

    employer_name = _plain(_field(company, "name", "company_name", "companyname", "title"))
    if not employer_name:
        employer_name = _plain(_field(record, "company_name", "employer_name", "organization_name"))
    location = _plain(_field(region, "name", "region_name", "title")) or _plain(
        _field(record, "region_name", "area_name", "region")
    )
    address_text = _plain(_field(address, "raw", "name", "address", "text")) or _plain(
        _field(record, "job_address", "address_text", "address")
    )
    if not address_text:
        address_items = address_list if isinstance(address_list, list) else [address_list]
        address_text = ", ".join(
            text for item in address_items
            if (text := _plain(_field(_as_dict(item), "location", "raw", "name", "address", "text")))
        )

    salary_value = _field(record, "salary", "salary_range", "salary_text", "salary_min")
    salary = _money(salary_value, record)
    if isinstance(salary_value, str) and salary is None:
        salary = None
    # Work Russia exposes duties as a separate field; keeping it separate avoids
    # a long duty paragraph containing a salary mention being mistaken for pay.
    description = _plain(_field(record, "description", "full_description", "vacancy_description"))
    extra_sections: list[str] = []
    for label, aliases in (
        ("Обязанности", ("responsibilities", "duties", "duty")),
        ("Требования", ("requirements", "requirement", "education", "required_education")),
        ("Условия", ("conditions", "working_conditions", "work_conditions")),
    ):
        text = _plain(_field(record, *aliases))
        if text and text.casefold() not in description.casefold():
            extra_sections.append(f"{label}: {text}")
    if extra_sections:
        description = "\n\n".join(part for part in (description, *extra_sections) if part)

    person = _plain(_field(contacts_obj, "name", "person", "contact_person", "contact_name")) or _plain(
        _field(record, "contact_person", "contact_name", "contactperson")
    )
    email = (
        _plain(_field(contacts_obj, "email"))
        or _plain(_field(record, "email", "contact_email"))
    )
    phone_value = _field(contacts_obj, "phones", "phone", "telephone") or _field(
        record, "contact_phone", "phone", "telephone", "phone_number"
    )
    phones: list[dict[str, str]] = []
    if isinstance(phone_value, list):
        for item in phone_value:
            phone_text = _plain(_field(_as_dict(item), "formatted", "number", "phone")) or _plain(item)
            if phone_text:
                phones.append({"formatted": phone_text})
    elif phone_value:
        phones.append({"formatted": _plain(phone_value)})

    contact_items = _field(record, "contact_list")
    if isinstance(contact_items, list):
        for item in contact_items:
            contact_type = _plain(_field(_as_dict(item), "contact_type", "type", "name")).casefold()
            contact_value = _plain(_field(_as_dict(item), "contact_value", "value", "text"))
            if not contact_value:
                continue
            if "почт" in contact_type or "email" in contact_type or "mail" in contact_type or "@" in contact_value:
                if not email:
                    email = contact_value
            elif "тел" in contact_type or "phone" in contact_type or re.search(r"\+?\d[\d()\s-]{5,}", contact_value):
                if not any(phone.get("formatted") == contact_value for phone in phones):
                    phones.append({"formatted": contact_value})
    if not email:
        email = _plain(_field(company, "email"))
    contacts = {"name": person, "email": email, "phones": phones}

    published = _plain(_field(record, "creation-date", "creation_date", "published_at", "publish_date"))
    schedule = _plain(_field(record, "schedule", "work_schedule", "working_schedule", "schedule_name"))
    employment = _plain(_field(record, "employment", "employment_form", "job_type", "employment_type"))
    experience = _plain(_field(record, "experience", "experience_name", "work_experience"))
    source_url = _source_link(record, vacancy_id)

    # The open-data API is the source of record. Respect explicit inactive flags,
    # but do not infer closure from absent optional fields.
    active_value = _field(record, "active", "is_active", "isactual", "actual")
    archived_value = _field(record, "archived", "is_archived", "deleted", "is_deleted")
    status = _plain(_field(record, "status", "vacancy_status", "state")).casefold()
    active = active_value is not False and str(active_value).casefold() not in {"false", "0", "нет"}
    active = active and archived_value is not True and str(archived_value).casefold() not in {"true", "1", "да"}
    if status and any(word in status for word in ("архив", "закрыт", "снят", "неактив", "inactive", "closed")):
        active = False
    end_date = _plain(_field(record, "end_date", "date_end", "expiration_date", "vacancy_end_date"))
    if end_date:
        try:
            expiry = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            active = active and expiry > datetime.now(timezone.utc)
        except ValueError:
            pass

    return {
        "id": vacancy_id,
        "name": title,
        "employer": {"name": employer_name},
        "area": {"name": location},
        "address": {"raw": address_text},
        "description": description,
        "salary": salary,
        "contacts": contacts,
        "experience": {"name": experience} if experience else None,
        "schedule": {"name": schedule} if schedule else None,
        "employment_form": {"name": employment} if employment else None,
        "published_at": published,
        "source_url": source_url,
        "active": active,
        "raw": record,
    }


def topic_for_title(title: str, topics: Iterable[dict[str, Any]]) -> str | None:
    normalized = re.sub(r"[ё]", "е", title.casefold())
    for topic in topics:
        name = str(topic.get("name", ""))
        if name.casefold() == "повар" and re.search(r"\bповар\w*\b|су[ -]?шеф|шеф[ -]?повар", normalized):
            return name
        if name.casefold() == "кондитер" and re.search(r"\bкондитер\w*\b|пекарь[ -]?кондитер", normalized):
            return name
    return None


def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results") or {}
    if isinstance(results, list):
        items: Any = results
    elif isinstance(results, dict):
        items = _field(results, "vacancies", "items", "records")
        if items is None:
            items = next((value for value in results.values() if isinstance(value, list)), [])
    else:
        items = []
    if not isinstance(items, list):
        return []
    output: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = _field(item, "vacancy")
        output.append(nested if isinstance(nested, dict) else item)
    return output


def _telegram_length(text: str) -> int:
    """Count UTF-16 units conservatively for Telegram's caption limit."""
    return len(text.encode("utf-16-le")) // 2


def _take_telegram_units(text: str, budget: int) -> str:
    taken: list[str] = []
    used = 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > budget:
            break
        taken.append(char)
        used += units
    return "".join(taken)


def _clip_block(block: str, budget: int) -> str:
    """Clip one formatted section while retaining its label and an ellipsis."""
    block = block.strip()
    if _telegram_length(block) <= budget:
        return block
    lines = block.splitlines()
    header = lines[0] if len(lines) > 1 else ""
    body = "\n".join(lines[1:]).strip() if header else block
    suffix = "…"
    if header:
        header_units = _telegram_length(header) + 1
        body_budget = budget - header_units
        if body_budget <= _telegram_length(suffix):
            return _take_telegram_units(header, max(0, budget - 1)).rstrip() + suffix
        clipped = _take_telegram_units(body, body_budget - 1).rstrip()
        return f"{header}\n{clipped}{suffix}"
    return _take_telegram_units(block, max(0, budget - 1)).rstrip() + suffix


def caption_for_telegram(post_text: str, limit: int = 1024) -> str:
    """Keep the approved post layout inside Telegram's single-photo caption cap."""
    post_text = post_text.strip()
    if _telegram_length(post_text) <= limit:
        return post_text

    blocks = [block.strip() for block in re.split(r"\n\s*\n", post_text) if block.strip()]
    optional_headers = ("#описание", "#условия", "#обязанности", "#требования")
    optional = [i for i, block in enumerate(blocks) if block.casefold().startswith(optional_headers)]
    mandatory = [i for i in range(len(blocks)) if i not in optional]

    def priority(index: int) -> tuple[int, int]:
        block = blocks[index].casefold()
        if block.startswith("#вакансия"):
            rank = 0
        elif block.startswith("#деньги"):
            rank = 1
        elif block.startswith("#контакты"):
            rank = 2
        elif block.startswith("источник:"):
            rank = 3
        elif block.startswith("#график"):
            rank = 4
        elif block.startswith("📍"):
            rank = 5
        elif block.startswith("#заведение"):
            rank = 6
        elif block.startswith("📝"):
            rank = 7
        else:
            rank = 8
        return rank, index

    selected: dict[int, str] = {i: blocks[i] for i in mandatory}

    def used_units() -> int:
        return sum(_telegram_length(value) for value in selected.values()) + 2 * max(0, len(selected) - 1)

    # Mandatory blocks (especially the contact and source links) get space
    # before long description sections. Only compact secondary fields if needed.
    excess = used_units() - limit
    for index in sorted(mandatory, key=priority, reverse=True):
        if excess <= 0:
            break
        block = selected[index]
        folded = block.casefold()
        if folded.startswith("#контакты") or folded.startswith("источник:") or folded.startswith("#вакансия"):
            continue
        if folded.startswith("📝"):
            selected.pop(index)
            excess = used_units() - limit
            continue
        lines = block.splitlines()
        minimum = _telegram_length(lines[0]) + (2 if len(lines) > 1 else 1)
        reducible = max(0, _telegram_length(block) - minimum)
        if reducible:
            selected[index] = _clip_block(block, _telegram_length(block) - min(excess, reducible))
            excess = used_units() - limit

    # Exceptional records can contain unusually long pay/contact fields. Keep
    # one complete contact and the full source URL even in that case.
    if used_units() > limit:
        for index in mandatory:
            if index not in selected:
                continue
            block = selected[index]
            if block.casefold().startswith("#контакты"):
                lines = block.splitlines()
                values = lines[1:]
                contact = next(
                    (line for line in values if "@" in line or re.search(r"\+?\d[\d()\s-]{5,}\d", line)),
                    values[0] if values else "",
                )
                selected[index] = "\n".join(part for part in (lines[0], contact) if part)
            elif block.casefold().startswith("#деньги"):
                lines = block.splitlines()
                selected[index] = "\n".join(lines[:2])
        excess = used_units() - limit
        for index in sorted(mandatory, key=priority, reverse=True):
            if excess <= 0:
                break
            if index not in selected or selected[index].casefold().startswith(("источник:", "#контакты", "#вакансия")):
                continue
            block = selected[index]
            if block.casefold().startswith("📝"):
                selected.pop(index)
                excess = used_units() - limit
                continue
            lines = block.splitlines()
            minimum = _telegram_length(lines[0]) + 1
            selected[index] = _clip_block(block, max(minimum, _telegram_length(block) - excess))
            excess = used_units() - limit

    remaining = limit - used_units()
    for position, index in enumerate(optional):
        sections_left = len(optional) - position
        if remaining <= 0:
            break
        # Share space across conditions, duties and requirements so one long
        # field cannot crowd out the other details.
        slot = max(0, (remaining - 2 * (sections_left - 1)) // sections_left - 2)
        block = blocks[index]
        minimum = _telegram_length(block.splitlines()[0]) + 2
        if slot < minimum:
            continue
        clipped = _clip_block(block, slot)
        selected[index] = clipped
        remaining -= _telegram_length(clipped) + 2

    result = "\n\n".join(selected[index] for index in sorted(selected))
    if _telegram_length(result) > limit:
        # Only reached for pathological mandatory fields. Retain the existing
        # source/contact blocks and trim the other text to the remaining budget.
        protected = [i for i in sorted(selected) if selected[i].casefold().startswith(("#контакты", "источник:"))]
        other = [i for i in sorted(selected) if i not in protected]
        output = {i: selected[i] for i in protected}
        available = limit - sum(_telegram_length(x) for x in output.values()) - 2 * max(0, len(output) - 1)
        for index in other:
            if available <= 2:
                break
            clipped = _clip_block(selected[index], available - 2)
            output[index] = clipped
            available -= _telegram_length(clipped) + 2
        result = "\n\n".join(output[i] for i in sorted(output))
    return result


class WorkRussiaClient:
    """Unauthenticated client for the published Work Russia open-data API."""

    def __init__(self, api_root: str = API_ROOT, region_code: str = MOSCOW_REGION_CODE) -> None:
        self.api_root = api_root.rstrip("/")
        self.region_code = str(region_code)
        self.last_scan_truncated = False

    def get_page(
        self,
        query: str,
        offset: int,
        limit: int,
        modified_from: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {"text": query, "offset": offset, "limit": min(limit, 100)}
        if modified_from:
            params["modifiedFrom"] = modified_from
        url = f"{self.api_root}/region/{self.region_code}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "FoodJobsTelegramBot/1.0"})
        try:
            with urlopen(request, timeout=40) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise WorkRussiaAPIError(f"Работа России API ответил HTTP {exc.code}") from None
        except (URLError, TimeoutError) as exc:
            raise WorkRussiaAPIError(f"Не удалось подключиться к API Работа России ({type(exc).__name__})") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WorkRussiaAPIError("API Работа России вернул некорректный JSON") from None

        if str(payload.get("status", "200")) != "200":
            error = _plain(_field(payload.get("meta"), "error")) or "неизвестная ошибка"
            raise WorkRussiaAPIError(f"API Работа России: {error[:180]}")
        try:
            total = int(_field(payload.get("meta") or {}, "total") or 0)
        except (TypeError, ValueError):
            total = 0
        return _extract_items(payload), total

    def search(
        self,
        query: str,
        modified_from: str | None = None,
        page_size: int = 100,
        hard_cap: int = 10_000,
    ) -> Iterable[dict[str, Any]]:
        self.last_scan_truncated = False
        # The live API treats offset as a zero-based page number: offset=0
        # returns page one and offset=1 returns page two.
        offset = 0
        count = 0
        total: int | None = None
        while count < hard_cap and (total is None or offset <= min(total, hard_cap)):
            records, total_now = self.get_page(query, offset, page_size, modified_from)
            if total is None:
                total = total_now
                if total > hard_cap:
                    self.last_scan_truncated = True
                    logging.warning("Work Russia query %s exceeds the documented 10,000-record page range", query)
            if not records:
                break
            for record in records:
                yield record
            count += len(records)
            offset += 1
            if len(records) < min(page_size, 100):
                break
            time.sleep(0.15)


class WorkRussiaStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS work_vacancies (
                    vacancy_id TEXT PRIMARY KEY,
                    topic_name TEXT NOT NULL,
                    post_text TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    salary_text TEXT NOT NULL DEFAULT '',
                    location_text TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_posted_at TEXT,
                    post_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    text_part_index INTEGER NOT NULL DEFAULT 0,
                    photo_message_id INTEGER,
                    text_message_id INTEGER,
                    image_path TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_after TEXT,
                    repeat_post INTEGER NOT NULL DEFAULT 0,
                    last_seen_scan TEXT
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_work_queue ON work_vacancies(delivery_status, active, first_seen_at)")
            # Old local builds could leave a photo-only step waiting for its
            # separate text message. Re-queue it so the new combined message is sent.
            db.execute("UPDATE work_vacancies SET delivery_status='pending' WHERE delivery_status='photo_sent'")
            db.execute(
                """CREATE TABLE IF NOT EXISTS work_sync_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def meta(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM work_sync_metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO work_sync_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def queued_counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT topic_name,COUNT(*) AS amount FROM work_vacancies WHERE active=1 AND delivery_status IN ('pending','sending_photo') GROUP BY topic_name"
            ).fetchall()
        return {str(row["topic_name"]): int(row["amount"]) for row in rows}

    def enqueue(self, vacancy: dict[str, Any], topic_name: str, scan_id: str | None = None) -> bool:
        vacancy_id = str(vacancy["id"])
        post_text = format_vacancy(vacancy, topic_name)
        source_url = str(vacancy.get("source_url") or f"{PORTAL_ROOT}/opendata")
        salary_value = vacancy.get("salary") or {}
        if isinstance(salary_value, dict):
            salary_text = ""
            low, high = salary_value.get("from"), salary_value.get("to")
            if low is not None and high is not None:
                salary_text = f"{low}–{high} ₽"
            elif low is not None:
                salary_text = f"от {low} ₽"
            elif high is not None:
                salary_text = f"до {high} ₽"
        else:
            salary_text = _plain(salary_value)
        location_text = _plain(vacancy.get("area"))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as db:
            old = db.execute("SELECT delivery_status FROM work_vacancies WHERE vacancy_id=?", (vacancy_id,)).fetchone()
            db.execute(
                """INSERT INTO work_vacancies
                   (vacancy_id,topic_name,post_text,source_url,title,salary_text,location_text,published_at,first_seen_at,last_seen_scan,active)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(vacancy_id) DO UPDATE SET
                     topic_name=excluded.topic_name,
                     post_text=CASE WHEN work_vacancies.delivery_status IN ('pending','failed') THEN excluded.post_text ELSE work_vacancies.post_text END,
                     source_url=excluded.source_url,
                     title=excluded.title,
                     salary_text=excluded.salary_text,
                     location_text=excluded.location_text,
                     published_at=excluded.published_at,
                     last_seen_scan=COALESCE(excluded.last_seen_scan,work_vacancies.last_seen_scan),
                     active=1,
                     delivery_status=CASE WHEN work_vacancies.delivery_status='inactive' THEN 'pending' ELSE work_vacancies.delivery_status END""",
                (
                    vacancy_id, topic_name, post_text, source_url,
                    str(vacancy.get("name", "Вакансия")), salary_text, location_text,
                    str(vacancy.get("published_at") or ""), now, scan_id,
                ),
            )
        return old is None

    def mark_inactive(self, vacancy_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE work_vacancies SET active=0, delivery_status=CASE WHEN delivery_status='pending' THEN 'inactive' ELSE delivery_status END WHERE vacancy_id=?",
                (vacancy_id,),
            )

    def mark_missing_from_full_scan(self, topic_name: str, scan_id: str) -> int:
        with self.connect() as db:
            cur = db.execute(
                "UPDATE work_vacancies SET active=0, delivery_status=CASE WHEN delivery_status='pending' THEN 'inactive' ELSE delivery_status END WHERE topic_name=? AND COALESCE(last_seen_scan,'')<>? AND delivery_status<>'sent'",
                (topic_name, scan_id),
            )
            return cur.rowcount

    def next_pending(self, now: datetime) -> sqlite3.Row | None:
        stamp = now.isoformat(timespec="seconds")
        with self.connect() as db:
            return db.execute(
                """SELECT * FROM work_vacancies
                   WHERE active=1 AND delivery_status='pending'
                     AND (retry_after IS NULL OR retry_after<=?)
                   ORDER BY repeat_post ASC,
                            CASE WHEN published_at='' THEN 1 ELSE 0 END,
                            published_at DESC, first_seen_at ASC LIMIT 1""",
                (stamp,),
            ).fetchone()

    def next_repeat(self, now: datetime, topics: list[dict[str, Any]], repeat_after_days: int) -> sqlite3.Row | None:
        cutoff = (now - timedelta(days=repeat_after_days)).isoformat(timespec="seconds")
        for topic in topics:
            topic_name = str(topic["name"])
            key = f"last_repeat:{topic_name}"
            last_repeat = self.meta(key)
            if last_repeat:
                try:
                    if datetime.fromisoformat(last_repeat) > now - timedelta(days=repeat_after_days):
                        continue
                except ValueError:
                    pass
            with self.connect() as db:
                row = db.execute(
                    "SELECT * FROM work_vacancies WHERE topic_name=? AND active=1 AND delivery_status='sent' AND last_posted_at<=? ORDER BY last_posted_at ASC LIMIT 1",
                    (topic_name, cutoff),
                ).fetchone()
            if row:
                with self.connect() as db:
                    db.execute(
                        "UPDATE work_vacancies SET delivery_status='pending', text_part_index=0, photo_message_id=NULL, text_message_id=NULL, repeat_post=1 WHERE vacancy_id=?",
                        (row["vacancy_id"],),
                    )
                self.set_meta(key, now.isoformat(timespec="seconds"))
                with self.connect() as db:
                    return db.execute("SELECT * FROM work_vacancies WHERE vacancy_id=?", (row["vacancy_id"],)).fetchone()
        return None

    def save_image(self, vacancy_id: str, image_path: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE work_vacancies SET image_path=? WHERE vacancy_id=?", (image_path, vacancy_id))

    def mark_message_sent(self, vacancy_id: str, message_id: int | None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "UPDATE work_vacancies SET delivery_status='sent', photo_message_id=?, text_message_id=NULL, last_posted_at=?, post_count=post_count+1, attempts=attempts+1, retry_after=NULL, repeat_post=0 WHERE vacancy_id=?",
                (message_id, now, vacancy_id),
            )

    def defer(self, vacancy_id: str, retry_after: datetime, error: str) -> None:
        safe_error = re.sub(r"[\r\n]+", " ", error)[:180]
        with self.connect() as db:
            db.execute(
                "UPDATE work_vacancies SET attempts=attempts+1, retry_after=?, delivery_status=CASE WHEN delivery_status='sending_photo' THEN 'pending' ELSE delivery_status END WHERE vacancy_id=?",
                (retry_after.isoformat(timespec="seconds"), vacancy_id),
            )
        logging.warning("Vacancy %s delivery deferred (%s)", vacancy_id, safe_error)

    def start_photo(self, vacancy_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE work_vacancies SET delivery_status='sending_photo' WHERE vacancy_id=?", (vacancy_id,))

    def reset_interrupted_photo(self) -> int:
        with self.connect() as db:
            cur = db.execute("UPDATE work_vacancies SET delivery_status='pending' WHERE delivery_status='sending_photo'")
            return cur.rowcount


def _format_values(vacancy: dict[str, Any], key: str, limit: int = 42) -> list[str]:
    words = str(vacancy.get(key, "")).split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + (1 if current else 0) <= limit:
            current = f"{current} {word}".strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_vacancy_card(vacancy_id: str, title: str, salary: str, location: str, topic_name: str) -> Path:
    """Create a per-vacancy original graphic card, without remote assets or tokens."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(vacancy_id.encode("utf-8")).hexdigest()[:24]
    output = IMAGES_DIR / f"{digest}.png"
    if output.exists() and output.stat().st_size > 10_000:
        return output

    width, height = IMAGE_SIZE
    is_cook = topic_name.casefold() == "повар"
    palettes = [
        ("#143D38", "#D7A852", "#F4EFE5"),
        ("#282D45", "#E8A66B", "#F6F0E8"),
        ("#334637", "#E3BD6B", "#F4F1E9"),
        ("#2D3B4F", "#E2A676", "#F5EFE9"),
    ]
    index = int(digest[:2], 16) % len(palettes)
    dark, accent, paper = palettes[index]
    image = Image.new("RGB", IMAGE_SIZE, paper)
    draw = ImageDraw.Draw(image)

    def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("segoeuib.ttf" if bold else "segoeui.ttf"),
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("arialbd.ttf" if bold else "arial.ttf"),
        ]
        for path in candidates:
            try:
                if path.exists():
                    return ImageFont.truetype(str(path), size)
            except OSError:
                continue
        return ImageFont.load_default()

    f_label = font(30, True)
    f_title = font(73, True)
    f_small = font(34, False)
    f_salary = font(56, True)
    f_brand = font(25, True)

    draw.rectangle((0, 0, width, 830), fill=dark)
    draw.ellipse((760, -180, 1280, 340), fill=accent)
    draw.ellipse((790, 520, 1160, 890), fill="#FFFFFF")
    draw.ellipse((825, 555, 1125, 855), fill=paper)

    # Simple custom chef/cupcake line art, drawn in the poster itself.
    cx, cy = 975, 525
    if is_cook:
        ink = dark
        draw.ellipse((cx - 104, cy - 120, cx + 104, cy + 88), fill=accent)
        draw.rounded_rectangle((cx - 122, cy - 4, cx + 122, cy + 125), radius=36, fill=ink)
        draw.ellipse((cx - 94, cy - 195, cx - 8, cy - 105), fill="#FFFFFF")
        draw.ellipse((cx - 42, cy - 233, cx + 55, cy - 120), fill="#FFFFFF")
        draw.ellipse((cx + 18, cy - 191, cx + 98, cy - 110), fill="#FFFFFF")
        draw.rounded_rectangle((cx - 115, cy - 135, cx + 115, cy - 77), radius=28, fill="#FFFFFF")
        draw.line((cx - 56, cy + 28, cx + 55, cy + 28), fill=accent, width=10)
    else:
        ink = dark
        # Cake stand, frosted cake and cherry.
        draw.line((cx - 126, cy + 92, cx + 126, cy + 92), fill=ink, width=18)
        draw.line((cx, cy + 92, cx, cy + 144), fill=ink, width=16)
        draw.line((cx - 65, cy + 145, cx + 65, cy + 145), fill=ink, width=15)
        draw.rounded_rectangle((cx - 100, cy - 28, cx + 100, cy + 83), radius=25, fill=accent)
        draw.arc((cx - 100, cy - 110, cx + 100, cy + 38), start=190, end=350, fill="#FFFFFF", width=34)
        draw.ellipse((cx - 22, cy - 137, cx + 24, cy - 92), fill="#B94842")
        draw.arc((cx + 4, cy - 192, cx + 80, cy - 106), start=180, end=285, fill="#83A76E", width=12)

    draw.text((76, 70), "РАБОТА В ОБЩЕПИТЕ", font=f_label, fill="#F7F2E9")
    draw.rounded_rectangle((76, 132, 430, 144), radius=6, fill=accent)
    title_size = 68
    f_title = font(title_size, True)
    title_lines: list[str] = []
    current_line = ""
    for word in str(title or "Вакансия").split():
        candidate = f"{current_line} {word}".strip()
        if current_line and draw.textlength(candidate, font=f_title) > 660:
            title_lines.append(current_line)
            current_line = word
        else:
            current_line = candidate
    if current_line:
        title_lines.append(current_line)
    y = 207
    if len(title_lines) > 2:
        title_size = 54
        f_title = font(title_size, True)
        title_lines = []
        current_line = ""
        for word in str(title or "Вакансия").split():
            candidate = f"{current_line} {word}".strip()
            if current_line and draw.textlength(candidate, font=f_title) > 660:
                title_lines.append(current_line)
                current_line = word
            else:
                current_line = candidate
        if current_line:
            title_lines.append(current_line)
    for line in title_lines[:3]:
        draw.text((76, y), line, font=f_title, fill="#FFFFFF")
        y += title_size + 16
    role = "ПОВАР" if is_cook else "КОНДИТЕР"
    draw.text((78, 746), role, font=font(30, True), fill="#F7F2E9")

    # Lower information panel.
    draw.rounded_rectangle((56, 875, 1024, 1245), radius=38, fill="#FFFFFF")
    draw.text((98, 928), "ЗАРАБОТНАЯ ПЛАТА", font=f_brand, fill="#66706C")
    salary_lines = _format_values({"salary": salary or "Условия уточняются"}, "salary", 24)
    draw.text((98, 980), salary_lines[0][:32], font=f_salary, fill=dark)
    location_lines = _format_values({"location": location or "Москва"}, "location", 44)
    draw.ellipse((99, 1100, 124, 1125), fill=accent)
    draw.text((148, 1090), location_lines[0][:48] if location_lines else "Москва", font=f_small, fill=dark)
    draw.text((76, 1285), "ОТКРЫТАЯ ВАКАНСИЯ  ·  trudvsem.ru", font=f_brand, fill="#66706C")

    image.save(output, format="PNG", optimize=True)
    return output


def _data_fields_for_card(post_text: str) -> tuple[str, str, str]:
    title = "Вакансия"
    salary = ""
    location = "Москва"
    for line in post_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and title == "Вакансия":
            title = stripped
        if "руб" in stripped.casefold() or "₽" in stripped:
            salary = stripped
        if stripped.startswith("📍"):
            continue
        if location == "Москва" and stripped and ("метро" in stripped.casefold() or "москва" in stripped.casefold()):
            location = stripped
    return title, salary, location


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _full_sync_due(store: WorkRussiaStore, now: datetime, interval_hours: int) -> bool:
    last = store.meta("last_full_scan")
    if not last:
        return True
    try:
        parsed = datetime.fromisoformat(last)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return now - parsed >= timedelta(hours=interval_hours)


def refresh_work_russia(
    config: dict[str, Any],
    store: WorkRussiaStore | None = None,
    client: WorkRussiaClient | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> int:
    """Poll cook/confectioner records; a failed poll never advances the cursor."""
    store = store or WorkRussiaStore()
    current = now or _utc_now()
    poll_minutes = max(1, int(config.get("source_poll_interval_minutes", 10)))
    last_poll = store.meta("last_poll_started")
    if last_poll and not force:
        try:
            if current - datetime.fromisoformat(last_poll) < timedelta(minutes=poll_minutes):
                return 0
        except ValueError:
            pass

    if client is None:
        client = WorkRussiaClient(
            str(config.get("work_russia_api_root", API_ROOT)),
            str(config.get("region_code", MOSCOW_REGION_CODE)),
        )
    topics = [
        topic for topic in config.get("target", {}).get("topics", [])
        if topic.get("name", "").casefold() in {"повар", "кондитер"}
    ]
    if not topics:
        raise WorkRussiaAPIError("В config.json не настроены темы «Повар» и «Кондитер»")

    full_scan = force or _full_sync_due(store, current, int(config.get("full_refresh_interval_hours", 24)))
    previous_cursor = store.meta("modified_cursor")
    modified_from = None if full_scan else previous_cursor
    scan_id = current.isoformat(timespec="seconds") if full_scan else None
    started_at = current
    added = 0
    store.set_meta("last_poll_started", current.isoformat(timespec="seconds"))
    try:
        for topic in topics:
            topic_name = str(topic["name"])
            query = "повар" if topic_name.casefold() == "повар" else "кондитер"
            for raw in client.search(
                query,
                modified_from=modified_from,
                page_size=int(config.get("source_page_size", 100)),
                hard_cap=int(config.get("source_hard_cap_per_query", 10_000)),
            ):
                vacancy = normalize_vacancy(raw)
                if not vacancy:
                    continue
                matching_topic = topic_for_title(str(vacancy["name"]), topics)
                if matching_topic != topic_name:
                    continue
                vacancy_id = str(vacancy["id"])
                if not vacancy.get("active", True):
                    store.mark_inactive(vacancy_id)
                    continue
                if store.enqueue(vacancy, topic_name, scan_id):
                    added += 1
            if full_scan and scan_id and not getattr(client, "last_scan_truncated", False):
                count = store.mark_missing_from_full_scan(topic_name, scan_id)
                if count:
                    logging.info("Marked %s missing or inactive Work Russia records in %s", count, topic_name)
    except Exception:
        retry_start = current - timedelta(minutes=max(0, poll_minutes - 2))
        store.set_meta("last_poll_started", retry_start.isoformat(timespec="seconds"))
        raise

    cursor = _iso_z(started_at - timedelta(minutes=10))
    store.set_meta("modified_cursor", cursor)
    if full_scan:
        store.set_meta("last_full_scan", started_at.isoformat(timespec="seconds"))
    store.set_meta("last_poll_completed", _utc_now().isoformat(timespec="seconds"))
    logging.info("Work Russia sync completed; %s new vacancies queued", added)
    return added


def work_russia_tick(
    config: dict[str, Any],
    telegram_api: Callable[..., Any],
    store: WorkRussiaStore | None = None,
    client: WorkRussiaClient | None = None,
    now: datetime | None = None,
    image_factory: Callable[[str, str, str, str, str], Path] = generate_vacancy_card,
) -> str | None:
    """Sync on a local interval, then send one photo-with-caption per vacancy."""
    if not config.get("automation_enabled") or config.get("source") != "work_russia":
        return None
    store = store or WorkRussiaStore()
    current = now or _utc_now()
    try:
        refresh_work_russia(config, store=store, client=client, now=current)
    except WorkRussiaAPIError as exc:
        logging.warning("Work Russia sync will retry later: %s", str(exc)[:220])
    except Exception:
        logging.exception("Unexpected Work Russia sync error; keeping stored queue")

    target = config.get("target", {})
    topics = [topic for topic in target.get("topics", []) if topic.get("thread_id") is not None]
    if not topics:
        logging.error("No configured Telegram topic IDs")
        return None

    gap = max(3, int(config.get("telegram_min_seconds_between_messages", 4)))
    last_send = store.meta("last_telegram_send_at")
    if last_send:
        try:
            previous = datetime.fromisoformat(last_send)
            if current - previous < timedelta(seconds=gap):
                return None
        except ValueError:
            pass

    row = store.next_pending(current)
    if not row:
        repeat_days = int(config.get("repeat_after_days", 7))
        row = store.next_repeat(current, topics, repeat_days)
    if not row:
        return "queue_empty"

    topic = next((item for item in topics if item["name"] == row["topic_name"]), None)
    if not topic:
        store.mark_inactive(row["vacancy_id"])
        return "topic_missing"
    payload = {
        "chat_id": target["chat_id"],
        "message_thread_id": topic["thread_id"],
    }

    try:
        title, salary, location = _data_fields_for_card(str(row["post_text"]))
        image_path = Path(row["image_path"]) if row["image_path"] else image_factory(
            str(row["vacancy_id"]), title, salary or str(row["salary_text"]),
            location or str(row["location_text"]), str(row["topic_name"]),
        )
        store.save_image(str(row["vacancy_id"]), str(image_path))
        store.start_photo(str(row["vacancy_id"]))
        caption = caption_for_telegram(str(row["post_text"]))
        result = telegram_api(
            "sendPhoto",
            {**payload, "caption": caption},
            files={"photo": image_path},
        )
        message = result if isinstance(result, dict) else {}
        store.mark_message_sent(str(row["vacancy_id"]), message.get("message_id"))
        store.set_meta("last_telegram_send_at", current.isoformat(timespec="seconds"))
        return "sent"
    except Exception as exc:
        retry_after = getattr(exc, "retry_after", None)
        delay = int(retry_after) if retry_after else min(3600, 20 * (2 ** min(int(row["attempts"]), 7)))
        resume_at = current + timedelta(seconds=delay)
        store.defer(str(row["vacancy_id"]), resume_at, str(exc))
        store.set_meta("last_telegram_send_at", current.isoformat(timespec="seconds"))
        return "deferred"

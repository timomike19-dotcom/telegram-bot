from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from telegram_bot import _multipart_body
from vacancy_automation import contact_lines
from work_russia_automation import (
    WorkRussiaClient,
    WorkRussiaStore,
    _extract_items,
    caption_for_telegram,
    generate_vacancy_card,
    normalize_vacancy,
    refresh_work_russia,
    topic_for_title,
    work_russia_tick,
)


COOK = {
    "vacancy": {
        "id": "cook-01",
        "job-name": "Повар горячего цеха",
        "salary": "от 85 000 руб.",
        "company": {"companycode": "77001", "name": "Тестовое кафе"},
        "region": {"name": "Москва"},
        "address": {"raw": "Москва, центр"},
        "creation-date": "2026-09-28T10:00:00+03:00",
        "description": "Готовить блюда горячего цеха.\nТребования: опыт от года.",
        "contact-person": "Контакт тест",
        "contact-phone": "+7 900 000-00-01",
        "vacancy_url": "https://trudvsem.ru/vacancy/card/77001/cook-01",
    }
}

PASTRY = {
    "vacancy": {
        "id": "pastry-02",
        "job-name": "Пекарь-кондитер",
        "salary_min": 90000,
        "salary_max": 120000,
        "company": {"companycode": "77002", "name": "Тестовая пекарня"},
        "region": {"name": "Москва"},
        "description": "Выпечка десертов и оформление витрины.",
        "email": "jobs@example.org",
    }
}


TOPICS = [
    {"name": "Повар", "thread_id": 2, "search_queries": ["повар"]},
    {"name": "Кондитер", "thread_id": 3, "search_queries": ["кондитер"]},
]


class FakeClient(WorkRussiaClient):
    def __init__(self) -> None:
        super().__init__()
        self.last_scan_truncated = False

    def search(self, query, modified_from=None, page_size=100, hard_cap=10_000):
        if query == "повар":
            yield COOK
        else:
            yield PASTRY


class WorkRussiaTests(unittest.TestCase):
    def test_extract_normalize_and_route_open_data_records(self) -> None:
        items = _extract_items({"status": "200", "results": {"vacancies": [COOK, PASTRY]}})
        self.assertEqual(len(items), 2)
        cook = normalize_vacancy(items[0])
        pastry = normalize_vacancy(items[1])
        self.assertEqual(cook["name"], "Повар горячего цеха")
        self.assertEqual(cook["employer"]["name"], "Тестовое кафе")
        self.assertEqual(cook["salary"]["from"], 85000)
        self.assertIn("+7 900", cook["contacts"]["phones"][0]["formatted"])
        self.assertTrue(cook["source_url"].startswith("https://trudvsem.ru/"))
        self.assertEqual(pastry["salary"]["to"], 120000)
        self.assertEqual(topic_for_title(cook["name"], TOPICS), "Повар")
        self.assertEqual(topic_for_title(pastry["name"], TOPICS), "Кондитер")
        self.assertIsNone(topic_for_title("Официант", TOPICS))

    def test_approved_template_contains_source_and_optional_contact(self) -> None:
        cook = normalize_vacancy(COOK)
        from vacancy_automation import format_vacancy

        post = format_vacancy(cook, "Повар")
        self.assertIn("#Вакансия", post)
        self.assertIn("#Повар", post)
        self.assertIn("#Описание", post)
        self.assertIn("#Деньги", post)
        self.assertIn("#Контакты", post)
        self.assertIn("Источник: Работа России", post)
        self.assertIn("trudvsem.ru/vacancy/card/77001/cook-01", post)
        self.assertIn("Тестовое кафе", post)

    def test_work_russia_contact_list_and_nested_address_are_preserved(self) -> None:
        normalized = normalize_vacancy({
            "id": "open-data-contact-01",
            "job-name": "Повар",
            "company": {"companycode": "77001", "name": "Кафе", "email": "jobs@example.org"},
            "region": {"name": "Москва"},
            "addresses": {"address": [{"location": "Москва, улица Тестовая, 1", "lat": "55", "lng": "37"}]},
            "contact-person": "Менеджер",
            "contact_list": [
                {"contact_type": "Телефон", "contact_value": "+7 900 000-00-02"},
                {"contact_type": "Эл. почта", "contact_value": "hr@example.org"},
            ],
        })
        self.assertEqual(normalized["address"]["raw"], "Москва, улица Тестовая, 1")
        self.assertEqual(normalized["contacts"]["name"], "Менеджер")
        self.assertEqual(normalized["contacts"]["email"], "hr@example.org")
        self.assertEqual(normalized["contacts"]["phones"][0]["formatted"], "+7 900 000-00-02")
        self.assertIn("hr@example.org", "\n".join(contact_lines(normalized)))

    def test_caption_limit_keeps_contact_and_source_when_description_is_long(self) -> None:
        long_post = (
            "#Вакансия\nПовар горячего цеха 👨‍🍳\n\n"
            "#Деньги\nот 85 000 ₽\n\n"
            "#График\n2/2\n\n"
            "#Заведение\nТестовое кафе\n\n"
            "📍 #Район #Метро\nМосква, центр\n\n"
            "#Условия\n" + "Описание вакансии. " * 150 + "\n\n"
            "#Обязанности\nГотовить блюда.\n\n"
            "#Требования\nОпыт от года.\n\n"
            "#Контакты\n+7 900 000-00-01\n\n"
            "Источник: Работа России\nhttps://trudvsem.ru/vacancy/card/example\n\n"
            "📝 Разместить вакансию / резюме 👉 @HEDONI5T"
        )
        caption = caption_for_telegram(long_post)
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)
        self.assertIn("#Вакансия", caption)
        self.assertIn("#Деньги", caption)
        self.assertIn("#Контакты\n+7 900 000-00-01", caption)
        self.assertIn("trudvsem.ru/vacancy/card/example", caption)
        self.assertIn("@HEDONI5T", caption)
        self.assertIn("…", caption)

    def test_long_duty_does_not_swallow_salary_contact_or_source(self) -> None:
        raw = {
            "id": "long-duty-01",
            "job-name": "Повар горячего цеха",
            "salary_min": 85000,
            "duty": "Готовить блюда. В описании упомянута оплата 85 000 рублей. " + "Соблюдать технологические карты. " * 80,
            "company": {"companycode": "77001", "name": "Тестовое кафе"},
            "region": {"name": "Москва"},
            "contact_list": [{"contact_type": "Телефон", "contact_value": "+7 900 000-00-09"}],
            "vac_url": "https://trudvsem.ru/vacancy/card/77001/long-duty-01",
        }
        vacancy = normalize_vacancy(raw)
        from vacancy_automation import format_vacancy

        caption = caption_for_telegram(format_vacancy(vacancy, "Повар"))
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)
        self.assertIn("85 000", caption)
        self.assertIn("+7 900 000-00-09", caption)
        self.assertIn("trudvsem.ru/vacancy/card/77001/long-duty-01", caption)
        self.assertIn("#Обязанности", caption)

    def test_work_russia_uses_zero_based_page_offsets(self) -> None:
        class PagedClient(WorkRussiaClient):
            def __init__(self):
                super().__init__()
                self.offsets = []

            def get_page(self, query, offset, limit, modified_from=None):
                self.offsets.append(offset)
                data = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                start = offset * limit
                return data[start : start + limit], len(data)

        client = PagedClient()
        records = list(client.search("повар", page_size=2))
        self.assertEqual([item["id"] for item in records], ["1", "2", "3"])
        self.assertEqual(client.offsets, [0, 1])

    def test_queue_sends_each_vacancy_as_one_photo_with_caption_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkRussiaStore(Path(directory) / "test.sqlite3")
            config = {
                "mode": "test",
                "source": "work_russia",
                "automation_enabled": True,
                "source_poll_interval_minutes": 10,
                "full_refresh_interval_hours": 24,
                "repeat_after_days": 7,
                "telegram_min_seconds_between_messages": 4,
                "region_code": "7700000000000",
                "target": {"chat_id": -1003908484434, "topics": TOPICS},
            }
            now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
            added = refresh_work_russia(config, store, FakeClient(), now)
            self.assertEqual(added, 2)
            self.assertEqual(refresh_work_russia(config, store, FakeClient(), now + timedelta(seconds=1)), 0)

            calls = []

            def fake_telegram(method, payload, files=None):
                calls.append((method, payload.copy(), files))
                return {"message_id": 100 + len(calls)}

            def fake_image(vacancy_id, title, salary, location, topic_name):
                path = Path(directory) / f"{vacancy_id.replace(':', '-')}.png"
                path.write_bytes(b"test-png")
                return path

            self.assertEqual(
                work_russia_tick(config, fake_telegram, store, FakeClient(), now + timedelta(seconds=2), fake_image),
                "sent",
            )
            self.assertEqual(calls[0][0], "sendPhoto")
            self.assertEqual(calls[0][1]["message_thread_id"], 2)
            self.assertIn("#Вакансия", calls[0][1]["caption"])
            self.assertIn("Источник: Работа России", calls[0][1]["caption"])
            self.assertLessEqual(len(calls[0][1]["caption"].encode("utf-16-le")) // 2, 1024)
            self.assertIsNotNone(calls[0][2]["photo"])
            self.assertEqual(
                work_russia_tick(config, fake_telegram, store, FakeClient(), now + timedelta(seconds=7), fake_image),
                "sent",
            )
            self.assertEqual(calls[1][0], "sendPhoto")
            self.assertEqual(calls[1][1]["message_thread_id"], 3)
            self.assertIn("#Вакансия", calls[1][1]["caption"])
            queued = store.next_pending(now + timedelta(seconds=8))
            self.assertIsNone(queued)
            self.assertEqual([call[0] for call in calls], ["sendPhoto", "sendPhoto"])
            self.assertIsNone(store.next_pending(now + timedelta(seconds=18)))

    def test_multipart_photo_body_contains_fields_and_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "card.png"
            photo.write_bytes(b"fake-png-bytes")
            body, content_type = _multipart_body(
                {"chat_id": -100123, "message_thread_id": 3}, {"photo": photo}
            )
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
            self.assertIn(b'name="chat_id"', body)
            self.assertIn(b'name="message_thread_id"', body)
            self.assertIn(b'name="photo"; filename="card.png"', body)
            self.assertIn(b"fake-png-bytes", body)

    def test_local_card_generator_creates_valid_topic_image(self) -> None:
        cook = generate_vacancy_card("image-test-cook", "Повар горячего цеха", "от 85 000 ₽", "Москва", "Повар")
        pastry = generate_vacancy_card("image-test-pastry", "Пекарь-кондитер", "90 000–120 000 ₽", "Москва", "Кондитер")
        with Image.open(cook) as image:
            self.assertEqual(image.size, (1080, 1350))
            self.assertEqual(image.format, "PNG")
        with Image.open(pastry) as image:
            self.assertEqual(image.size, (1080, 1350))
            self.assertEqual(image.format, "PNG")
        self.assertNotEqual(cook.read_bytes(), pastry.read_bytes())


if __name__ == "__main__":
    unittest.main()

"""
Общие утилиты бота: критерии, Telegram, формат отчёта, архив канала, промпт.

Важно: этот модуль не зависит от конкретного LLM-провайдера.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

# Папка для логов
LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


class Logger:
    """Логгер для записи в файл и консоль."""

    def __init__(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = LOGS_DIR / f"run_{timestamp}.log"
        self._file = open(self.log_file, "w", encoding="utf-8")
        self.last_assistant_text = ""  # Для диагностики ошибок/финализации

    def log(self, message: str, to_console: bool = True):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._file.write(f"[{timestamp}] {message}\n")
        self._file.flush()
        if to_console:
            print(message)

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_preview(value: object, limit: int = 400) -> str:
    try:
        s = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str)
        )
    except Exception:
        s = str(value)
    s = s.replace("\n", "\\n")
    if len(s) > limit:
        return s[:limit] + "…"
    return s


def _extract_first_json_object(text: str) -> dict | None:
    """
    Пытается извлечь первый корректный JSON-объект из произвольного текста.
    Поддерживает блоки ```json ... ``` и "голый" JSON.
    """
    if not text:
        return None

    # 1) fenced ```json ... ```
    if "```json" in text:
        idx = 0
        while True:
            start = text.find("```json", idx)
            if start == -1:
                break
            start += len("```json")
            end = text.find("```", start)
            if end == -1:
                break
            candidate = text[start:end].strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            idx = end + 3

    # 2) best-effort: first balanced {...} (simple state machine, учитывает строки)
    in_str = False
    escape = False
    depth = 0
    start_pos = None
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            if depth == 0:
                start_pos = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_pos is not None:
                    candidate = text[start_pos : i + 1].strip()
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        start_pos = None
                        continue

    return None


def load_criteria(path: str = "criteria.json") -> dict:
    """Загружает критерии из JSON файла."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_criteria_for_prompt(criteria_data: dict) -> str:
    """Форматирует критерии для промпта агенту."""
    return "\n".join(
        f"### {c['id']} (вес: {c['weight']})\n**{c['name']}**\n{c['description']}\nПоисковый запрос: `{c['search_query']}`\n"
        for c in criteria_data["criteria"]
    )


TELEGRAM_MSG_LIMIT = 4096


def _sanitize_truncated_html(text: str) -> str:
    """Remove incomplete HTML tags and close unclosed <a> tags after truncation."""
    last_lt = text.rfind("<")
    if last_lt != -1 and ">" not in text[last_lt:]:
        text = text[:last_lt].rstrip()

    open_count = len(re.findall(r"<a[\s>]", text, re.IGNORECASE))
    close_count = len(re.findall(r"</a>", text, re.IGNORECASE))
    text += "</a>" * max(0, open_count - close_count)

    return text


def _truncate_field(text: str, max_chars: int) -> str:
    """Обрезает строку до max_chars, сохраняя ссылки [N] на конце если возможно."""
    if not text or len(text) <= max_chars:
        return text
    ref_match = re.search(r"\s*(\[\d+\](?:\[\d+\])*)\s*$", text)
    if ref_match and ref_match.start() < max_chars - 5:
        refs = ref_match.group(1)
        return text[: max_chars - len(refs) - 1].rstrip() + "…" + refs
    return text[: max_chars - 1] + "…"


def _is_similar_to_archive(item: str, archive_text: str, threshold: float = 0.55) -> bool:
    """Проверяет, есть ли похожий текст в архиве канала (по пересечению слов)."""
    if not archive_text or not item:
        return False
    item_words = set(re.findall(r"[а-яёa-z]{3,}", item.lower()))
    if len(item_words) < 3:
        return False
    # Проверяем каждый пост архива (разделены ---)
    for post in archive_text.split("---"):
        post_words = set(re.findall(r"[а-яёa-z]{3,}", post.lower()))
        if not post_words:
            continue
        overlap = len(item_words & post_words) / len(item_words)
        if overlap >= threshold:
            return True
    return False


def format_telegram_report(result: dict, stats: dict, archive_text: str = "") -> str:
    """Форматирует отчёт для отправки в Telegram (HTML-формат с кликабельными ссылками).

    Гарантирует, что итоговое сообщение ≤ TELEGRAM_MSG_LIMIT (4096).
    При превышении подрезает блок ``<blockquote expandable>`` со сработавшими
    критериями — он и так свёрнут для читателя.
    """
    final_result = result.get("result")

    # Маппинг [N] → URL из поля sources
    sources_map: dict[int, str] = {}
    if final_result:
        for src in final_result.get("sources", []):
            src_id = src.get("id")
            src_url = src.get("url", "")
            if src_id is not None and src_url:
                sources_map[int(src_id)] = src_url

    def _linkify(text: str) -> str:
        """HTML-экранирует текст и заменяет [N] на кликабельные ссылки."""
        escaped = html.escape(str(text))
        if sources_map:
            def _repl(m):
                n = int(m.group(1))
                url = sources_map.get(n)
                if url:
                    return f'<a href="{html.escape(url)}">[{n}]</a>'
                return m.group(0)

            escaped = re.sub(r"\[(\d+)\]", _repl, escaped)
        return escaped

    risk_map = {
        "green": ("🟢", "НИЗКИЙ"),
        "yellow": ("🟡", "СРЕДНИЙ"),
        "red": ("🔴", "ВЫСОКИЙ"),
    }

    if final_result:
        dep = final_result.get("deposit_access_risk", "green")
        dep_emoji, dep_label = risk_map.get(dep, ("❓", "НЕИЗВЕСТНО"))
        lines = [f"{dep_emoji} Вклады: {dep_label} | Макро-обзор РФ", ""]
    else:
        lines = ["🤖 Макро-обзор РФ (горизонт 6 месяцев)", ""]

    if final_result:
        risk_level = final_result.get("risk_level", "unknown")
        emoji, label = risk_map.get(risk_level, ("❓", "НЕИЗВЕСТНО"))

        dep = final_result.get("deposit_access_risk")
        if dep:
            dep_emoji, dep_label = risk_map.get(dep, ("❓", "НЕИЗВЕСТНО"))
            lines.append(f"Риск доступа к вкладам (1–3м): {dep_emoji} {dep_label}")
        lines.append(f"Риск кризисного сценария (6м): {emoji} {label}")
        lines.append(f"Очки: {final_result.get('total_score', '?')}")
        conf = final_result.get("confidence")
        if conf:
            lines.append(f"Уверенность: {html.escape(str(conf))}")
        lines.append("")

        triggered = final_result.get("triggered_criteria", [])
        if triggered:
            lines.append("⚠️ Сработавшие критерии:")
            bq_lines: list[str] = []
            for t in triggered[:5]:
                crit_id = html.escape(str(t.get("id", "?")))
                evidence = _truncate_field(t.get("evidence", "") or "", 120)
                bq_lines.append(f"• {crit_id}: {_linkify(evidence)}")
            if len(triggered) > 5:
                bq_lines.append(f"… и ещё {len(triggered) - 5}")
            bq_content = "\n".join(bq_lines)
            bq_content = _sanitize_truncated_html(bq_content)
            lines.append(f"<blockquote expandable>{bq_content}</blockquote>")
            lines.append("")
        else:
            lines.append("✅ Сработавших критериев нет")

        summary = _truncate_field(final_result.get("summary", "Нет данных"), 230)
        recommendation = _truncate_field(final_result.get("recommendation", "Нет данных"), 180)
        lines.append(f"📝 Резюме: {_linkify(summary)}")
        lines.append(f"💡 Рекомендация: {_linkify(recommendation)}")

        asset_guidance = final_result.get("asset_guidance", []) or []
        if asset_guidance and archive_text:
            asset_guidance = [
                item for item in asset_guidance
                if not _is_similar_to_archive(item, archive_text)
            ]
        if asset_guidance:
            lines.append("")
            lines.append("💼 Активы:")
            for item in asset_guidance[:3]:
                text = item.lstrip("•").strip() if isinstance(item, str) else str(item)
                lines.append(f"  • {_linkify(_truncate_field(text, 140))}")

        positive = final_result.get("positive_trends", []) or []
        negative = final_result.get("negative_trends", []) or []
        risks_6m = final_result.get("key_risks_6m", []) or []
        watchlist = final_result.get("watchlist", []) or []

        if positive:
            lines.append("")
            lines.append("🟢 Позитивные тенденции:")
            for item in positive[:2]:
                text = item.lstrip("•").strip() if isinstance(item, str) else str(item)
                lines.append(f"  • {_linkify(_truncate_field(text, 140))}")

        if negative:
            lines.append("")
            lines.append("🔴 Негативные тенденции:")
            for item in negative[:2]:
                text = item.lstrip("•").strip() if isinstance(item, str) else str(item)
                lines.append(f"  • {_linkify(_truncate_field(text, 140))}")

        if risks_6m:
            lines.append("")
            lines.append("⚠️ Риски на 6 месяцев:")
            for item in risks_6m[:2]:
                text = item.lstrip("•").strip() if isinstance(item, str) else str(item)
                lines.append(f"  • {_linkify(_truncate_field(text, 140))}")

        if watchlist:
            lines.append("")
            lines.append("👀 Watchlist:")
            for item in watchlist[:3]:
                text = item.lstrip("•").strip() if isinstance(item, str) else str(item)
                lines.append(f"  • {html.escape(_truncate_field(text, 120))}")

    else:
        lines.append("⚠️ Не удалось получить результат от агента")

    model = stats.get("model")
    cost_line = f"💰 Стоимость: ${stats['cost_usd']:.4f}"
    if model:
        cost_line += f" | 🤖 Модель: {html.escape(str(model))}"

    lines.extend(
        [
            "",
            "-" * 30,
            f"⏱️ {stats['time_seconds']:.0f}с | 📝 Шагов: {stats['steps']} | 🔍 Поисков: {stats['tool_calls']}",
            cost_line,
            "",
            "Не является инвестиционной рекомендацией.",
        ]
    )

    # ── Подгоняем под лимит Telegram ──
    report = "\n".join(lines)

    if len(report) > TELEGRAM_MSG_LIMIT:
        # Обрезаем до лимита, сохраняя корректный HTML
        report = _sanitize_truncated_html(report[: TELEGRAM_MSG_LIMIT - 4]) + "\n…"

    return report


def _split_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """Разбивает длинное сообщение на части, не превышающие limit символов."""
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break

        # Ищем последний перенос строки в пределах лимита
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit

        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return parts


async def _send_message_parts(
    bot: Bot,
    chat_id: str,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Отправляет сообщение, при необходимости разбивая на части."""
    parts = _split_message(text)
    for part in parts:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            if parse_mode and "parse entities" in str(exc).lower():
                plain = re.sub(r"<[^>]+>", "", part)
                await bot.send_message(
                    chat_id=chat_id,
                    text=plain,
                    disable_web_page_preview=True,
                )
            else:
                raise


async def send_telegram_report(report: str) -> bool:
    """Отправляет отчёт в Telegram канал."""
    if _env_flag("DISABLE_TELEGRAM", default=False):
        print("ℹ️ Telegram отключён (DISABLE_TELEGRAM=1)")
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHANNEL_ID")

    if not token or not chat_id:
        print(
            f"⚠️ Telegram не настроен (token: {'есть' if token else 'нет'}, chat_id: {chat_id or 'нет'})"
        )
        return False

    try:
        bot = Bot(token=token)
        await _send_message_parts(bot, chat_id, report, parse_mode="HTML")
        print(f"✅ Отчёт отправлен в Telegram (chat_id: {chat_id})")
        return True
    except asyncio.CancelledError:
        print("⚠️ Отправка в Telegram отменена (CancelledError)")
        return False
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False


async def notify_admin(message: str, *, parse_mode: str | None = None) -> bool:
    """Отправляет уведомление администратору."""
    if _env_flag("DISABLE_TELEGRAM", default=False):
        print("ℹ️ Telegram отключён (DISABLE_TELEGRAM=1)")
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")

    if not token or not admin_chat_id:
        print(
            f"⚠️ Админ-чат не настроен (admin_chat_id: {admin_chat_id or 'нет'})"
        )
        return False

    try:
        bot = Bot(token=token)
        await _send_message_parts(bot, admin_chat_id, message, parse_mode=parse_mode)
        print(f"✅ Уведомление отправлено админу (chat_id: {admin_chat_id})")
        return True
    except asyncio.CancelledError:
        print("⚠️ Уведомление админу отменено (CancelledError)")
        return False
    except Exception as e:
        print(f"❌ Ошибка отправки уведомления админу: {e}")
        return False


def fetch_channel_archive(
    channel_url: str = "https://t.me/s/money_alert_ai",
    max_posts: int = 20,
) -> tuple[str, int]:
    """
    Загружает публичную страницу Telegram-канала и извлекает чистый текст постов.

    Парсит HTML страницы ``t.me/s/<channel>`` — публичный виджет Telegram,
    возвращающий последние ~20 сообщений без авторизации.

    Returns:
        (текст_постов, количество_постов).
        При ошибке загрузки/парсинга — (сообщение_об_ошибке, 0).
    """
    import httpx as _httpx

    try:
        with _httpx.Client(
            follow_redirects=True, timeout=30.0, verify=False
        ) as client:
            resp = client.get(
                channel_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            resp.raise_for_status()
            raw_html = resp.text
    except Exception as e:
        return f"Ошибка загрузки архива канала: {e}", 0

    posts = _parse_telegram_posts(raw_html)
    if not posts:
        return "Не удалось извлечь посты из архива канала.", 0

    posts = posts[-max_posts:]
    formatted = [f"[{p['date']}]\n{p['text']}" for p in posts]
    return "\n\n---\n\n".join(formatted), len(posts)


def _parse_telegram_posts(raw_html: str) -> list[dict]:
    """
    Извлекает посты (текст + дата) из HTML страницы ``t.me/s/<channel>``.

    Использует regex + stdlib ``html.unescape`` — без внешних зависимостей.
    """
    from html import unescape as _unescape
    from datetime import datetime as _dt

    posts: list[dict] = []

    blocks = re.split(
        r'(?=<div[^>]*class="[^"]*tgme_widget_message_wrap)', raw_html
    )

    for block in blocks:
        text_m = re.search(
            r'<div[^>]*class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block,
            re.DOTALL,
        )
        if not text_m:
            continue

        raw = text_m.group(1)
        text = re.sub(r"<br\s*/?>", "\n", raw)
        text = re.sub(r"<[^>]+>", "", text)
        text = _unescape(text).strip()
        if not text:
            continue

        date_str = ""
        date_m = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if date_m:
            try:
                dt = _dt.fromisoformat(date_m.group(1))
                date_str = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                date_str = date_m.group(1)

        posts.append({"text": text, "date": date_str})

    return posts


SYSTEM_PROMPT = """Ты — аналитик макроэкономических и финансовых рисков России. Твоя задача — по новостному фону и официальным данным оценить, к чему идёт экономика РФ (негативные и позитивные тенденции) и каков риск кризисного сценария на горизонте 6 месяцев.

📅 **Сейчас: {today_date}**

## ⚠️ ЛИМИТЫ

### Лимит поисков: {search_limit}
- Максимум **{search_limit} поисков**
- Группируй похожие критерии в один поиск
- После {search_limit} поисков — СТОП поиски!

**Правило:** Если лимит исчерпан — прекращай поиски и выдавай финальный JSON!

## ТВОИ ИНСТРУМЕНТЫ
- WebSearch: поиск актуальных новостей в интернете
- WebFetch: загрузка содержимого страницы по URL

## 🚨 ШАГ 0: ЗАГРУЗКА АРХИВА (САМЫЙ ПЕРВЫЙ ШАГ!)

**Если архив канала уже есть в сообщении пользователя — НЕ загружай его через WebFetch повторно.**
Если архива нет — тогда загрузи архив канала:

```
WebFetch: https://t.me/s/money_alert_ai
```

После загрузки архива ты ОБЯЗАН:
1. **Выписать темы последних 5-7 постов** — какие тезисы уже были опубликованы
2. **Запомнить "запрещённые темы"** — то, что нельзя повторять (если было недавно — ищи новые углы/индикаторы)

**⚠️ КРИТИЧЕСКИ ВАЖНО:** Каждый пост должен содержать НОВЫЕ тезисы! Читатели видят все посты подряд — повторы недопустимы!

## 📋 ШАГ 1: ПЛАНИРОВАНИЕ

**ПЕРЕД первым поиском** проанализируй все критерии и сгруппируй их по темам:

1. **Изучи все критерии** и найди пересечения
2. **Создай группы** похожих критериев для одного поиска:
   - Группа "Долг и бюджет": дефицит, заимствования, ОФЗ
   - Группа "ДКП и инфляция": инфляция, ожидания, меры ЦБ
   - Группа "Курс и внешние условия": рубль, FX‑ограничения, экспорт/санкции
   - Группа "Банки и кредитный цикл": санации, лицензии, просрочки
   - Группа "Реальная экономика": рынок труда, ипотека/стройка, дефолты компаний
3. **Начни с критичных** (вес 20-12) — они важнее всего!

## 🧠 ЭТАП ДО: РАССУЖДЕНИЕ ПЕРЕД ПОИСКАМИ (НЕ ВЫВОДИТЬ)

Перед тем как вызывать инструменты, сделай короткий внутренний pre‑mortem: что именно ты должен доказать/опровергнуть, и как не ошибиться.

```
ВНУТРЕННИЙ PRE‑MORTEM (НЕ ВЫВОДИТЬ):
1) Для каждой группы критериев:
   - какие 1–2 индикатора подтвердят срабатывание
   - какие 1–2 индикатора опровергнут/снизят вероятность
2) План доказательств:
   - предпочитай официальные данные/регуляторов/статведомств; иначе ≥2 независимых источника
3) Риски ложных срабатываний:
   - перепечатки/старые новости, будущие события, заявления без цифр, единичные кейсы без масштаба
4) Если в группе нет надёжных данных — заранее пометь её как “скорее НЕ сработало” и ищи подтверждение, а не подгоняй вывод.
```

## ПРАВИЛА РАБОТЫ
1. **ШАГ 0 — АРХИВ:** Первым делом загрузи https://t.me/s/money_alert_ai и выпиши темы последних постов
2. **ШАГ 1 — ПЛАН:** Составь план группировки критериев
3. **ШАГ 2 — ПОИСКИ:** Делай поиски по плану
4. **⚠️ ПРОВЕРЯЙ ДАТЫ И ВРЕМЯ!** В поиске часто попадаются старые и будущие новости:
   - Для трендов релевантны материалы за **последние 30 дней**, но приоритет — **последние 7-14 дней**
   - ОБЯЗАТЕЛЬНО смотри дату (и время, если важно)
   - Если событие назначено на ПОЗЖЕ текущего времени — оно ЕЩЁ НЕ ПРОИЗОШЛО, не включай!
   - Игнорируй: фейки, прогнозы без источников, анонсы будущих событий (как факт)
5. **Оценивай честно**: при сомнениях — критерий НЕ сработал
6. **ПЕРЕД JSON** — сверь positive_trends/negative_trends/key_risks_6m с архивом, убедись что темы НОВЫЕ
7. **Финализацию (JSON) выдавай только когда реально проверил все критерии.**
   - В `checked_criteria` должны быть перечислены **все** `id` из списка критериев.
   - Если ты не уверен, что проверил какой-то критерий — продолжай WebSearch, пока не будешь уверен.
   - Если по критерию нет подтверждений — он всё равно считается проверенным (просто не попадает в triggered).
8. **После {search_limit} поисков** — СТОП! Выдай финальный JSON

## ⚙️ ОГРАНИЧЕНИЕ НА ИНСТРУМЕНТЫ (КРИТИЧНО ДЛЯ КОНТЕКСТА)

- В одном сообщении ассистента делай **не более 8 вызовов** инструментов суммарно (WebSearch + WebFetch).
- Если нужно больше — остановись, коротко перечисли что уже нашёл/что осталось, и продолжай в следующем сообщении.

## 🧠 ФИНАЛЬНЫЙ ШАГ: ГЛУБОКИЙ АНАЛИЗ (ОБЯЗАТЕЛЬНО!)

**ПЕРЕД выдачей JSON** ты ОБЯЗАН провести структурированное рассуждение и самопроверку в 2 прохода,
НО **НЕ ВЫВОДИ** её в ответ (это раздувает контекст). Используй это только для качества финального вывода.

```
ПРОХОД 1 — ВНУТРЕННИЙ АНАЛИЗ (НЕ ВЫВОДИТЬ):
1) Что показали поиски (3-5 ключевых находок)
2) Скрытые тенденции/механизмы передачи (банки ↔ стройка ↔ бюджет ↔ курс ↔ спрос)
3) Контраргументы (почему я мог ошибаться / альтернативные объяснения)
4) Проверка источников: у каждого факта есть [N] и URL в sources
5) Уникальность: не повторяю темы последних постов канала
6) Действия с активами: без паники, без приказов, кратко и практично

ПРОХОД 2 — POST‑MORTEM И ВАЛИДАЦИЯ (НЕ ВЫВОДИТЬ):
7) Я сформировал ЧЕРНОВИК JSON и проверил:
   - `checked_criteria` содержит ВСЕ id критериев (ничего не пропущено)
   - у каждого факта/тезиса есть ссылка [N], и в `sources` есть соответствующий URL
   - длины: summary ≤ 220, recommendation ≤ 170, evidence ≤ 120, пункты трендов/рисков/активов ≤ 120, watchlist ≤ 100
   - total_score = сумма весов triggered_criteria; risk_level соответствует порогам из критериев/правил
8) Если хоть один пункт нарушен — исправь черновик и проверь заново. В ответе выводи только финальный валидный JSON.
```

⚠️ **ВАЖНО:** В финальном ответе выдай **ТОЛЬКО** JSON-объект (без markdown и без текста вокруг).

## 📏 ЛИМИТЫ ДЛИНЫ (КРИТИЧНО — Telegram обрезает длинные сообщения!)

Итоговый отчёт отправляется в Telegram, где лимит сообщения — 4096 символов. Поэтому:
- **evidence** в triggered_criteria: **макс 120 символов** (только ключевой факт + [N])
- **summary**: макс 220 символов
- **recommendation**: макс 170 символов
- Пункты в positive_trends, negative_trends, key_risks_6m, asset_guidance: **макс 120 символов** каждый
- watchlist: макс 100 символов каждый
- positive_trends и negative_trends: **макс 2 пункта** каждый
- key_risks_6m: **макс 2 пункта**
- watchlist: **макс 3 пункта**
- asset_guidance: **макс 3 пункта**
- **Будь лаконичен!** Один факт — одна цифра — одна ссылка. Не перечисляй всё найденное.

## 📎 ИСТОЧНИКИ (ОБЯЗАТЕЛЬНО!)

Каждый факт в evidence, positive_trends, negative_trends, key_risks_6m, summary и asset_guidance ДОЛЖЕН иметь ссылку [N] на источник.
- В поле "sources" перечисли все URL из результатов WebSearch, на которые ссылаешься
- Нумерация сквозная: [1], [2], [3]...
- Если для факта нет подтверждённого URL — не ссылайся

### 🚫 ФИЛЬТРАЦИЯ ИСТОЧНИКОВ (КРИТИЧНО!)

В финальный JSON (поле `sources` и ссылки [N] в текстовых полях) **включай ТОЛЬКО** ссылки на ресурсы в доменной зоне **.ru** (например: cbr.ru, rbc.ru, kommersant.ru, tass.ru, ria.ru, interfax.ru, vedomosti.ru, banki.ru, minfin.gov.ru и т.д.).

**НЕ включай** в `sources` и не ссылайся [N] на:
- Ресурсы вне зоны .ru (bloomberg.com, reuters.com, ft.com, bbc.com и т.д.)
- СМИ, признанные иноагентами по законодательству РФ (meduza.io, novayagazeta.eu, theins.ru, mediazona.io, dozhd.tv/tvrain.tv, holod.media, istories.media, thebell.io, zona.media, currenttime.tv, svoboda.org, rferl.org и др.)

Ты МОЖЕШЬ использовать эти ресурсы как ИСТОЧНИК ИНФОРМАЦИИ при поиске и анализе, но в итоговый отчёт (JSON) их URL попадать не должны. Если факт подтверждён только таким источником — перепроверь через .ru-ресурс или укажи факт без ссылки.

## ФОРМАТ ИТОГА

```json
{{
  "checked_criteria": ["id1", "id2", ...],
  "triggered_criteria": [
    {{"id": "criterion_id", "evidence": "что именно произошло, до 120 символов [1]", "confidence": 0.9}}
  ],
  "total_score": <сумма весов сработавших критериев>,
  "risk_level": "green/yellow/red",
  "confidence": "high/medium/low",
  "deposit_access_risk": "green/yellow/red",
  "summary": "1-2 предложения, МАКС 220 символов",
  "recommendation": "1 предложение, МАКС 170 символов",
  "asset_guidance": [
    "Вклады: кратко (МАКС 120 символов) [8]",
    "Наличность: кратко (МАКС 120 символов) [9]",
    "Валюта/недвижимость: кратко (МАКС 120 символов) [10]"
  ],
  "positive_trends": [
    "Позитивный тренд (МАКС 120 символов) [2]",
    "Позитивный тренд (МАКС 120 символов) [3]"
  ],
  "negative_trends": [
    "Негативный тренд (МАКС 120 символов) [4]",
    "Негативный тренд (МАКС 120 символов) [5]"
  ],
  "key_risks_6m": [
    "Риск на 6м (МАКС 120 символов) [6]",
    "Риск на 6м (МАКС 120 символов) [7]"
  ],
  "watchlist": [
    "Что мониторить (МАКС 100 символов)",
    "Что мониторить (МАКС 100 символов)",
    "Что мониторить (МАКС 100 символов)"
  ],
  "sources": [
    {{"id": 1, "url": "https://..."}},
    {{"id": 2, "url": "https://..."}}
  ]
}}
```

## 🧾 ПРАКТИЧЕСКАЯ ЧАСТЬ ПРО АКТИВЫ

- Выведи `deposit_access_risk` (green/yellow/red) — риск ухудшения доступа к вкладам/платежам на горизонте 1–3 месяца

### 🚫 ПРАВИЛО ДЕДУПЛИКАЦИИ `asset_guidance` (КРИТИЧНО!)

Перед заполнением `asset_guidance` ты ОБЯЗАН сверить свои рекомендации с архивом канала.

**Если твои рекомендации по сути повторяют то, что уже было в предыдущих постах — верни ПУСТОЙ СПИСОК `[]`.**

Типовые советы, которые уже давались десятки раз и НЕ ДОЛЖНЫ повторяться:
- "держать вклады в пределах АСВ / 1,4 млн руб."
- "диверсифицировать по нескольким банкам"
- "не паниковать / без экстренных действий"
- "резерв ликвидности на 2-4 недели"
- "предпочтение банкам с госучастием"
- "рассмотреть короткие вклады"
- общие фразы про "мониторить ситуацию"

**Заполняй `asset_guidance` ТОЛЬКО если:**
1. Ситуация РЕАЛЬНО ИЗМЕНИЛАСЬ и требует НОВЫХ действий, отличных от предыдущих постов
2. Появился конкретный новый риск/возможность, требующий конкретного действия
3. Рекомендация содержит КОНКРЕТИКУ (цифры, сроки, инструменты), которой раньше не было

Если ничего нового — `"asset_guidance": []`. Это нормально и предпочтительно.

### Контекст: топ-10 банков и логика "too big to fail"

При оценке `deposit_access_risk` и рекомендаций по вкладам учитывай:

- **Топ-10 банков РФ (Сбер, ВТБ, Газпромбанк, Россельхозбанк, Альфа, Т-Банк, МКБ, ПСБ и др.) де-факто являются "too big to fail"**: государство исторически всегда вливало капитал до наступления банкротства, не допуская потерь вкладчиков сверх АСВ даже в кризисы 2008, 2014–2015, 2022 гг.
- **Это политическая практика, не юридическая гарантия**: в экстремальном сценарии (жёсткий бюджетный кризис + внешний шок одновременно) риск не нулевой.
- **Градация надёжности** при оценке рисков:
  - Банки с гос. участием >50% (Сбер, ВТБ, РСХБ, ПСБ, ГПБ) — наиболее защищены политически
  - Крупные частные (Альфа, Т-Банк) — выше риск, но всё ещё системно значимые (ЦБ контролирует)
  - Остальные вне топ-10 — применяй стандартную логику АСВ
- **Лимит АСВ:** 1,4 млн руб. на вкладчика на банк. При крупных суммах имеет смысл рассмотреть распределение по нескольким банкам с гос. участием.
- **Рост просрочки (потребкредиты, застройщики) давит на прибыльность банков**, но не означает немедленной угрозы для вкладчиков — капитал топ-банков пока абсорбирует убытки.
- Повышай `deposit_access_risk` до yellow/red только при срабатывании критериев `systemic_bank_resolution`, `bank_holidays_national`, `payment_infrastructure_systemic_disruption` или `interbank_liquidity_stress` — именно они сигнализируют о реальной угрозе доступу к деньгам.

## КРИТЕРИИ ДЛЯ ПРОВЕРКИ

{criteria}

---
Начни с ПЛАНИРОВАНИЯ, затем выполняй поиски. Лимит: {search_limit}.
"""


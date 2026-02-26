# Агенты мониторинга макро-рисков РФ

## Обзор



## 1. LangChain агент (`src/lc_money_alert_bot.py`)

**Архитектура:** Один агент с инструментами

- Один агент (LangChain) с инструментами WebSearch/WebFetch
- Получает **все критерии сразу** в системном промпте
- **Сам планирует** порядок проверки
- Использует **WebSearch** для поиска новостей (Tavily)
- Может **группировать** похожие критерии

**Учёт расходов:**
- Выводит итоговую сумму в USD

**Формат вывода:**
```
📊 ИТОГОВЫЙ ОТЧЁТ
Уровень риска: 🟢 НИЗКИЙ
Очки: 0
✅ Сработавших критериев нет

📝 Резюме: ...
💡 Рекомендация: ...

💰 СТАТИСТИКА РАСХОДОВ
   Шагов агента: N
   Input токены: X
   Output токены: Y
   💵 ИТОГО: $Z.ZZZZ
```

**Запуск:**
```bash
uv run python src/lc_money_alert_bot.py                    # OpenAI (по умолчанию)
uv run python src/lc_money_alert_bot.py --provider gemini  # Gemini
uv run python src/lc_money_alert_bot.py --provider gigachat # GigaChat
```

⚠️ **Внимание:** Запуск занимает несколько минут и стоит денег (API вызовы).

---

## 3. Запуск на Modal.com по расписанию

**Файл:** `src/modal_app.py`

Бот запускается автоматически **каждые 3 дня в 9:00 по Москве** (6:00 UTC).

### Настройка секретов

Перед деплоем создайте секреты в [Modal Dashboard](https://modal.com/secrets) или через CLI:

```bash
# Tavily API ключ
modal secret create tavily TAVILY_API_KEY=tvly-...

# OpenAI (если используете openai)
modal secret create openai OPENAI_API_KEY=sk-...

# Google Gemini (если используете gemini)
modal secret create google GOOGLE_API_KEY=AIza...

# Telegram секреты
modal secret create telegram \
  TELEGRAM_BOT_TOKEN=123456789:ABC... \
  TELEGRAM_CHANNEL_ID=-100123456789 \
  TELEGRAM_ADMIN_CHAT_ID=-100123456789
```

| Секрет | Переменные | Обязательно | Описание |
|--------|------------|-------------|----------|
| `tavily` | `TAVILY_API_KEY` | ✅ | API ключ Tavily |
| `openai` | `OPENAI_API_KEY` | ⚪ | API ключ OpenAI (если используете OpenAI) |
| `google` | `GOOGLE_API_KEY` | ⚪ | API ключ Google Gemini (если используете Gemini) |
| `telegram` | `TELEGRAM_BOT_TOKEN` | ✅ | Токен Telegram бота |
| `telegram` | `TELEGRAM_CHANNEL_ID` | ✅ | ID канала для публикации отчётов |
| `telegram` | `TELEGRAM_ADMIN_CHAT_ID` | ⚪ | ID чата для уведомлений админу (опционально) |

### Деплой

```bash
# Задеплоить приложение с расписанием
modal deploy src/modal_app.py
```

После деплоя бот будет автоматически запускаться каждые 3 дня в 9:00 MSK.

### Тестовый запуск

```bash
# Запустить вручную и смотреть логи в реальном времени
modal run src/modal_app.py
```

### Просмотр логов

```bash
# Логи приложения
modal app logs money-alert-bot
```

Или в веб-интерфейсе: https://modal.com/apps/money-alert-bot

### Особенности

- **Таймаут:** 30 минут (агент может работать долго)
- **Расписание:** Cron `0 6 */3 * *` (каждые 3 дня, 9:00 MSK = 6:00 UTC)
- **Результат:** Отправляется в Telegram канал
- **Уведомления:** Админу отправляется сообщение о публикации с краткой статистикой

---

## Критерии

Критерии загружаются из `criteria.json` (по умолчанию) или из файла, заданного в `CRITERIA_FILE`.

- `criteria.json`: макро-критерии (негатив/позитив тенденции + риск кризисного сценария на 6 месяцев)
- `criteria_deposit_freeze.json`: старый профиль «риск заморозки вкладов» (оставлен для совместимости/архива)

**Пороги риска:**
- 🟢 **НИЗКИЙ** (green): 0-9 очков — ситуация стабильная
- 🟡 **СРЕДНИЙ** (yellow): 10-19 очков — повышенное внимание  
- 🔴 **ВЫСОКИЙ** (red): 20+ очков — срочные меры

---

## Зависимости

```
python-dotenv>=1.2.1
python-telegram-bot>=21.0
modal>=0.67.0
langchain>=1.2.10
langchain-gigachat-lc1>=0.4.0b4
langchain-openai>=0.3.0
langchain-google-genai>=2.1.0
langgraph>=1.0.8
httpx>=0.27.0
langchain-tavily>=0.2.17
```

### Переменные окружения

Для локального запуска — в файле `.env`:
- `TAVILY_API_KEY` — ключ Tavily для поиска
- `MODEL_PROVIDER` — `openai`, `gigachat` или `gemini`
- `OPENAI_API_KEY` — ключ OpenAI (если `MODEL_PROVIDER=openai`)
- `GOOGLE_API_KEY` — ключ Google Gemini (если `MODEL_PROVIDER=gemini`)
- `TELEGRAM_BOT_TOKEN` — токен Telegram бота
- `TELEGRAM_CHANNEL_ID` — ID канала для публикации отчётов
- `TELEGRAM_ADMIN_CHAT_ID` — ID чата для уведомлений админу (опционально)

## Cursor Cloud specific instructions

**Project type:** Pure Python CLI application (no web server, no database, no Docker). Uses `uv` as the sole package manager.

**Running the bot locally:** `uv run python src/lc_money_alert_bot.py --provider <openai|gemini|gigachat>`. Requires `TAVILY_API_KEY` and an LLM provider key (`OPENAI_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, or GigaChat credentials). Telegram is optional (`DISABLE_TELEGRAM=1` to skip).

**Quick test run:** `CRITERIA_FILE=criteria_small.json DISABLE_TELEGRAM=1 uv run python src/lc_money_alert_bot.py --provider openai` — uses 8 criteria instead of 25, finishes in ~30s, costs ~$0.18 (OpenAI) or ~$0.33 (Gemini).

**Gotcha — module-level API key validation:** `src/lc_money_alert_bot.py` cannot be imported or run (even `--help`) without `TAVILY_API_KEY` set, because `TavilySearch()` validates the key at import time (line 64).

**Gotcha — Gemini preview models:** `gemini-3.1-pro-preview` may have high latency or time out from cloud VMs. Override with `GEMINI_MODEL=gemini-2.5-flash` for faster/more reliable runs. The library accepts both `GOOGLE_API_KEY` and `GEMINI_API_KEY`.

**Linting:** No linter is configured in `pyproject.toml`. Use `uvx ruff check src/` for ad-hoc linting. One pre-existing warning exists (unused import in `modal_app.py`).

**Tests:** No test suite exists. Verify correctness by running the core utilities directly (criteria loading, prompt formatting, report generation — see `src/bot_common.py`), or by running the bot end-to-end with `criteria_small.json`.

**Criteria files:** `criteria.json` (25 criteria, default), `criteria_small.json` (8 criteria, for testing). Set `CRITERIA_FILE=criteria_small.json` for cheaper/faster test runs.

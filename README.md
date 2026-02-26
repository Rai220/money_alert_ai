# 📈 RF Macro Outlook AI

AI-агент для **макро-оценки экономики РФ**: негативные/позитивные тенденции и риск кризисного сценария на горизонте **6 месяцев**.

Ведёт Telegram-канал [@money_alert_ai](https://t.me/money_alert_ai)

## 🎯 Что делает

Анализирует новостной фон по набору критериев риска и выдаёт оценку риска кризисного сценария (6м):

- 🟢 **НИЗКИЙ** (0-9 очков) — ситуация стабильная
- 🟡 **СРЕДНИЙ** (10-19 очков) — повышенное внимание
- 🔴 **ВЫСОКИЙ** (20+ очков) — срочные меры

## 🏗️ Архитектура

**Один агент** (LangChain) с инструментами WebSearch/WebFetch:

- Получает все критерии в системном промпте
- Сам планирует порядок проверки
- Делает WebSearch по группам критериев
- Выдаёт компактный JSON → форматируется в Telegram-отчёт

## 🚀 Быстрый старт

### Требования

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) — быстрый менеджер пакетов
- `TAVILY_API_KEY` (поиск) и ключ LLM-провайдера (OpenAI, Gemini или GigaChat)

### Установка

```bash
# Клонировать репозиторий
git clone https://github.com/Rai220/money_alert_ai.git
cd money_alert_ai

# Скопировать пример конфигурации и заполнить своими ключами
cp .env.example .env

# Установить зависимости
uv sync
```

### Конфигурация

Отредактируйте `.env` файл:

| Переменная | Обязательно | Описание |
|------------|-------------|----------|
| `TAVILY_API_KEY` | ✅ | API ключ Tavily для поиска |
| `MODEL_PROVIDER` | ✅ | `openai`, `gemini` или `gigachat` |
| `OPENAI_API_KEY` | при `MODEL_PROVIDER=openai` | API ключ OpenAI |
| `GOOGLE_API_KEY` | при `MODEL_PROVIDER=gemini` | API ключ Google Gemini |
| `GIGACHAT_USER`/`GIGACHAT_PASSWORD` | при `MODEL_PROVIDER=gigachat` | доступ к GigaChat |
| `CRITERIA_FILE` | ❌ | Путь к файлу критериев (по умолчанию `criteria.json`) |
| `TELEGRAM_BOT_TOKEN` | ❌ | Токен бота для отправки отчётов в Telegram |
| `TELEGRAM_CHANNEL_ID` | ❌ | ID чата/канала для публикации |

Для тестов можно использовать сокращённый набор критериев:
```bash
CRITERIA_FILE=criteria_small.json
```

Также в репозитории сохранён старый профиль «риск заморозки вкладов»:

```bash
CRITERIA_FILE=criteria_deposit_freeze.json
```

### Локальный запуск

```bash
uv run python src/lc_money_alert_bot.py                    # OpenAI (по умолчанию)
uv run python src/lc_money_alert_bot.py --provider gemini  # Gemini
uv run python src/lc_money_alert_bot.py --provider gigachat # GigaChat
```

⚠️ **Внимание:** Запуск занимает несколько минут и стоит денег (API вызовы LLM + поиск).

### ☁️ Деплой на Modal.com (по расписанию)

Бот может автоматически запускаться каждый день в 9:00 по Москве.

**1. Создать секреты в Modal:**

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
  TELEGRAM_CHANNEL_ID=123456789
```

**2. Задеплоить:**

```bash
modal deploy src/modal_app.py
```

**3. Тестовый запуск (с логами):**

```bash
modal run src/modal_app.py
```

**4. Посмотреть логи:**

```bash
modal app logs money-alert-bot
```

## 📊 Пример вывода

```
📊 ИТОГОВЫЙ ОТЧЁТ
Уровень риска: 🟢 НИЗКИЙ
Очки: 0
✅ Сработавших критериев нет

📝 Резюме: Финансовая система стабильна...
💡 Рекомендация: Продолжать мониторинг...

💰 СТАТИСТИКА РАСХОДОВ
   Шагов агента: 12
   Input токены: 45,230
   Output токены: 3,450
   💵 ИТОГО: $0.1874
```

## 📁 Структура проекта

```
├── criteria.json                     # Макро-критерии РФ (по умолчанию)
├── criteria_small.json               # Сокращённый макро-набор для тестов
├── criteria_deposit_freeze.json      # Старый профиль: риск заморозки вкладов (архив)
├── criteria_deposit_freeze_small.json# Сокращённый депозитный набор
├── src/
│   ├── lc_money_alert_bot.py   # Основной агент (LangChain)
│   ├── bot_common.py           # Общие утилиты (Telegram, критерии, отчёт, промпт)
│   └── modal_app.py            # Запуск по расписанию на Modal
└── AGENTS.md             # Подробная документация
```

## 🔧 Зависимости

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

## 📜 Лицензия

MIT License © 2026 Konstantin Krestnikov

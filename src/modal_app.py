"""
Modal приложение для запуска бота мониторинга по расписанию.

Запуск:
  modal deploy src/modal_app.py   # Деплой с расписанием
  modal run src/modal_app.py      # Ручной тестовый запуск

Секреты Modal:
  - openai: OPENAI_API_KEY (если MODEL_PROVIDER=openai)
  - google: GOOGLE_API_KEY (если MODEL_PROVIDER=gemini)
  - tavily: TAVILY_API_KEY
  - telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_ADMIN_CHAT_ID
"""

import modal
from pathlib import Path

# Путь к корню проекта (относительно этого файла)
PROJECT_ROOT = Path(__file__).parent.parent

# Создаём Modal приложение
app = modal.App("money-alert-bot")

# Определяем image с зависимостями и локальными файлами
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "python-dotenv>=1.2.1",
        "python-telegram-bot>=21.0",
        "langchain>=1.2.10",
        "langchain-gigachat-lc1>=0.4.0b4",
        "langchain-openai>=0.3.0",
        "langchain-google-genai>=2.1.0",
        "langgraph>=1.0.8",
        "httpx>=0.27.0",
        "langchain-tavily>=0.2.17",
    )
    # Копируем файлы проекта в образ
    .add_local_file(PROJECT_ROOT / "criteria.json", "/app/criteria.json")
    .add_local_file(PROJECT_ROOT / "criteria_small.json", "/app/criteria_small.json")
    .add_local_file(
        PROJECT_ROOT / "criteria_deposit_freeze.json",
        "/app/criteria_deposit_freeze.json",
    )
    .add_local_file(
        PROJECT_ROOT / "criteria_deposit_freeze_small.json",
        "/app/criteria_deposit_freeze_small.json",
    )
    .add_local_dir(PROJECT_ROOT / "src", "/app/src")
)


@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name("openai"),  # OPENAI_API_KEY (если provider=openai)
        # modal.Secret.from_name("google"),  # GOOGLE_API_KEY (раскомментировать при MODEL_PROVIDER=gemini)
        modal.Secret.from_name("tavily"),  # TAVILY_API_KEY
        modal.Secret.from_name("telegram"),  # TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
    ],
    timeout=1800,  # 30 минут таймаут (агент может работать долго)
    schedule=modal.Cron("0 6 */3 * *"),  # Каждые 3 дня в 6:00 UTC = 9:00 MSK
)
async def run_periodic_check():
    """Проверка каждые 3 дня в 9:00 по Москве."""
    import os
    import sys
    from datetime import datetime
    
    # Переходим в директорию приложения
    os.chdir("/app")
    sys.path.insert(0, "/app/src")
    
    channel_id = os.getenv('TELEGRAM_CHANNEL_ID', 'не задан')
    admin_id = os.getenv('TELEGRAM_ADMIN_CHAT_ID', 'не задан')
    print(f"📤 Telegram канал: {channel_id}")
    print(f"👤 Админ-чат: {admin_id}")
    criteria_file = os.getenv("CRITERIA_FILE", "criteria.json")
    print(f"📄 Файл критериев: {criteria_file}")
    
    # Импортируем и запускаем бота (LangChain-версия)
    from lc_money_alert_bot import run_agent
    from bot_common import (
        format_telegram_report,
        send_telegram_report,
        notify_admin,
        Logger,
    )
    
    # Уведомление о старте
    await notify_admin("🚀 Начата генерация отчёта...")
    
    provider = os.getenv("MODEL_PROVIDER", "openai")

    with Logger() as logger:
        result = await run_agent(criteria_file, logger, provider=provider)
        report = format_telegram_report(result, result["stats"])
        
        if result and result.get("result"):
            score = result["result"].get("total_score", 0)
            risk_level = result["result"].get("risk_level", "unknown")
            stats = result["stats"]
            risk_map = {
                "green": ("🟢", "НИЗКИЙ"),
                "yellow": ("🟡", "СРЕДНИЙ"),
                "red": ("🔴", "ВЫСОКИЙ"),
            }
            risk_emoji, risk_label = risk_map.get(risk_level, ("❓", "НЕИЗВЕСТНО"))
            
            if score > 0:
                # Ненулевой риск — отправляем админу для ручной проверки
                print(f"⚠️ Обнаружен риск (очки: {score}) — отправка админу")
                admin_msg = (
                    f"Риск: {risk_emoji} {risk_label}, очки: {score}\n"
                    f"Время: {stats['time_seconds']:.0f}с, поисков: {stats['tool_calls']}\n"
                    f"Стоимость: ${stats['cost_usd']:.4f}\n\n"
                    f"--- ОТЧЁТ ---\n{report}"
                )
                await notify_admin(admin_msg, parse_mode="HTML")
            else:
                # Нулевой риск — публикуем в канал
                print("📤 Нулевой риск — публикация в канал")
                await send_telegram_report(report)
                await notify_admin(
                    f"✅ Пост опубликован\n"
                    f"Риск: {risk_emoji} {risk_label}, очки: {score}\n"
                    f"Время: {stats['time_seconds']:.0f}с, поисков: {stats['tool_calls']}\n"
                    f"Стоимость: ${stats['cost_usd']:.4f}"
                )
        else:
            await notify_admin("⚠️ Проверка завершена без результата")
    
    # Логируем результат
    if result and result.get("result"):
        risk_level = result["result"].get("risk_level", "unknown")
        score = result["result"].get("total_score", 0)
        print(f"✅ Проверка завершена: risk_level={risk_level}, score={score}")
    else:
        print("⚠️ Проверка завершена без структурированного результата")
    
    return result


@app.local_entrypoint()
def main_entrypoint():
    """Локальная точка входа для тестового запуска через `modal run`."""
    result = run_periodic_check.remote()
    print(f"Результат: {result}")


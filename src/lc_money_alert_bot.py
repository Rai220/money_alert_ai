"""
LangChain агент для макро-оценки экономики РФ (6 месяцев).
Поддерживает провайдеры: GigaChat, OpenAI, Gemini.

Запуск:
  uv run python src/lc_money_alert_bot.py                    # OpenAI (по умолчанию)
  uv run python src/lc_money_alert_bot.py --provider openai  # OpenAI (gpt-5.2)
  uv run python src/lc_money_alert_bot.py --provider gigachat # GigaChat (явно)
  uv run python src/lc_money_alert_bot.py --provider gemini  # Gemini (gemini-3.1-pro-preview)
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from html import unescape as _html_unescape

import httpx
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_gigachat import GigaChat
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langgraph.checkpoint.memory import InMemorySaver



# Добавляем src/ в path для импорта общих утилит
sys.path.insert(0, str(Path(__file__).parent))

from bot_common import (
    Logger,
    load_criteria,
    format_criteria_for_prompt,
    fetch_channel_archive,
    format_telegram_report,
    send_telegram_report,
    notify_admin,
    _extract_first_json_object,
    SYSTEM_PROMPT,
)

load_dotenv()


# ─────────────────────────── Tools ───────────────────────────


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


# Tavily — поисковый движок, оптимизированный для AI-агентов
# Важно: держим выдачу короткой, иначе раздуваем контекст.
_tavily = TavilySearch(max_results=_env_int("TAVILY_MAX_RESULTS", 6))


def _truncate_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n... (текст обрезан)"


def _extract_readable_text_from_html(html: str) -> str:
    """
    Best-effort извлечение читаемого текста из HTML без внешних зависимостей.
    Сильно уменьшает объём токенов по сравнению с сырым HTML.
    """
    if not html:
        return ""

    # Удаляем <script>/<style> блоки
    import re as _re

    s = _re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    s = _re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)

    # Явные переносы строк (br/p/li/tr)
    s = _re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _re.sub(r"(?i)</p\s*>", "\n", s)
    s = _re.sub(r"(?i)</li\s*>", "\n", s)
    s = _re.sub(r"(?i)</tr\s*>", "\n", s)

    # Убираем все теги
    s = _re.sub(r"(?s)<[^>]+>", " ", s)

    # HTML entities + схлопывание пробелов
    s = _html_unescape(s)
    s = _re.sub(r"[ \t\r\f\v]+", " ", s)
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


@tool
def WebSearch(query: str) -> str:
    """Поиск актуальных новостей в интернете. Принимает поисковый запрос, возвращает результаты поиска с заголовками, описаниями и ссылками."""
    try:
        # Жёстко ограничиваем размер выдачи, иначе история диалога раздувается
        # и следующее обращение к модели падает с context_length_exceeded.
        per_item_max = _env_int("WEBSEARCH_PER_ITEM_MAX_CHARS", 280)
        total_max = _env_int("WEBSEARCH_MAX_CHARS", 4_000)

        raw = _tavily.invoke({"query": query})
        # Tavily возвращает dict с ключом "results"
        if isinstance(raw, dict):
            results = raw.get("results", [])
        elif isinstance(raw, list):
            results = raw
        else:
            return str(raw)

        if not results:
            return "Результатов не найдено."

        formatted: list[str] = []
        for r in results:
            title = r.get("title", "")
            content = r.get("content", "") or r.get("snippet", "") or ""
            url = r.get("url", "")
            date = r.get("published_date") or r.get("date") or ""

            content = content.strip()
            if content:
                content = _truncate_text(content, per_item_max)

            # Стараемся быть компактными: title/date/url + очень короткий фрагмент.
            block: list[str] = []
            if title:
                block.append(f"Заголовок: {title}")
            if date:
                block.append(f"Дата: {date}")
            if content:
                block.append(f"Фрагмент: {content}")
            if url:
                block.append(f"URL: {url}")
            formatted.append("\n".join(block).strip())

        out = "\n\n---\n\n".join([b for b in formatted if b])
        return _truncate_text(out, total_max) if out else "Результатов не найдено."
    except Exception as e:
        return f"Ошибка поиска: {e}"


@tool
def WebFetch(url: str) -> str:
    """Загрузка содержимого веб-страницы по URL. Возвращает текстовое содержимое страницы."""
    try:
        # WebFetch почти всегда дорог по контексту — режем агрессивно.
        max_len = _env_int("WEBFETCH_MAX_CHARS", 8_000)

        with httpx.Client(
            follow_redirects=True,
            timeout=30.0,
            verify=False,
        ) as client:
            response = client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            response.raise_for_status()
            raw = response.text

            # По возможности превращаем HTML → читаемый текст (меньше токенов, больше пользы).
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/html" in content_type or "<html" in raw[:2000].lower():
                text = _extract_readable_text_from_html(raw)
            else:
                text = raw

            return _truncate_text(text, max_len)
    except Exception as e:
        return f"Ошибка загрузки страницы: {e}"


# ─────────────────────────── Agent ───────────────────────────


def _init_gigachat() -> GigaChat:
    """Инициализирует модель GigaChat из переменных окружения."""
    model_name = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")

    return GigaChat(
        model=model_name,
        timeout=120,
        max_tokens=8192,
        streaming=False,
        profanity_check=False,
    )


def _init_openai() -> ChatOpenAI:
    """Инициализирует модель OpenAI из переменных окружения."""
    model_name = os.getenv("OPENAI_MODEL", "gpt-5.2")
    kwargs: dict = {
        "model": model_name,
        "temperature": 0,
        "max_tokens": 8192,
    }
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _init_gemini() -> ChatGoogleGenerativeAI:
    """Инициализирует модель Gemini из переменных окружения."""
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")

    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0,
        max_output_tokens=8192,
    )


def _init_model(provider: str):
    """Создаёт LLM по имени провайдера."""
    factories = {
        "gigachat": _init_gigachat,
        "openai": _init_openai,
        "gemini": _init_gemini,
    }
    factory = factories.get(provider)
    if factory is None:
        supported = ", ".join(sorted(factories))
        raise ValueError(
            f"Неизвестный провайдер: {provider}. Допустимые: {supported}"
        )
    return factory()


def _estimate_cost(
    provider: str, input_tokens: int, output_tokens: int
) -> float:
    """Приблизительная оценка стоимости в USD (0 если тарифы не заданы)."""
    # Тарифы: USD за 1M токенов (input / output)
    rates_per_million: dict[str, dict[str, float]] = {
        "openai": {"input": 2.50, "output": 20.00},  # gpt-5.3 (оценка по тренду 5.1→5.2)
        "openai:gpt-5.2": {"input": 1.75, "output": 14.00},
        "openai:gpt-5.1": {"input": 1.25, "output": 10.00},
        "gemini": {"input": 2.00, "output": 12.00},  # gemini-3.1-pro-preview (<200K ctx)
        # GigaChat — отдельная модель тарификации, считаем 0
    }
    # Сначала пробуем точный ключ "provider:model", потом просто "provider"
    model_env_map = {
        "openai": "OPENAI_MODEL",
        "gemini": "GEMINI_MODEL",
    }
    model_name = os.getenv(model_env_map.get(provider, ""), "")
    r = rates_per_million.get(f"{provider}:{model_name}") or rates_per_million.get(provider)
    if not r:
        return 0.0
    return (input_tokens * r["input"] + output_tokens * r["output"]) / 1_000_000


# Промпты для продолжения поисков, если агент останавливается слишком рано
_CONTINUATION_PROMPTS = [
    (
        "СТОП! Ты сделал слишком мало поисков. "
        "Ты ОБЯЗАН провести реальные WebSearch поиски по каждой группе критериев. "
        "Начни с критичных (вес 20-12): события по госдолгу (ОФЗ), резкое ужесточение FX-контроля, "
        "банковские каникулы/остановка расчётов, крупные санации/лицензии. "
        "Используй WebSearch для поиска АКТУАЛЬНЫХ новостей по этим темам (приоритет 7-14 дней). "
    ),
    (
        "Продолжай поиски! Теперь проверь блоки макро и ДКП: "
        "инфляция/ожидания, меры и риторика ЦБ, межбанк/ликвидность, регулирование цен. "
        "Сформулируй отдельные WebSearch запросы по каждому блоку."
    ),
    (
        "Продолжай! Проверь реальную экономику и кредитный цикл: "
        "корпоративные дефолты/реструктуризации, ипотека/застройщики, просрочка населения, "
        "рынок труда/задержки зарплат, региональные финансы. "
        "Затем проверь внешние условия: экспорт/санкции/Urals/логистика/расчёты. "
        "После всех поисков — выдай финальный JSON."
    ),
    (
        "Ты уже провёл достаточно поисков. Теперь проведи глубокий анализ "
        "по инструкциям из системного промпта (секция 'ФИНАЛЬНЫЙ ШАГ: ГЛУБОКИЙ АНАЛИЗ') "
        "и выдай итоговый JSON-объект строго по формату."
    ),
]


async def _stream_agent_turn(
    agent,
    messages: list[dict],
    config: dict,
    *,
    log,
    step_count: int,
    tool_calls_count: int,
    total_input_tokens: int,
    total_output_tokens: int,
    start_time: datetime,
    search_limit: int,
) -> tuple[int, int, int, int, dict | None, str]:
    """
    Прогоняет один «турн» агента (astream) и возвращает обновлённую статистику.

    Returns:
        (step_count, tool_calls_count, total_input_tokens, total_output_tokens,
         final_result_or_none, last_assistant_text)
    """
    final_result = None
    last_assistant_text = ""

    async for chunk in agent.astream(
        {"messages": messages},
        config=config,
        stream_mode="updates",
    ):
        for step_name, data in chunk.items():
            for msg in data.get("messages", []):
                # ─── AIMessage: модель думает / вызывает инструменты ───
                if step_name == "model":
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        total_input_tokens += usage.get("input_tokens", 0)
                        total_output_tokens += usage.get("output_tokens", 0)

                    has_tool_calls = hasattr(msg, "tool_calls") and msg.tool_calls

                    if has_tool_calls:
                        step_count += 1
                        elapsed = (datetime.now() - start_time).total_seconds()
                        log("")
                        log(
                            f"📝 ШАГ {step_count} | "
                            f"🔍 {tool_calls_count}/{search_limit} | "
                            f"⏱️ {elapsed:.0f}с"
                        )

                        for tc in msg.tool_calls:
                            tool_calls_count += 1
                            t_name = tc.get("name", "unknown")
                            t_args = tc.get("args", {})

                            if t_name == "WebSearch":
                                log(
                                    f"   🔧 #{tool_calls_count} WebSearch: "
                                    f"{t_args.get('query', '')[:60]}..."
                                )
                            elif t_name == "WebFetch":
                                url = t_args.get("url", "")
                                log(
                                    f"   🔧 #{tool_calls_count} WebFetch: "
                                    f"{url[:80]}"
                                )
                            else:
                                log(f"   🔧 #{tool_calls_count} {t_name}")

                        text = _msg_text(msg)
                        if text:
                            last_assistant_text = text
                            _log_text_preview(log, text, step_count)

                    else:
                        # Финальный ответ модели (без tool_calls)
                        text = _msg_text(msg)
                        if text:
                            step_count += 1
                            elapsed = (datetime.now() - start_time).total_seconds()
                            log("")
                            log(
                                f"📝 ШАГ {step_count} (ответ) | "
                                f"🔍 {tool_calls_count}/{search_limit} | "
                                f"⏱️ {elapsed:.0f}с"
                            )
                            last_assistant_text = text
                            _log_text_preview(log, text, step_count)

                            # Ищем финальный JSON
                            if final_result is None:
                                extracted = _extract_first_json_object(text)
                                if extracted:
                                    log("   🎯 Извлечён JSON из ответа модели!")
                                    final_result = extracted

    return (
        step_count,
        tool_calls_count,
        total_input_tokens,
        total_output_tokens,
        final_result,
        last_assistant_text,
    )


async def run_agent(
    criteria_path: str = "criteria.json",
    logger: Logger | None = None,
    *,
    provider: str = "openai",
) -> dict:
    """Запускает LangChain агента для анализа критериев."""

    provider_labels = {
        "gigachat": "GigaChat",
        "openai": "OpenAI",
        "gemini": "Gemini",
    }
    provider_label = provider_labels.get(provider, provider)

    def log(msg: str, to_console: bool = True):
        if logger:
            logger.log(msg, to_console)
        elif to_console:
            print(msg)

    telegram_id = os.getenv("TELEGRAM_CHANNEL_ID", "не задан")
    log("=" * 60)
    log(f"🤖 Агент макро-рисков РФ (LangChain + {provider_label})")
    log(f"📤 Telegram: {telegram_id}")
    log("=" * 60)

    # ── Критерии ──
    criteria_data = load_criteria(criteria_path)
    criteria_text = format_criteria_for_prompt(criteria_data)
    log(f"📋 Загружено критериев: {len(criteria_data['criteria'])}")

    # ── Настройки ──
    search_limit = 50
    max_turns = int(os.getenv("MAX_TURNS", "100"))

    moscow_tz = ZoneInfo("Europe/Moscow")
    now_msk = datetime.now(moscow_tz)
    today_date = now_msk.strftime("%d.%m.%Y, %H:%M МСК")

    prompt = SYSTEM_PROMPT.format(
        criteria=criteria_text,
        search_limit=search_limit,
        today_date=today_date,
    )

    # ── Модель ──
    model = _init_model(provider)
    if provider == "gigachat":
        model_display = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")
        log(f"🤖 Модель: {model_display} (GigaChat)")
        log(f"🔗 Base URL: {os.getenv('GIGACHAT_BASE_URL', 'default')}")
    elif provider == "openai":
        model_display = os.getenv("OPENAI_MODEL", "gpt-5.2")
        log(f"🤖 Модель: {model_display} (OpenAI)")
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            log(f"🔗 Base URL: {base_url}")
    elif provider == "gemini":
        model_display = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
        log(f"🤖 Модель: {model_display} (Gemini)")
    else:
        log(f"🤖 Провайдер: {provider}")

    # ── Агент с checkpointer для продолжения разговора ──
    checkpointer = InMemorySaver()
    agent = create_agent(
        model=model,
        tools=[WebSearch, WebFetch],
        system_prompt=prompt,
        checkpointer=checkpointer,
    )

    # ── Предзагрузка архива канала ──
    log("📰 Загрузка архива канала...")
    archive_text, archive_count = fetch_channel_archive()
    if archive_count > 0:
        log(f"   ✅ Архив загружен: {archive_count} постов, {len(archive_text):,} символов")
    else:
        log(f"   ⚠️ Не удалось загрузить архив: {archive_text}")

    # ── Статистика ──
    step_count = 0
    tool_calls_count = 0
    start_time = datetime.now()
    final_result = None
    total_input_tokens = 0
    total_output_tokens = 0

    log(f"🔍 Запуск агента (лимит поисков: {search_limit})")
    log("-" * 60)

    # recursion_limit: каждый «ход» агента = model + tools ≈ 2 supersteps
    config = {
        "recursion_limit": max_turns * 3,
        "configurable": {"thread_id": "analysis-run"},
    }

    # Пользовательское сообщение с архивом (модель следует системному промпту)
    user_message = "Начни анализ критериев риска."
    if archive_count > 0:
        user_message += (
            f"\n\n=== АРХИВ КАНАЛА ({archive_count} постов) ===\n\n"
            f"{archive_text}"
        )

    # ── Основной цикл с продолжениями ──
    max_continuations = len(_CONTINUATION_PROMPTS)
    did_recover_context = False

    for turn_idx in range(max_continuations + 1):
        if turn_idx == 0:
            messages = [{"role": "user", "content": user_message}]
        else:
            # Продолжение: агент остановился слишком рано
            prompt_idx = min(turn_idx - 1, len(_CONTINUATION_PROMPTS) - 1)
            cont_msg = _CONTINUATION_PROMPTS[prompt_idx]
            messages = [{"role": "user", "content": cont_msg}]
            log("")
            log(f"🔄 Продолжение #{turn_idx}: поисков {tool_calls_count}")

        try:
            (
                step_count,
                tool_calls_count,
                total_input_tokens,
                total_output_tokens,
                turn_result,
                last_text,
            ) = await _stream_agent_turn(
                agent,
                messages,
                config,
                log=log,
                step_count=step_count,
                tool_calls_count=tool_calls_count,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                start_time=start_time,
                search_limit=search_limit,
            )

            if logger and last_text:
                logger.last_assistant_text = last_text

            if turn_result is not None:
                final_result = turn_result
                log("   ✅ JSON получен — принимаем результат")
                break
            elif turn_idx == max_continuations:
                break

        except Exception as e:
            log(f"❌ Ошибка в турне #{turn_idx}: {e}")

            # Если переполнили контекст, последующие продолжения на том же thread_id
            # будут падать сразу. Переключаемся на новый thread_id и даём модели
            # короткую «передачу смены» (без огромной истории инструментов).
            s = str(e)
            if (
                ("context_length_exceeded" in s)
                or ("Input tokens exceed" in s)
                or ("configured limit" in s and "tokens" in s)
            ):
                did_recover_context = True
                new_thread_id = f"analysis-run-recover-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{turn_idx}"
                config["configurable"]["thread_id"] = new_thread_id

                handoff_max = _env_int("CONTEXT_RECOVERY_HANDOFF_MAX_CHARS", 8_000)
                handoff = ""
                if logger and getattr(logger, "last_assistant_text", ""):
                    handoff = _truncate_text(logger.last_assistant_text, handoff_max)

                messages = [
                    {
                        "role": "user",
                        "content": (
                            "Предыдущая попытка упёрлась в лимит контекста. Продолжаем в новом чате.\n\n"
                            "КРАТКИЙ КОНТЕКСТ (сокращено):\n"
                            f"{handoff}\n\n"
                            "Сделай ещё WebSearch (если нужно), "
                            "проверь критерии и выдай ТОЛЬКО финальный JSON строго по формату."
                        ),
                    }
                ]

                try:
                    (
                        step_count,
                        tool_calls_count,
                        total_input_tokens,
                        total_output_tokens,
                        turn_result,
                        last_text,
                    ) = await _stream_agent_turn(
                        agent,
                        messages,
                        config,
                        log=log,
                        step_count=step_count,
                        tool_calls_count=tool_calls_count,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                        start_time=start_time,
                        search_limit=search_limit,
                    )
                    if logger and last_text:
                        logger.last_assistant_text = last_text
                    if turn_result is not None:
                        final_result = turn_result
                        log("   ✅ JSON получен — принимаем результат")
                        break
                except Exception as e2:
                    log(f"❌ Ошибка восстановления контекста: {e2}")

            if turn_idx == max_continuations:
                break

    # ── Финализация: если JSON так и не получен ──
    if final_result is None:
        log("⚠️ Финальный JSON не получен — запускаю финализацию...")
        try:
            finalize_config = dict(config)
            if did_recover_context:
                finalize_config = {
                    **config,
                    "configurable": {
                        **config.get("configurable", {}),
                        "thread_id": f"analysis-run-finalize-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                    },
                }
            finalize_result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Выдай ТОЛЬКО финальный JSON-объект без markdown-обёртки "
                                "и без комментариев. БУДЬ КРАТКИМ — отчёт для Telegram.\n"
                                '{"checked_criteria": [...], "triggered_criteria": [...], '
                                '"total_score": 0, "risk_level": "green", "confidence": "high/medium/low", "deposit_access_risk":"green", '
                                '"summary": "1-2 предложения макс 220 символов", '
                                '"recommendation": "1 предложение макс 170 символов", '
                                '"asset_guidance": ["пункт до 140 символов","пункт до 140 символов","пункт до 140 символов"], '
                                '"positive_trends": ["тезис до 140 символов", "тезис до 140 символов"], '
                                '"negative_trends": ["тезис до 140 символов", "тезис до 140 символов"], '
                                '"key_risks_6m": ["тезис до 140 символов", "тезис до 140 символов"], '
                                '"watchlist": ["индикатор до 120 символов", "индикатор до 120 символов", "индикатор до 120 символов"], '
                                '"sources": [{"id": 1, "url": "https://..."}]}'
                            ),
                        }
                    ]
                },
                config=finalize_config,
            )
            last_msg = finalize_result["messages"][-1]
            text = _msg_text(last_msg)
            if text:
                extracted = _extract_first_json_object(text)
                if extracted:
                    final_result = extracted
                    log("   🎯 Извлечён JSON из финализации!")

            usage = getattr(last_msg, "usage_metadata", None)
            if usage:
                total_input_tokens += usage.get("input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)
        except Exception as e:
            log(f"❌ Ошибка финализации: {e}")

    # ── Итоги ──
    total_time = (datetime.now() - start_time).total_seconds()
    total_cost = _estimate_cost(provider, total_input_tokens, total_output_tokens)

    log("")
    log("=" * 60)
    log("📊 ИТОГОВЫЙ ОТЧЁТ")
    log("=" * 60)

    if final_result:
        risk_map = {
            "green": ("🟢", "НИЗКИЙ"),
            "yellow": ("🟡", "СРЕДНИЙ"),
            "red": ("🔴", "ВЫСОКИЙ"),
        }
        emoji, label = risk_map.get(
            final_result.get("risk_level"), ("❓", "НЕИЗВЕСТНО")
        )

        log(f"Риск кризисного сценария (6м): {emoji} {label}")
        log(f"Очки: {final_result.get('total_score', '?')}")
        conf = final_result.get("confidence")
        if conf:
            log(f"Уверенность: {conf}")
        dep = final_result.get("deposit_access_risk")
        if dep:
            dep_emoji, dep_label = risk_map.get(dep, ("❓", "НЕИЗВЕСТНО"))
            log(f"Риск доступа к вкладам (1–3м): {dep_emoji} {dep_label}")

        triggered = final_result.get("triggered_criteria", [])
        if triggered:
            log("")
            log("⚠️ Сработавшие критерии:")
            for t in triggered:
                evidence = t.get("evidence", "")
                log(f"   • {t.get('id', '?')}: {evidence}")
            log("")
        else:
            log("✅ Сработавших критериев нет")

        log(f"📝 Резюме: {final_result.get('summary', 'Нет данных')}")
        log(f"💡 Рекомендация: {final_result.get('recommendation', 'Нет данных')}")

        asset_guidance = final_result.get("asset_guidance", []) or []
        if asset_guidance:
            log("")
            log("💼 Активы:")
            for item in asset_guidance:
                log(f"   • {item}")

        positive = final_result.get("positive_trends", []) or []
        negative = final_result.get("negative_trends", []) or []
        risks_6m = final_result.get("key_risks_6m", []) or []
        watchlist = final_result.get("watchlist", []) or []

        if positive:
            log("")
            log("🟢 Позитивные тенденции:")
            for item in positive:
                log(f"   • {item}")

        if negative:
            log("")
            log("🔴 Негативные тенденции:")
            for item in negative:
                log(f"   • {item}")

        if risks_6m:
            log("")
            log("⚠️ Риски на 6 месяцев:")
            for item in risks_6m:
                log(f"   • {item}")

        if watchlist:
            log("")
            log("👀 Watchlist:")
            for item in watchlist:
                log(f"   • {item}")

        # Backward compatibility
        key_insights = final_result.get("key_insights", [])
        if (not positive and not negative and not risks_6m and not watchlist) and key_insights:
            log("")
            log("📝 Тезисы:")
            for insight in key_insights:
                log(f"   • {insight}")

        hidden_risks = final_result.get("hidden_risks")
        if hidden_risks and (not positive and not negative and not risks_6m):
            log(f"🔍 На заметку: {hidden_risks}")
    else:
        log("⚠️ Не удалось получить результат от агента")
        if logger and logger.last_assistant_text:
            log(
                f"[DEBUG] last text: {logger.last_assistant_text[:500]}",
                to_console=False,
            )

    log("")
    log("-" * 60)
    log(
        f"⏱️ Время: {total_time:.0f}с | "
        f"📝 Шагов: {step_count} | "
        f"🔍 Поисков: {tool_calls_count}"
    )
    log(f"📊 Токены: {total_input_tokens:,} in / {total_output_tokens:,} out")
    # Добавляем модель рядом со стоимостью
    if total_cost > 0:
        log(f"💵 Стоимость: ${total_cost:.4f} | 🤖 Модель: {provider}:{model_display}")
    else:
        log(
            f"💵 Стоимость: н/д (тарифы для {provider} не заданы) | "
            f"🤖 Модель: {provider}:{model_display}"
        )
    log("=" * 60)

    return {
        "result": final_result,
        "stats": {
            "steps": step_count,
            "tool_calls": tool_calls_count,
            "time_seconds": total_time,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "model": f"{provider}:{model_display}",
        },
    }


# ─────────────────────────── Helpers ─────────────────────────


def _msg_text(msg) -> str:
    """Извлекает текстовое содержимое из сообщения LangChain."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


def _log_text_preview(log, text: str, step_count: int):
    """Логирует превью текста (первые 5 строк в консоль, полный — в файл)."""
    lines = text.strip().split("\n")[:5]
    for line in lines:
        if line.strip():
            log(f"   {line[:120]}")
    if len(text.strip().split("\n")) > 5:
        log("   ... (ещё строки)")
    log(f"\n--- ТЕКСТ ШАГА {step_count} ---\n{text}\n---\n", to_console=False)


# ─────────────────────────── Main ────────────────────────────


async def main():
    """Точка входа."""
    parser = argparse.ArgumentParser(
        description="Агент макро-рисков РФ (LangChain)"
    )
    parser.add_argument(
        "--provider",
        choices=["gigachat", "openai", "gemini"],
        default=os.getenv("MODEL_PROVIDER", "openai"),
        help="Провайдер LLM (по умолчанию: openai, или из MODEL_PROVIDER)",
    )
    args = parser.parse_args()

    provider_labels = {"gigachat": "GigaChat", "openai": "OpenAI", "gemini": "Gemini"}
    provider_label = provider_labels.get(args.provider, args.provider)

    with Logger() as logger:
        logger.log(f"📁 Лог: {logger.log_file}")

        await notify_admin(
            f"🚀 Начата генерация отчёта (LangChain + {provider_label})..."
        )

        criteria_file = os.getenv("CRITERIA_FILE", "criteria.json")
        logger.log(f"📄 Файл критериев: {criteria_file}")

        try:
            result = await run_agent(
                criteria_file, logger, provider=args.provider
            )
        except Exception as e:
            error_detail = (
                logger.last_assistant_text.strip()
                if logger.last_assistant_text
                else str(e)
            )
            error_msg = f"❌ Агент упал с ошибкой:\n\n{error_detail}"
            logger.log(error_msg)
            await notify_admin(error_msg)
            return None

        logger.log("")
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
                logger.log(
                    f"⚠️ Обнаружен риск (очки: {score}) — отправка админу для проверки"
                )
                admin_msg = (
                    f"Риск: {risk_emoji} {risk_label}, очки: {score}\n"
                    f"Время: {stats['time_seconds']:.0f}с, "
                    f"поисков: {stats['tool_calls']}\n\n"
                    f"--- ОТЧЁТ ---\n{report}"
                )
                await notify_admin(admin_msg, parse_mode="HTML")
            else:
                logger.log("📤 Нулевой риск — публикация в канал...")
                await send_telegram_report(report)
                await notify_admin(
                    f"✅ Пост опубликован (LangChain + {provider_label})\n"
                    f"Риск: {risk_emoji} {risk_label}, очки: {score}\n"
                    f"Время: {stats['time_seconds']:.0f}с, "
                    f"поисков: {stats['tool_calls']}"
                )

        logger.log(f"📁 Лог сохранён: {logger.log_file}")
        return result


if __name__ == "__main__":
    asyncio.run(main())

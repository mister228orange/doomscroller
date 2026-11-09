# AGENTS.md — памятка для любых агентов на этой машине

Факты о машине и правила работы. Читай перед тем, как что-то запускать.

## Машина

- **ОС: Ubuntu 24.04.5 LTS (Noble Numbat)**, ядро 7.0.0-42-generic. Владелец машины
  просил это запомнить: все пути, команды и сервисы — Linux-специфичные, не выдумывай
  macOS/Windows-варианты.
- Репозиторий doomscroller: `/home/onemanarmy/Documents/doomscroller`, виртуальное
  окружение `.venv/bin/python`.
- VS Code установлен как **snap** — его CLI из песочницы не работает
  (`snap-confine ... cap_dac_override`); пуш через VS Code делает владелец кнопкой
  Sync Changes. Cursor установлен (`cursor-agent` работает).

## doomscroller: окружение и правила

- База: PostgreSQL из compose — внутри compose-сети `db:5432`. **Проброс
  `127.0.0.1:5434` на этой машине сломан** (см. ниже). Хостовые процессы (CLI, скорер)
  получают `DATABASE_URL` с `127.0.0.1:5434`; в `.env` он уже такой.
- **⚠️ Опубликованные docker-порты не доставляют данные.** TCP-соединение
  устанавливается, SYN/FIN проходят, но ни один байт не доходит до клиента (проверено
  на всех портах: 5432, 5434, 5435, 8080, 8000, 8090). Хостовые процессы отвечают
  нормально (3080, 22, 11434). Перезапуск демона `systemctl restart docker` **не лечит**
  (и убивает docker-proxy до пересоздания контейнеров); вероятно нужен ребут хоста.
  **Обход:** запускать MCP внутри compose-сети —
  `docker compose run --rm --no-deps -T -e DATABASE_URL=...@db:5432/... doomscroller python -m doomscroller mcp --transport stdio`.
  Проверка: `docker compose exec -T db pg_isready` (внутри здоров) vs
  `nc -z -w 3 127.0.0.1 5434` (с хоста порт открыт, но данные не идут).
- **`.venv` в репо может быть сломан**: симлинк `bin/python` указывает на несуществующий
  интерпретатор. Лечение: `python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"`
  (uv sync падает в песочнице: `Read-only file system` на `~/.cache/uv`).
- **Одна сессия Telethon — один процесс.** Файлы сессий лежат в корне репо
  (`scroller_session.session`, `scroller_session_mcp.session`, gitignored). Два процесса
  на одном файле с разными IP = Telegram отзывает ключ навсегда
  (`AuthKeyDuplicatedError`) — уже случалось. Логин: `python -m doomscroller login`
  (сам спросит код и пароль 2FA).
- LLM — NanoGPT (`https://nano-gpt.com/api/v1`), ключ в `.env` (`SCORER_API_KEY`).
  Маршруты NanoGPT флапают: одна и та же модель в разные часы отвечает с reasoning и без
  (`reasoning_content` съедает тысячи токенов). Для сравнимых прогонов пин
  `SCORER_REASONING_EFFORT=none`.
- **Скоринг по умолчанию сэмплирует 1-из-5 постов** (`SCORER_SAMPLE_EVERY=5`) — так
  распорядился владелец («дорого»). Полное чтение — `--sample-every 1` или слово
  «детальнее» от владельца. `--force` и пересчёты выборку не соблюдают.
- **NanoGPT держит залог за каждый запрос** по оценке «до 100 000 выходных токенов» (~$0.2).
  При маленьком балансе крошечный ping проходит, а скоринг падает в `HTTP 402 Insufficient
  balance`. Лечится `SCORER_MAX_TOKENS=2000` — залог считается от него, и вызов проходит.
- Зависшие запросы к NanoGPT реальны: с дефолтным `SCORER_TIMEOUT=600` один зависший вызов
  блокирует пост на 10 минут. Для прогонов держим `SCORER_TIMEOUT=90`, `SCORER_RETRIES=1`.
- Тесты: `.venv/bin/python -m pytest tests/ -q` (SQLite-фолбэк работает без БД).

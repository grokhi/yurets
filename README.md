# Юрец ФМ

Минималистичный прототип интернет-радио: одна страница, один непрерывный аудиострим, классический веб-подход.

## Быстрый старт (Docker)

1) Подготовьте env-файл:

- Скопируйте `.env.example` в `.env`
- Положите хотя бы один файл `.mp3` в папку `./music` (или `.ogg/.opus`, если выберете `audio/ogg`)

2) Запуск:

```bash
docker compose up --build
```

3) Откройте в браузере:

- http://localhost:8000

## Локальный дебаг (VS Code)

В репозитории есть конфиг отладки VS Code: `.vscode/launch.json` (конфигурация `agent`).

1) Создайте виртуальное окружение и установите зависимости через Poetry:

```bash
poetry install
```

2) Подготовьте env-файл для дебага:

- Скопируйте `.env.example` в `src/.env` (он используется как `envFile` в `launch.json`)
- При необходимости поправьте пути (например `YURETS_LOCAL_MUSIC_DIR`)

3) Запустите конфигурацию **Run and Debug → agent**.

## Endpoints

- `GET /` — главная страница (одна `index.html`)
- `GET /stream` — непрерывный поток (chunked HTTP)
- `GET /api/now-playing` — информация о текущем треке
- `GET /health` — healthcheck

## Источники музыки

Сейчас реализованы два источника:

1) **local** — локальная папка с музыкой (`YURETS_LOCAL_MUSIC_DIR`)
2) **telegram** — один Telegram-канал (архитектурно готово к нескольким)

### Telegram настройки

Для работы Telegram-источника заполните:

- `YURETS_TELEGRAM_API_ID`
- `YURETS_TELEGRAM_API_HASH`
- `YURETS_TELEGRAM_BOT_TOKEN` (рекомендуется для Docker)
- `YURETS_TELEGRAM_CHANNEL`

Сессия Telethon хранится в `YURETS_TELEGRAM_SESSION` (в docker-compose монтируется `./telegram_session`).

## Расписание вещания

Расписание задаётся JSON-строкой в `YURETS_SCHEDULE_JSON`.

Пример:

```env
YURETS_SCHEDULE_JSON=[{"start":"00:00","end":"08:00","source":"telegram"},{"start":"08:00","end":"18:00","source":"local"},{"start":"18:00","end":"00:00","source":"telegram"}]
```

Источник выбирается между треками (не посреди одного файла).

## Важно про автозапуск аудио

В `index.html` стоит `autoplay`, но некоторые браузеры блокируют автозапуск звука без взаимодействия пользователя.

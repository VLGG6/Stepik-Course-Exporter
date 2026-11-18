# Stepik Course Exporter

Одностраничное Streamlit-приложение для локального экспорта курсов Stepik в Markdown и JSON.

## Установка через pipx (рекомендуется)

```bash
pipx install /path/to/Parser
# или из папки проекта:
pipx install .
```

После установки становится доступна команда:

```bash
stepik-exporter
```

Она запустит Streamlit-сервер на `http://localhost:8501` (режим headless).

## Настройка секретов

1. Получите `CLIENT_ID` и `CLIENT_SECRET` на Stepik: https://stepik.org/oauth2/applications/
2. Создайте файл `.env` в папке, **откуда запускаете команду**:

```bash
cp .env.example .env
# отредактируйте .env, впишите свои ключи
```

Или задайте переменные окружения:

```bash
export STEPIK_CLIENT_ID=xxx
export STEPIK_CLIENT_SECRET=yyy
```

## Использование

1. Запустите: `stepik-exporter`
2. В открывшемся браузере введите ссылку на курс Stepik или его ID
3. Нажмите «Получить информацию о курсе» для предпросмотра
4. Нажмите «Экспортировать курс» для сохранения в `./exports`

## Разработка (без pipx)

```bash
pip install -e .
streamlit run app.py
```

## Структура экспорта

```
exports/
└── course_<id>_<title>/
    ├── course.json
    ├── modules/
    │   ├── module_<id>_<title>/
    │   │   ├── module.json
    │   │   ├── lessons/
    │   │   │   ├── lesson_<id>_<title>.md
    │   │   │   └── lesson_<id>_<title>.json
    │   │   └── ...
    │   └── ...
    └── errors.log (если были ошибки)
```

## Зависимости

- Python 3.9+
- streamlit
- requests
- python-dotenv

## Лицензия

MIT
 
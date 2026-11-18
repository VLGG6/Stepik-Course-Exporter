"""
Stepik Course Exporter
======================
Одностраничное Streamlit-приложение для локального экспорта курсов Stepik.

Установка зависимостей:
    pip install streamlit requests python-dotenv

Создание .env (рядом с app.py):
    cp .env.example .env
    # затем впишите свои STEPIK_CLIENT_ID и STEPIK_CLIENT_SECRET

Запуск:
    streamlit run app.py

Секреты (CLIENT_ID / CLIENT_SECRET) читаются из переменных окружения или .env.
Поля в sidebar — необязательный резервный способ ввода. Секреты не пишутся
в JSON курса и не выводятся в логи.
"""

from __future__ import annotations

# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "streamlit",
#   "requests",
#   "python-dotenv",
# ]
# ///
import streamlit as st
import requests
from dotenv import load_dotenv

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Константы и логирование
# --------------------------------------------------------------------------- #

# Загружаем переменные окружения из .env:
# 1) рядом с app.py (streamlit run app.py)
# 2) из текущей рабочей директории (stepik-exporter запускается из папки проекта)
for _env_path in (Path(__file__).parent / ".env", Path.cwd() / ".env"):
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path)
        break

STEPIK_API_BASE = "https://stepik.org/api"
STEPIK_TOKEN_URL = "https://stepik.org/oauth2/token/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("stepik_exporter")


# --------------------------------------------------------------------------- #
# OAuth2 и HTTP
# --------------------------------------------------------------------------- #

def get_access_token(client_id: str, client_secret: str, retries: int = 3) -> Optional[str]:
    """Получить OAuth2 access token (Client Credentials Flow).

    POST https://stepik.org/oauth2/token/ с grant_type=client_credentials.

    Args:
        client_id: Stepik Client ID.
        client_secret: Stepik Client Secret.
        retries: Количество попыток при сетевых ошибках.

    Returns:
        Access token или None при ошибке.
    """
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    for attempt in range(retries):
        try:
            resp = requests.post(STEPIK_TOKEN_URL, data=payload, timeout=20)
            if resp.status_code == 200:
                return resp.json().get("access_token")
            if resp.status_code == 401:
                logger.error("Неверные Stepik credentials (401)")
                return None
            logger.warning("Token request: HTTP %s (попытка %s)", resp.status_code, attempt + 1)
        except requests.RequestException as exc:
            logger.warning("Сетевая ошибка токена: %s (попытка %s)", exc, attempt + 1)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return None


def api_get(
    endpoint: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
    delay: float = 0.3,
    retries: int = 3,
) -> Optional[Dict[str, Any]]:
    """GET запрос к Stepik API с Bearer-токеном, повторами и задержкой.

    При ответе 401 возвращает None — вызывающий код должен обновить токен.

    Args:
        endpoint: Путь относительно API (например "courses/1") или полный URL.
        token: Access token.
        params: Query-параметры.
        delay: Задержка после успешного запроса, сек.
        retries: Количество попыток.

    Returns:
        Разобранный JSON или None при ошибке.
    """
    url = endpoint if endpoint.startswith("http") else f"{STEPIK_API_BASE}/{endpoint.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                time.sleep(delay)
                return resp.json()
            if resp.status_code == 401:
                logger.warning("Токен истёк (401)")
                return None
            if resp.status_code == 404:
                logger.warning("404: %s", url)
                return None
            if resp.status_code == 429:
                logger.warning("Rate limit (429), ждём")
                time.sleep(2 + attempt)
                continue
            logger.warning("API %s: HTTP %s", url, resp.status_code)
        except requests.RequestException as exc:
            logger.warning("Сетевая ошибка API %s: %s", url, exc)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return None


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def extract_course_id(url_or_id: str) -> Optional[int]:
    """Извлечь course_id из URL вида https://stepik.org/course/{id}/ или из числа.

    Args:
        url_or_id: Ссылка или числовой ID курса.

    Returns:
        course_id или None, если распознать не удалось.
    """
    value = (url_or_id or "").strip()
    if value.isdigit():
        return int(value)
    match = re.search(r"course/(\d+)", value)
    if match:
        return int(match.group(1))
    return None


def safe_filename(name: str, max_len: int = 100, default: str = "unknown") -> str:
    """Очистить имя от запрещённых символов и обрезать до max_len.

    Args:
        name: Исходная строка (заголовок раздела/урока/шага).
        max_len: Максимальная длина результата.
        default: Значение при пустом результате.

    Returns:
        Безопасное имя файла/папки.
    """
    cleaned = re.sub(r'[/\\:*?"<>|\n\r\t]+', "_", name or "")
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip("._") + "..."
    return cleaned or default


# --------------------------------------------------------------------------- #
# Получение данных Stepik (по id-связям объекта)
# --------------------------------------------------------------------------- #

def _fetch_by_ids(
    resource: str,
    ids: List[int],
    token: str,
    delay: float,
    client_id: str,
    client_secret: str,
) -> List[Dict[str, Any]]:
    """Получить объекты Stepik по списку id с поддержкой пагинации.

    Использует батч-запрос /api/{resource}?ids[]=... и сохраняет исходный порядок id.

    Args:
        resource: Имя ресурса ("sections", "units", "lessons", "steps").
        ids: Список идентификаторов в нужном порядке.
        token: Access token.
        delay: Задержка между запросами.
        client_id: Client ID (для обновления токена).
        client_secret: Client Secret.

    Returns:
        Список объектов в порядке ids (пропущенные недоступные id опускаются).
    """
    if not ids:
        return []
    collected: Dict[int, Dict[str, Any]] = {}
    page = 1
    while True:
        params: List[tuple] = [("ids[]", i) for i in ids] + [("page", page)]
        data = api_get(resource, token, params=params, delay=delay)
        if data is None:
            # токен мог истечь — обновляем и повторяем один раз
            new_token = get_access_token(client_id, client_secret)
            if new_token and new_token != token:
                token = new_token
                data = api_get(resource, token, params=params, delay=delay)
        if not data:
            break
        for item in data.get(resource, []):
            collected[item["id"]] = item
        meta = data.get("meta") or {}
        if meta.get("has_next"):
            page = int(meta.get("page", page)) + 1
        else:
            break
    return [collected[i] for i in ids if i in collected]


def get_course(course_id: int, token: str, delay: float = 0.3) -> Optional[Dict[str, Any]]:
    """Получить курс: GET /api/courses/{course_id}."""
    data = api_get(f"courses/{course_id}", token, delay=delay)
    if data and data.get("courses"):
        return data["courses"][0]
    return None


def get_sections(course: Dict[str, Any], token: str, delay: float, client_id: str, client_secret: str) -> List[Dict[str, Any]]:
    """Получить разделы курса по course["sections"] (список id)."""
    ids = course.get("sections") or []
    return _fetch_by_ids("sections", ids, token, delay, client_id, client_secret)


def get_units(section: Dict[str, Any], token: str, delay: float, client_id: str, client_secret: str) -> List[Dict[str, Any]]:
    """Получить units раздела по section["units"] (список id)."""
    ids = section.get("units") or []
    return _fetch_by_ids("units", ids, token, delay, client_id, client_secret)


def get_lessons(unit: Dict[str, Any], token: str, delay: float, client_id: str, client_secret: str) -> List[Dict[str, Any]]:
    """Получить lesson(ы) для unit по unit["lesson"] (одиночный id)."""
    lesson_id = unit.get("lesson")
    if not lesson_id:
        return []
    return _fetch_by_ids("lessons", [lesson_id], token, delay, client_id, client_secret)


def get_steps(lesson: Dict[str, Any], token: str, delay: float, client_id: str, client_secret: str) -> List[Dict[str, Any]]:
    """Получить шаги урока по lesson["steps"] (список id)."""
    ids = lesson.get("steps") or []
    return _fetch_by_ids("steps", ids, token, delay, client_id, client_secret)


def check_api_connection(client_id: str, client_secret: str) -> bool:
    """Проверить подключение: получить токен и сделать тестовый запрос."""
    token = get_access_token(client_id, client_secret)
    if not token:
        return False
    return api_get("courses", token, params={"page": 1}) is not None


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

def _convert_html_images(text: str) -> str:
    """Превратить <img src=...> в Markdown ![alt](url). Ссылки не скачиваются."""
    pattern = re.compile(
        r"<img[^>]*?src=[\"']([^\"']+)[\"'][^>]*?>",
        re.IGNORECASE,
    )

    def repl(match: re.Match) -> str:
        src = match.group(1)
        alt_match = re.search(r"alt=[\"']([^\"']*)[\"']", match.group(0), re.IGNORECASE)
        alt = alt_match.group(1) if alt_match else ""
        return f"![{alt}]({src})"

    return pattern.sub(repl, text)


def _extract_title(text: str) -> Optional[str]:
    """Взять первый Markdown-заголовок из текста (если есть)."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


def step_to_markdown(
    step: Dict[str, Any],
    unit_id: int,
    lesson_id: int,
    section_id: int,
    step_index: int,
) -> str:
    """Конвертировать step Stepik в Markdown-строку.

    Изображения — строго ![alt](url), видео — строго [Название](url).
    Медиа не скачиваются, сохраняются оригинальные ссылки.

    Args:
        step: Объект шага из API (содержит поле "block").
        unit_id: ID unit.
        lesson_id: ID lesson.
        section_id: ID section.
        step_index: Позиция шага (1-based).

    Returns:
        Текст Markdown для файла шага.
    """
    block = step.get("block")
    if not isinstance(block, dict):
        block = {}
    block_type = block.get("name") or step.get("step_type") or "unknown"
    raw_text = block.get("text") or ""
    raw_text = _convert_html_images(raw_text)

    heading = _extract_title(raw_text)
    body = raw_text.strip()
    if heading:
        # убираем дублирующий заголовок из тела
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                lines.pop(i)
                break
        body = "\n".join(lines).strip()

    parts: List[str] = [f"# {heading or f'Шаг {step_index}'}"]

    if body:
        parts.append(body)

    # Варианты ответов для тестовых шагов
    options = block.get("options") or []
    if block_type in ("choice", "matching", "sorting") and options:
        lines = ["**Варианты ответов:**"]
        for idx, opt in enumerate(options, 1):
            text = (opt.get("text") or "").strip()
            mark = " ✅" if opt.get("is_correct") else ""
            lines.append(f"{idx}. {text}{mark}")
        parts.append("\n".join(lines))

    # Кодовые шаблоны
    templates = block.get("code_templates") or block.get("code") or {}
    if isinstance(templates, dict):
        for lang, code in templates.items():
            if code:
                parts.append(f"```{lang}\n{code}\n```")
    elif isinstance(templates, str) and templates:
        parts.append(f"```\n{templates}\n```")

    # Видео из блока (если есть)
    video = block.get("video") or {}
    urls = video.get("urls") or []
    if urls:
        title = video.get("title") or "Видео"
        url = urls[0].get("url") if isinstance(urls[0], dict) else urls[0]
        if url:
            parts.append(f"[{title}]({url})")

    # Метаданные
#    parts.append(
#       "\n---\n"
#        f"- **Step ID:** {step.get('id')}\n"
#        f"- **Тип шага:** {block_type}\n"
#        f"- **Позиция:** {step.get('position', step_index)}\n"
#        f"- **Lesson ID:** {lesson_id}\n"
#        f"- **Unit ID:** {unit_id}\n"
#        f"- **Section ID:** {section_id}"
#    )
    return "\n\n".join(parts).strip() + "\n"


# --------------------------------------------------------------------------- #
# Экспорт
# --------------------------------------------------------------------------- #

def _write_text(path: Path, content: str, overwrite: bool) -> bool:
    """Записать текстовый файл с учётом флага перезаписи."""
    if path.exists() and not overwrite:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _write_json(path: Path, data: Dict[str, Any], overwrite: bool) -> bool:
    """Записать JSON-файл (UTF-8, отступ 2) с учётом флага перезаписи."""
    return _write_text(path, json.dumps(data, ensure_ascii=False, indent=2), overwrite)


def export_course(
    course_id: int,
    export_dir: Path,
    client_id: str,
    client_secret: str,
    delay: float = 0.3,
    overwrite: bool = False,
    structure_only: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Экспортировать курс Stepik в локальную папку.

    Ошибка одного урока/шага не прерывает экспорт: она логируется и
    записывается в errors.log в папке курса.

    Args:
        course_id: ID курса.
        export_dir: Корневая папка экспорта.
        client_id: Stepik Client ID.
        client_secret: Stepik Client Secret.
        delay: Задержка между запросами, сек.
        overwrite: Перезаписывать существующие файлы.
        structure_only: Создать только структуру и JSON без Markdown.
        progress_cb: Колбэк прогресса (done_steps, total_steps, status_text).
        log_cb: Колбэк для строк логов.

    Returns:
        Статистика экспорта (path, modules, lessons, steps, errors) или {"error": ...}.
    """
    def emit(msg: str) -> None:
        logger.info(msg)
        if log_cb:
            log_cb(msg)

    token = get_access_token(client_id, client_secret)
    if not token:
        return {"error": "Не удалось получить access token Stepik"}

    course = get_course(course_id, token, delay=delay)
    if not course:
        return {"error": f"Не удалось получить курс {course_id}"}

    export_dir = Path(export_dir).expanduser()
    course_dir = export_dir / safe_filename(course.get("title") or f"course_{course_id}")
    course_dir.mkdir(parents=True, exist_ok=True)

    sections = get_sections(course, token, delay, client_id, client_secret)
    # Предварительный подсчёт шагов для прогресс-бара
    total_steps = 0
    tree: List[Dict[str, Any]] = []
    for section in sections:
        units = get_units(section, token, delay, client_id, client_secret)
        unit_entries = []
        for unit in units:
            lessons = get_lessons(unit, token, delay, client_id, client_secret)
            lesson = lessons[0] if lessons else {}
            steps = get_steps(lesson, token, delay, client_id, client_secret) if lesson else []
            total_steps += len(steps)
            unit_entries.append({"unit": unit, "lesson": lesson, "steps": steps})
        tree.append({"section": section, "units": unit_entries})

    # course.json
    course_json = {
        "id": course.get("id"),
        "title": course.get("title"),
        "description": course.get("description"),
        "url": f"https://stepik.org/course/{course.get('id')}/",
        "language": course.get("language"),
        "authors": course.get("authors", []),
        "create_date": course.get("create_date"),
        "update_date": course.get("update_date"),
        "module_ids": [s["section"].get("id") for s in tree],
    }
    _write_json(course_dir / "course.json", course_json, overwrite)

    errors: List[str] = []
    modules = lessons_count = steps_count = done = 0

    for si, entry in enumerate(tree, 1):
        section = entry["section"]
        sec_dir = course_dir / "modules" / safe_filename(section.get("title") or f"section_{section.get('id')}")
        sec_dir.mkdir(parents=True, exist_ok=True)
        modules += 1

        unit_ids: List[int] = []
        lesson_ids: List[int] = []
        lessons_dir = sec_dir / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)

        for ui, unit_entry in enumerate(entry["units"], 1):
            unit = unit_entry["unit"]
            lesson = unit_entry["lesson"]
            steps = unit_entry["steps"]
            unit_ids.append(unit.get("id"))
            if lesson.get("id"):
                lesson_ids.append(lesson.get("id"))

            lessons_count += 1

            try:
                # Заголовок урока для имени файла
                lesson_title = lesson.get("title") or f"unit_{unit.get('id')}"
                lesson_md_path = lessons_dir / f"{safe_filename(lesson_title)}.md"

                parts: List[str] = []
                try:
                    for sti, step in enumerate(steps, 1):
                        block = step.get("block")
                        if not isinstance(block, dict):
                            block = {}
                        # Экспортируем в Markdown только текстовые шаги (тип "text").
                        # Остальные типы (choice, matching, sorting, video и т.п.) пропускаются.
                        block_type = block.get("name") or step.get("step_type")
                        if block_type != "text":
                            done += 1
                            if progress_cb:
                                progress_cb(done, total_steps, f"Модуль {si}, урок {ui}, шаг {sti} (пропущен)")
                            continue
                        title = _extract_title(block.get("text") or "") or f"step_{step.get('id')}"
                        try:
                            if not structure_only:
                                md = step_to_markdown(
                                    step,
                                    unit_id=unit.get("id"),
                                    lesson_id=lesson.get("id"),
                                    section_id=section.get("id"),
                                    step_index=sti,
                                )
                                parts.append(md)
                            steps_count += 1
                        except Exception as exc:  # noqa: BLE001 - не прерываем экспорт
                            msg = f"Ошибка шага {step.get('id')} (урок {lesson.get('id')}): {exc}"
                            errors.append(msg)
                            emit(msg)
                        done += 1
                        if progress_cb:
                            progress_cb(done, total_steps, f"Модуль {si}, урок {ui}, шаг {sti}")
                except Exception as exc:  # noqa: BLE001 - ошибка урока не прерывает экспорт
                    msg = f"Ошибка урока {lesson.get('id')}: {exc}"
                    errors.append(msg)
                    emit(msg)

                if not structure_only and parts:
                    lesson_md = "\n---\n\n".join(parts) + "\n"
                    _write_text(lesson_md_path, lesson_md, overwrite)
            except Exception as exc:  # noqa: BLE001 - ошибка урока не прерывает экспорт
                msg = f"Ошибка урока {lesson.get('id')}: {exc}"
                errors.append(msg)
                emit(msg)

    if errors:
        log_path = course_dir / "errors.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            for err in errors:
                fh.write(err + "\n")

    return {
        "path": str(course_dir),
        "modules": modules,
        "lessons": lessons_count,
        "steps": steps_count,
        "errors": len(errors),
    }


# --------------------------------------------------------------------------- #
# Streamlit UI
# --------------------------------------------------------------------------- #

def main() -> None:
    """Точка входа Streamlit-приложения."""
    st.set_page_config(page_title="Stepik Course Exporter", layout="wide")
    st.title("📚 Экспорт курсов Stepik")

    env_id = os.environ.get("STEPIK_CLIENT_ID", "")
    env_secret = os.environ.get("STEPIK_CLIENT_SECRET", "")

    with st.sidebar:
        st.header("⚙️ Настройки")
        st.caption("Секреты читаются из .env/окружения. Поля ниже — резервный ввод.")
        client_id_input = st.text_input("CLIENT_ID", value="", type="default")
        client_secret_input = st.text_input("CLIENT_SECRET", value="", type="password")
        delay = st.number_input("Задержка между запросами, сек", min_value=0.0, max_value=10.0, value=0.3, step=0.1)
        overwrite = st.checkbox("Перезаписать существующие файлы", value=False)
        structure_only = st.checkbox("Создать только структуру без Markdown", value=False)
        check_btn = st.button("Проверить подключение к Stepik API")

        client_id = client_id_input.strip() or env_id
        client_secret = client_secret_input.strip() or env_secret

        if check_btn:
            if not client_id or not client_secret:
                st.warning("Укажите CLIENT_ID и CLIENT_SECRET (в полях или .env)")
            elif check_api_connection(client_id, client_secret):
                st.success("Подключение к Stepik API работает ✓")
            else:
                st.error("Не удалось подключиться к Stepik API")

    course_input = st.text_input("Ссылка на курс Stepik или ID", placeholder="https://stepik.org/course/12345/")
    export_dir = st.text_input("Папка сохранения", value="./exports")

    col1, col2 = st.columns(2)
    get_info_btn = col1.button("Получить информацию о курсе")
    export_btn = col2.button("Экспортировать курс", type="primary")

    if get_info_btn or export_btn:
        course_id = extract_course_id(course_input)
        if course_id is None:
            st.error("Не удалось извлечь ID курса из ссылки или ввода.")
            return
        if not client_id or not client_secret:
            st.error("CLIENT_ID и CLIENT_SECRET не заданы (в полях sidebar или .env).")
            return

    if get_info_btn:
        with st.spinner("Загрузка информации о курсе..."):
            token = get_access_token(client_id, client_secret)
            course = get_course(course_id, token, delay=delay) if token else None
        if not course:
            st.error("Не удалось получить информацию о курсе.")
            return
        st.session_state["course_id"] = course_id
        st.subheader(course.get("title") or "Без названия")
        if course.get("description"):
            st.markdown(course["description"])
        sections = get_sections(course, token, delay, client_id, client_secret)
        lessons_n = steps_n = 0
        for section in sections:
            for unit in get_units(section, token, delay, client_id, client_secret):
                for lesson in get_lessons(unit, token, delay, client_id, client_secret):
                    lessons_n += 1
                    steps_n += len(get_steps(lesson, token, delay, client_id, client_secret))
        c1, c2, c3 = st.columns(3)
        c1.metric("Разделов", len(sections))
        c2.metric("Уроков", lessons_n)
        c3.metric("Шагов", steps_n)

    if export_btn:
        progress = st.progress(0.0, text="Подготовка...")
        status = st.empty()
        log_box = st.empty()
        logs: List[str] = []

        def on_progress(done: int, total: int, text: str) -> None:
            progress.progress(min(done / total, 1.0) if total else 0.0, text=text)

        def on_log(msg: str) -> None:
            logs.append(msg)
            log_box.text("\n".join(logs[-8:]))

        with st.spinner("Экспорт курса..."):
            result = export_course(
                course_id,
                Path(export_dir),
                client_id,
                client_secret,
                delay=delay,
                overwrite=overwrite,
                structure_only=structure_only,
                progress_cb=on_progress,
                log_cb=on_log,
            )

        if "error" in result:
            status.error(result["error"])
        else:
            progress.progress(1.0, text="Готово")
            status.success("Экспорт завершён успешно ✓")
            st.write(f"**Путь:** `{result['path']}`")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Модулей", result["modules"])
            m2.metric("Уроков", result["lessons"])
            m3.metric("Шагов", result["steps"])
            m4.metric("Ошибок", result["errors"])
            if result["errors"]:
                st.warning("Часть шагов не удалось экспортировать. Подробности — в errors.log.")


def run() -> None:
    """Точка входа консольной команды stepik-exporter (запуск через Streamlit)."""
    import sys
    from pathlib import Path
    from streamlit.web import cli as stcli

    app_path = Path(__file__).resolve()
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.headless=true",
        "--server.port=8501",
    ]
    sys.exit(stcli.main())

if __name__ == "__main__":
    main()

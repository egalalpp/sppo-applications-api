# Один образ для api, worker, init и flower — отличаются только командой запуска.
# На бесплатных хостингах без отдельных воркеров (Render free) весь стек работает в одном
# контейнере: RUN_MODE=all, см. entrypoint.sh и docs/DEPLOY.md.
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Tesseract по умолчанию запускает несколько потоков OpenMP; на контейнерах с долей
    # процессора это только замедляет распознавание.
    OMP_THREAD_LIMIT=1

WORKDIR /srv

# Tesseract OCR с русским (rus) и английским (eng) языковыми пакетами.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --list-langs

COPY requirements.txt .
RUN pip install -r requirements.txt

# Не запускаем приложение от root.
RUN useradd --create-home --uid 10001 app
COPY --chmod=755 entrypoint.sh ./entrypoint.sh
# Если файл побывал в Windows и получил CRLF, shebang перестаёт работать — убираем \r.
RUN sed -i 's/\r$//' ./entrypoint.sh
COPY app ./app
USER app

EXPOSE 8000
# Режим выбирается переменной RUN_MODE: api (по умолчанию) | worker | all
CMD ["./entrypoint.sh"]


# Образ для тестов: docker build --target test -t sppo-api-test ./api && docker run --rm sppo-api-test
FROM runtime AS test
USER root
# DejaVu — шрифт с кириллицей: тесты рисуют картинки с текстом и прогоняют их через Tesseract.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

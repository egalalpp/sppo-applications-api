# Развёртывание альфа-версии в облаке (бесплатные тарифы)

Схема из задания: **MongoDB Atlas (M0)** — база, **Cloudflare R2** — сканы, **Render** — бэкенд, **Brevo** — письма,
**GitHub Pages** — фронтенд. Весь бэкенд (API, воркер Celery, Tesseract) работает в **одном контейнере** Render.

```
Браузер (GitHub Pages) ──HTTPS──▶ Render: sppo-api  ──▶ MongoDB Atlas (M0)
                                   │  API + Celery + Tesseract ──▶ Cloudflare R2 (сканы)
                                   │                          ──▶ Brevo API (письма, HTTPS)
                                   └──▶ Render Key Value (Redis: очередь и кэш)
```

> **Только тестовые данные.** Atlas, R2, Render и Brevo расположены за рубежом, а персональные поля в базе пока
> не шифруются. Для реальных данных нужен сервер в РФ и доработки из раздела «Безопасность» в README.

## Что проверено, а что нет

Локально проверено: контейнер в режиме `RUN_MODE=all` (то, что запускает Render) проходит весь сквозной сценарий
`scripts/smoke_test.py`, включая распознавание, письма и права; потребление памяти — около 165 МБ в простое
и около 200 МБ после обработки нескольких заявок. Сами облачные сервисы из этой среды развернуть было нельзя
(нужны ваши аккаунты), поэтому инструкция ниже собрана по документации сервисов и **не прогонялась вживую**.
Места, где стоит свериться с актуальным интерфейсом, помечены ⚠.

## Ограничения бесплатного тарифа Render (из документации Render)

| Ограничение | Что это значит для проекта |
|---|---|
| **Исходящий SMTP закрыт** (порты 25, 465, 587) | Письма — только по HTTPS API: `EMAIL_BACKEND=brevo` |
| **Фоновых воркеров на бесплатном тарифе нет** (только веб-сервисы, Postgres, Key Value) | Celery работает внутри веб-сервиса: `RUN_MODE=all` |
| Сервис «засыпает» после 15 минут без запросов, пробуждение около минуты | Первый запрос после простоя долгий; задачи в очереди ждут пробуждения. Фронтенд должен это пережить (запасные данные из `data.js`) |
| 750 бесплатных часов в месяц на аккаунт | Один постоянно работающий сервис укладывается; несколько — нет |
| Нет постоянного диска, файловая система сбрасывается | Всё состояние вне контейнера: Atlas, R2, Key Value |
| Бесплатный Key Value хранит данные **только в памяти** (25 МБ) | При его перезапуске очередь теряется. Заявки, «зависшие» в `queued`/`running`, **подметальщик сам** ставит в очередь заново через `OCR_STALE_MINUTES` (15 минут); оператор тоже может нажать «Перезапустить OCR» |
| Один экземпляр, без масштабирования | `API_REPLICAS` и nginx здесь не нужны |
| ⚠ По обзорам сторонних сайтов — 512 МБ памяти и 0,1 CPU | Распознавание одной заявки может занимать минуты. Если памяти не хватает, уменьшите `OCR_MAX_SIDE_PX` (1600), `OCR_MAX_PDF_PAGES` (2) |

## Порядок действий

### 1. MongoDB Atlas

1. https://www.mongodb.com/atlas → создать проект и кластер **M0 (Free)**. Регион — ближайший к Render (Франкфурт).
2. **Database Access** → Add New Database User: имя `sppo_app`, пароль из букв и цифр (так не придётся экранировать
   его в строке подключения). Права: `readWrite` на базу `sppo` и, если интерфейс позволяет, `dbAdmin` (нужен для обновления
   валидаторов схемы). С одним `readWrite` приложение тоже работает: при старте оно пишет предупреждение
   «Cannot update validator» и продолжает с существующим валидатором. Atlas M0 — это replica set, поэтому смена статуса и запись
   в журнал аудита идут одной транзакцией без дополнительной настройки.
3. **Network Access** → Add IP Address → **Allow Access from Anywhere** (`0.0.0.0/0`): у бесплатных сервисов Render
   нет фиксированных исходящих адресов.
4. **Connect → Drivers** → скопировать строку вида
   `mongodb+srv://sppo_app:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`
   и подставить пароль. Это значение `MONGO_URI`. Название базы — переменная `MONGO_DB` (по умолчанию `sppo`),
   создавать её вручную не нужно: коллекции, индексы и справочник создаёт приложение при старте.

### 2. Cloudflare R2

1. https://dash.cloudflare.com → **R2 Object Storage** → Create bucket с именем `sppo-documents`.
   ⚠ Для включения R2 может понадобиться привязать платёжную карту (бесплатные лимиты при этом остаются):
   проверьте условия в панели.
2. **R2 → Manage R2 API Tokens** → Create API token → права **Object Read & Write**, лучше ограничить одним бакетом.
   Сохраните **Access Key ID** и **Secret Access Key** (показываются один раз) и адрес **S3 API endpoint**
   `https://<account_id>.r2.cloudflarestorage.com` (без имени бакета).
3. Переменные: `S3_ENDPOINT_URL` = этот адрес, `S3_PUBLIC_ENDPOINT_URL` = он же, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
   `S3_BUCKET=sppo-documents`, **`S3_REGION=auto`**.
4. Скачивание сканов идёт по presigned-ссылкам, которые открываются прямо в браузере (переходом по ссылке). Если
   фронтенд будет подгружать файл через `fetch()` из JavaScript, в настройках бакета нужна CORS-политика, например:
   `[{"AllowedOrigins":["https://username.github.io"],"AllowedMethods":["GET"],"AllowedHeaders":["*"]}]`.

Приложение не создаёт бакет, если у токена нет на это прав, — только предупреждает в логе. Проверка готовности `/ready`
использует листинг объектов, поэтому работает и с таким токеном.

*Если нужен российский хостинг:* код работает с любым S3-совместимым хранилищем — достаточно сменить
`S3_ENDPOINT_URL`, ключи и регион (например, Yandex Object Storage: `https://storage.yandexcloud.net`, регион
`ru-central1`).

### 3. Почта <a id="почта"></a>

Письма уходят студенту при статусах «принята», «одобрена», «отклонена». Что выбрать для альфа-версии:

| Сервис | Бесплатно | Для чего | Как подключить |
|---|---|---|---|
| **Mailpit** (в docker-compose) | без ограничений, без регистрации | Разработка, **скриншоты для презентации**: письма видны на http://localhost:8025 | Работает по умолчанию |
| **Mailtrap Email Sandbox** | да (лимит писем в месяц небольшой: по разным источникам 50–500 — смотрите тарифы) | Красивый «виртуальный ящик» для демонстрации, письма настоящим адресатам не уходят | SMTP: `SMTP_HOST=sandbox.smtp.mailtrap.io`, `SMTP_PORT=2525`, `SMTP_USER`/`SMTP_PASS` из кабинета |
| **Brevo** (бывший Sendinblue) | 300 писем в день, настоящая доставка | Альфа-версия на Render | HTTPS API (см. ниже) |

Другие сервисы (Resend, Mailjet и т. п.) не проверялись; у SendGrid, по сообщениям, бесплатный тариф закрыт. Если решите брать другой,
для Render нужен вариант с HTTPS API (его придётся добавить в `app/services/mailer.py`) или SMTP на порту, не
входящем в список закрытых (⚠ не проверено, что такой порт у выбранного сервиса работает с Render).

**Brevo для Render (HTTPS API):**

1. Зарегистрируйтесь на https://www.brevo.com.
2. **Senders, Domains & Dedicated IPs → Senders** → добавьте адрес отправителя и подтвердите его по ссылке из письма.
   Brevo отправляет только от подтверждённых адресов; для письма от другого адреса сервис вернёт ошибку 400
   (она видна в логах воркера как «Brevo ответил 400»).
3. **SMTP & API → API Keys** → создать ключ. Переменные: `EMAIL_BACKEND=brevo`, `BREVO_API_KEY=<ключ>`,
   `MAIL_FROM="СППО РУТ (МИИТ) <подтверждённый@адрес>"`.
4. ⚠ Убедитесь, что транзакционные письма через API доступны на вашем тарифе (на момент написания Brevo заявляет
   300 писем в день на бесплатном плане, включая SMTP и API).

**Brevo по SMTP** (для своего сервера или платного Render, где SMTP открыт): `SMTP_HOST=smtp-relay.brevo.com`,
`SMTP_PORT=587` (STARTTLS) или `465` (SSL), `SMTP_USER` — логин в Brevo, `SMTP_PASS` — **SMTP-ключ** (не пароль от аккаунта),
`MAIL_FROM` — подтверждённый адрес.

### 4. Render

Используется **Docker** (а не нативное окружение Python): в образ нужно поставить Tesseract (`apt-get`), а нативное окружение
этого не позволяет. Поэтому вместо команды `uvicorn main:app --host 0.0.0.0 --port $PORT` контейнер запускает
`api/entrypoint.sh`, который делает то же самое (порт берётся из `$PORT`) и заодно поднимает воркер.

**Вариант А: Blueprint (рекомендуется).** В репозитории есть [`render.yaml`](../../render.yaml).

1. https://render.com → войти через GitHub.
2. **New → Blueprint** → выбрать репозиторий `egalalpp/sppo-applications-api`. Render прочитает `render.yaml`, создаст
   веб-сервис `sppo-api` и бесплатный Key Value `sppo-redis` и спросит значения секретных переменных (таблица ниже).
   ⚠ Если при создании Render ругнётся на синтаксис `render.yaml` (например, на `fromService`/`keyvalue`), используйте вариант Б:
   поля описаны по документации Blueprint, но вживую не проверялись.

**Вариант Б: вручную.**

1. **New → Key Value** → имя `sppo-redis`, тариф Free, Maxmemory policy `noeviction` → скопируйте **Internal Connection String**.
2. **New → Web Service** → репозиторий `egalalpp/sppo-applications-api` → Language **Docker**, Instance Type **Free**,
   Dockerfile Path `./backend/api/Dockerfile`, Docker Build Context Directory `./backend/api`, Health Check Path `/health`.
3. Добавьте переменные из таблицы.

**Переменные окружения веб-сервиса:**

| Переменная | Значение |
|---|---|
| `RUN_MODE` | `all` |
| `CELERY_POOL` / `CELERY_CONCURRENCY` | `threads` / `2` |
| `ENVIRONMENT` | `prod` |
| `JWT_SECRET` | случайная строка от 32 символов (Blueprint сгенерирует сам; вручную: `python -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `HASH_PEPPER` | секрет HMAC-хэшей паспорта и счёта, от 16 символов (Blueprint сгенерирует сам). **После запуска не меняйте**: прежние хэши перестанут находить дубли, пока не выполнен `python -m app.bootstrap --rehash` |
| `MONGO_URI` | строка из шага 1 |
| `MONGO_DB` | `sppo` |
| `REDIS_URL` | Internal Connection String от Key Value |
| `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL` | адрес R2 из шага 2 |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | ключи R2 |
| `S3_BUCKET` / `S3_REGION` | `sppo-documents` / `auto` |
| `EMAIL_BACKEND` / `BREVO_API_KEY` / `MAIL_FROM` | `brevo` / ключ / подтверждённый отправитель |
| `CORS_ORIGINS` | `["https://username.github.io"]` — адрес сайта фронтенда **без пути и без слеша на конце**, JSON-список |
| `FRONTEND_URL` | `https://username.github.io/sppo` — адрес для ссылки на кабинет в письмах (необязательно) |
| `SEED_DEMO_USERS` | `true` — создаст `student@rut.ru` / `operator@rut.ru` / `admin@rut.ru` с паролями из ТЗ; **на сервере с реальными данными — `false`** |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | необязательно: «боевой» администратор со своим паролем |

Опционально для слабой машины: `OCR_MAX_SIDE_PX=1600`, `OCR_MAX_PDF_PAGES=2`, `CELERY_CONCURRENCY=1`.

После деплоя Render выдаст адрес вида `https://sppo-api.onrender.com`. Проверка:

```bash
curl https://sppo-api.onrender.com/ready          # {"status":"ok", ... "mongo":"ok","redis":"ok","s3":"ok"}
curl -s -X POST https://sppo-api.onrender.com/api/v1/auth/login -d "username=operator@rut.ru&password=operator123"
```

Документация API — `https://sppo-api.onrender.com/redoc`. При каждом старте контейнер идемпотентно создаёт коллекции,
индексы, справочник и демо-учётки (`python -m app.bootstrap`), потом запускает воркер Celery в фоне и веб-сервер.

### 5. Фронтенд на GitHub Pages

В `ui.js` / `api.js`:

```js
const API_BASE = window.location.hostname === 'localhost'
  ? 'http://localhost:8000/api/v1'
  : 'https://sppo-api.onrender.com/api/v1';
```

Порт `8000` — адрес локального бэкенда из `docker compose` (`HTTP_PORT` в `.env`). Адрес сайта на Pages должен быть в
`CORS_ORIGINS` на Render, иначе браузер заблокирует запросы. Для локальной разработки фронтенда добавьте в `.env` локального стека
свой адрес, например `CORS_ORIGINS=["http://localhost:5500","http://127.0.0.1:5500"]`.

Что должен учесть фронтенд (контракт API):

- вход: `POST /auth/login` (форма `username` = email, `password`) → `access_token`; дальше `Authorization: Bearer ...`;
- в форме заявки **email обязателен** (на него приходят уведомления), поле `full_name_as_written` больше не нужно;
- кнопка «Отправить» разблокируется, когда заполнена форма, есть согласие на обработку ПДн и приложены все документы из
  `GET /benefit-types` для выбранного основания; иначе `POST .../submit` вернёт `422` со списком `missing_slots`;
- студент не получает ничего про OCR — ни полей, ни статусов; оператор видит `ocr.state` (`ready_for_review`, `ocr_failed`),
  `ocr.accuracy`, результаты по вложениям и кнопку «Перезапустить OCR» (`POST /applications/{id}/ocr/restart`);
- отказ — только с причиной (`rejection_reason`), студент видит её в `status` и в письме;
- если сервер «спит», первый запрос может идти около минуты: показывайте плашку «сервер недоступен» и запасные данные.

## Что делать, если...

| Симптом | Причина и решение |
|---|---|
| Деплой упал на сборке образа | Смотрите лог сборки: в нём `apt-get install tesseract-ocr`, `pip install`. Сборка Docker на бесплатном тарифе долгая |
| В логе `Cannot update validator ... continuing` | У пользователя Atlas нет права `collMod`. Не ошибка; чтобы убрать — выдайте `dbAdmin` на базу `sppo` |
| `/ready` → `mongo: error` | `MONGO_URI` не тот или в Network Access нет `0.0.0.0/0`; пароль с символами `@ : /` нужно url-кодировать |
| `/ready` → `s3: error` | Неверные ключи/endpoint или токен без доступа к бакету; для R2 проверьте `S3_REGION=auto` |
| Письма не приходят, в логе «Brevo ответил 400» | Адрес `MAIL_FROM` не подтверждён в Brevo |
| В логе `SMTP недоступен` на Render | Используется SMTP на закрытом порту: переключитесь на `EMAIL_BACKEND=brevo` |
| Запросы из браузера блокируются (CORS) | Адрес сайта не в `CORS_ORIGINS` или указан с путём/слешем: нужно `https://username.github.io` |
| Заявка долго «queued» | Сервис спал или Key Value перезапускался и очередь потерялась. Подметальщик подхватит заявку сам через 15 минут (`OCR_STALE_MINUTES`); оператор может нажать «Перезапустить OCR» раньше |
| Контейнер перезапускается, в логах `Killed` | Не хватило памяти на распознавание: `OCR_MAX_SIDE_PX=1600`, `CELERY_CONCURRENCY=1` |

## Тот же стек на своём VPS

Всё, что выше, нужно только из-за ограничений бесплатных тарифов. На обычном VPS (от 2 ГБ памяти) достаточно
`docker compose up -d --build`: отдельные воркеры, nginx с двумя репликами API, Mailpit или настоящий SMTP.
Обязательно смените пароли в `.env`, `SEED_DEMO_USERS=false`, поставьте HTTPS и откройте порт 8333 хранилища, если
сканы остаются в локальном SeaweedFS (`S3_PUBLIC_ENDPOINT_URL=http://<ip>:8333`).

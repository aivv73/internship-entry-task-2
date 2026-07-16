# Вступительное задание: «Платёж прошёл? Докажи»

## Руководство проверяющего

Требования: Docker с Compose, `curl`; для запуска тестов также нужны Python 3.14 и
[uv](https://docs.astral.sh/uv/). В Compose запускаются `candidate-service`, PostgreSQL и неизменённый
`ghcr.io/fintech-dev-lab/internship-provider-simulator:v0.2.0`. Миграции Alembic применяются до
запуска HTTP-сервера кандидата.

### Чистый запуск

```bash
git clone https://github.com/aivv73/internship-entry-task-2.git
cd internship-entry-task-2
SIMULATOR_MODE=success docker compose up --build --detach --wait
docker compose ps
curl -i http://localhost:8080/health
curl -sS http://localhost:8080/metrics
```

Кандидат доступен на `localhost:8080`, реальный симулятор — на `localhost:8081`. Симулятор вызывает
`http://candidate-service:8080/receipts`, а кандидат отправляет платежи на
`http://provider-simulator:8081` внутри общей Compose-сети.

### Сквозной результат `COMPLETED`

```bash
OP="review-completed-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"1000.00\",\"currency\":\"RUB\",\"description\":\"Reviewer success\"}"
curl -i -X POST "http://localhost:8080/operations/$OP/submit"
for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP")
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' && break
  sleep 1
done
printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' &&
  curl -sS "http://localhost:8080/operations/$OP/events" &&
  docker compose logs provider-simulator | grep "\"operationId\":\"$OP\""
```

Последний ответ операции содержит `"status":"COMPLETED"`. В аудите симулятора ровно одна запись
нового платежа (`replay:false`):

```bash
docker compose logs provider-simulator \
  | grep '"msg":"payment accepted"' \
  | grep "\"operationId\":\"$OP\"" \
  | grep -c '"replay":false'
```

Команда печатает `1`.

### Сквозной результат `REJECTED`

Режим симулятора выбирается только при старте контейнера, поэтому его нужно пересоздать:

```bash
SIMULATOR_MODE=reject docker compose up --detach --force-recreate provider-simulator
OP="review-rejected-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"250.00\",\"currency\":\"RUB\",\"description\":\"Reviewer rejection\"}"
curl -i -X POST "http://localhost:8080/operations/$OP/submit"
for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP")
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"REJECTED"' && break
  sleep 1
done
printf '%s' "$RESPONSE" | grep -q '"status":"REJECTED"' &&
  curl -sS "http://localhost:8080/operations/$OP/events" &&
  docker compose logs provider-simulator | grep "\"operationId\":\"$OP\""
```

Операция получает `"status":"REJECTED"`; результат установлен callback-квитанцией, а не HTTP-ответом
на создание платежа.

### Конкурентная отправка и аудит одного платежа

```bash
SIMULATOR_MODE=success docker compose up --detach --force-recreate provider-simulator
OP="review-concurrent-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"500.00\",\"currency\":\"RUB\"}"
seq 1 16 | xargs -P16 -I{} curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST "http://localhost:8080/operations/$OP/submit"
for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP")
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' && break
  sleep 1
done
printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' &&
  curl -sS "http://localhost:8080/operations/$OP/events" &&
  docker compose logs provider-simulator \
    | grep '"msg":"payment accepted"' \
    | grep "\"operationId\":\"$OP\"" \
    | grep -c '"replay":false'
```

Коды содержат один `202` и пятнадцать `200`; аудит снова печатает `1`.

### Перезапуск, восстановление и сохранность данных

Сначала остановим провайдера, чтобы намерение гарантированно осталось незавершённым, затем заменим
процесс кандидата и вернём провайдера:

```bash
docker compose stop provider-simulator
OP="review-recovery-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"750.00\",\"currency\":\"RUB\"}"
curl -i -X POST "http://localhost:8080/operations/$OP/submit"
docker compose restart candidate-service
docker compose start provider-simulator
for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP" 2>/dev/null || true)
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' && break
  sleep 1
done
printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' &&
  docker compose logs provider-simulator | grep "\"operationId\":\"$OP\""
```

Кандидат возобновляет сохранённое намерение с тем же ключом, операция завершается, а аудит содержит
один новый платёж. Обычная остановка Compose не удаляет именованный том PostgreSQL:

```bash
docker compose down
SIMULATOR_MODE=success docker compose up --build --detach --wait
curl -sS "http://localhost:8080/operations/$OP"
```

Операция и её история остаются доступны после пересоздания контейнеров.

### Тесты

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .

TEST_DATABASE_URL=postgresql+asyncpg://payment:payment@localhost:5432/payments \
  uv run pytest tests/integration
```

Автоматический smoke-тест сам создаёт изолированный Compose-проект, вызывает реальный симулятор,
проверяет конкурентные `submit`, callback, аудит единственного платежа и перезапуск кандидата. Порты
должны быть свободны, поэтому сначала остановите основной стек:

```bash
docker compose down
RUN_COMPOSE_SMOKE=1 uv run pytest tests/compose/test_real_provider.py -q
```

### Остановка и очистка

```bash
# Остановить контейнеры, сохранив PostgreSQL-том:
docker compose down

# Удалить контейнеры и все данные задания без возможности восстановления:
docker compose down --volumes --remove-orphans
```

### Локальный запуск без Compose

```bash
export DATABASE_URL=postgresql+asyncpg://payment:payment@localhost:5432/payments
export PROVIDER_URL=http://localhost:8081
uv run alembic upgrade head
uv run uvicorn payment_service.main:app --host 0.0.0.0 --port 8080
```

Нужно собрать сервис, который проводит платёжную операцию через внешнего провайдера и сохраняет корректное состояние при повторах, конкурентных запросах, потерянных HTTP-ответах и перезапусках.

Внешнюю систему изображает готовый `provider-simulator`. Сервис должен действительно вызывать его по HTTP. Успешный транспортный ответ не доказывает завершение платежа, а отсутствие ответа не доказывает, что платёж не был создан. Финальный результат определяется только callback-квитанцией.

Нам не нужна большая архитектура. Важно получить небольшой, воспроизводимый и устойчивый сквозной сценарий.

## На чём делать

| Вариант | Версия |
|---|---|
| C# | .NET 10 |
| Python | Python 3.14 |

Можно использовать любой удобный фреймворк, постоянное хранилище и AI-инструменты.

## Состояния операции

| Состояние | Значение |
|---|---|
| `CREATED` | Операция создана, отправка ещё не запрошена |
| `PROCESSING` | Намерение отправки надёжно сохранено, ожидается результат провайдера |
| `COMPLETED` | Провайдер подтвердил успех callback-квитанцией |
| `REJECTED` | Провайдер подтвердил отказ callback-квитанцией |

Главный инвариант:

> При любых повторах, конкурентных запросах, потерянных ответах и перезапусках одной операции соответствует не более одного платежа провайдера, а финальный статус определяется только callback-квитанцией.

## Обязательный API сервиса

Методы и маршруты ниже являются обязательной частью контракта и изменяться не должны.

| Метод | Маршрут | Успешный статус | Назначение |
|---|---|---|---|
| `GET` | `/health` | `200` | Проверка готовности |
| `POST` | `/operations` | `201` | Создание операции |
| `POST` | `/operations/{id}/submit` | `202` или `200` | Надёжно запланировать отправку |
| `POST` | `/receipts` | `204` | Принять callback-квитанцию |
| `GET` | `/operations/{id}` | `200` | Получить текущее состояние |
| `GET` | `/operations/{id}/events` | `200` | Получить историю переходов |

### Создание операции

```http
POST /operations
Content-Type: application/json
```

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа"
}
```

`operationId` обязателен. `amount` — положительная десятичная строка с не более чем двумя знаками после точки. Поддерживается валюта `RUB`.

Ответ операции содержит как минимум:

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа",
  "status": "CREATED",
  "providerPaymentId": null
}
```

Дополнительные поля допустимы. Повторное создание того же `operationId` возвращает `409 Conflict`.

### Отправка

Первый `POST /operations/{id}/submit` должен атомарно:

1. проверить состояние `CREATED`;
2. сохранить намерение отправки в постоянном хранилище;
3. перевести операцию в `PROCESSING`;
4. гарантировать, что после перезапуска отправка будет продолжена;
5. вернуть `202 Accepted`.

Сетевой вызов провайдера можно выполнить после фиксации намерения в том же процессе или фоновым обработчиком. Нельзя удерживать блокировку операции на всё время внешнего HTTP-вызова: callback может прийти раньше ответа провайдера.

Повторный `submit` для `PROCESSING`, `COMPLETED` или `REJECTED` не создаёт новое намерение и возвращает текущее состояние с `200 OK`. При конкурентных запросах ровно один запрос создаёт намерение, остальные получают уже сохранённое состояние.

### История

`GET /operations/{id}/events` возвращает массив событий в порядке их фиксации. Каждое событие содержит как минимум:

```json
{
  "eventId": 1,
  "type": "CREATED",
  "fromStatus": null,
  "toStatus": "CREATED",
  "message": "Operation created",
  "occurredAt": "2026-07-15T12:00:00Z"
}
```

`eventId` монотонно возрастает в пределах операции. Повторная квитанция не создаёт второй переход в то же состояние.

## Контракт внешнего провайдера

Адрес передаётся через `PROVIDER_URL`. В Docker Compose это `http://provider-simulator:8081`.

```http
POST {PROVIDER_URL}/payments
Content-Type: application/json
Idempotency-Key: operation-123
X-Correlation-ID: operation-123
```

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB"
}
```

Требования:

- `Idempotency-Key` обязателен и равен `operationId`;
- все повторы одной операции используют тот же ключ и неизменное тело;
- `X-Correlation-ID` равен `operationId` и используется в журналах;
- повтор с тем же ключом возвращает тот же `providerPaymentId` и не создаёт новый платёж;
- `503 Service Unavailable` и сетевые ошибки требуют ограниченного повтора с тем же ключом;
- после сетевой ошибки операция остаётся `PROCESSING`: провайдер мог уже принять платёж.

Обычный успешный ответ — `202 Accepted`:

```json
{
  "providerPaymentId": "aa5b7856-e9f2-4fd5-955b-38b1f28d9c57",
  "status": "ACCEPTED"
}
```

Ответ сохраняет `providerPaymentId`, но не переводит операцию в финальный статус. Если квитанция уже обработана, поздний `202` не должен вернуть операцию из финального состояния в `PROCESSING`.

## Callback-квитанция

Симулятор выполняет `POST` на `http://candidate-service:8080/receipts`:

```json
{
  "providerPaymentId": "aa5b7856-e9f2-4fd5-955b-38b1f28d9c57",
  "operationId": "operation-123",
  "result": "COMPLETED",
  "message": "Payment completed",
  "occurredAt": "2026-07-15T12:00:00Z"
}
```

Правила обработки:

- `result` принимает значение `COMPLETED` или `REJECTED`;
- квитанция может прийти до HTTP-ответа на запрос провайдера;
- если `providerPaymentId` ещё не сохранён, он устанавливается из первой валидной квитанции;
- повтор той же квитанции отвечает `204` и не создаёт новый переход;
- поздняя квитанция с противоположным результатом отвечает `204`, фиксируется как проигнорированная и не меняет финальный статус;
- несовпадающий `providerPaymentId` после установления связи возвращает `409`;
- обработка квитанции и изменение состояния выполняются одной транзакцией.

## Постоянное хранение и восстановление

Операции, намерения отправки, `providerPaymentId` и история событий должны переживать удаление и повторное создание контейнера без удаления volume.

После запуска сервис автоматически находит незавершённые операции `PROCESSING` и продолжает отправку с прежним `Idempotency-Key`. Остановка между внешним принятием платежа и локальным сохранением ответа не должна создавать второй платёж.

## Docker Compose

В корне решения нужен `compose.yaml` или `docker-compose.yml` с сервисами `candidate-service` и `provider-simulator` в общей сети.

```yaml
services:
  candidate-service:
    build: .
    environment:
      PROVIDER_URL: http://provider-simulator:8081
    ports:
      - "8080:8080"
    volumes:
      - candidate-data:/data

  provider-simulator:
    image: ghcr.io/fintech-dev-lab/internship-provider-simulator:v0.2.0
    environment:
      CALLBACK_URL: http://candidate-service:8080/receipts
    ports:
      - "8081:8081"
    depends_on:
      - candidate-service

volumes:
  candidate-data:
```

Образ публичный, `docker login` не нужен. Всё решение запускается командой:

```bash
docker compose up --build
```

## Что обязательно

- проект собирается и запускается через Docker Compose;
- сервис кандидата слушает порт `8080`;
- реализованы все обязательные маршруты;
- используется постоянное хранилище;
- намерение отправки сохраняется до внешнего вызова;
- провайдер вызывается с `Idempotency-Key` и `X-Correlation-ID`;
- конкурентные и повторные `submit` не создают второй платёж;
- сетевой сбой не приводит к ложному отказу или повторному платежу;
- ранняя, повторная и конфликтующая квитанции обрабатываются по указанным правилам;
- незавершённая обработка продолжается после перезапуска;
- README содержит команды запуска и полного сквозного сценария.

## Как проверяем

Оценки по баллам нет. Результат — `зачёт` или `незачёт`.

Автопроверка выполняет в том числе:

1. базовый путь до `COMPLETED` и `REJECTED`;
2. серию одновременных `submit` одной операции;
3. временный отказ и потерю ответа после фактического принятия платежа;
4. callback до ответа провайдера;
5. остановку и повторный запуск `candidate-service` во время обработки;
6. повторную и запоздалую конфликтующую квитанции;
7. проверку истории и сохранности данных;
8. сверку по внутреннему аудиту провайдера, что создан ровно один платёж.

Заглушка, которая выставляет результат без фактического вызова провайдера, не проходит. Решение, создающее несколько платежей одной операции, также получает `незачёт`.

## Как сдать

1. Создайте отдельный публичный репозиторий GitHub.
2. Разместите в нём решение, Dockerfile, Docker Compose и README с командами запуска и проверки.
3. Убедитесь, что репозиторий можно клонировать и запустить на чистой машине по README.
4. В ответ на сообщение с тестовым заданием на hh.ru пришлите ссылку на репозиторий.

Делать fork репозитория задания и открывать pull request не нужно. Репозиторий с решением должен оставаться публичным на время проверки.

## Что даст преимущество

- ограниченный backoff с jitter;
- структурированные журналы с `operationId`, `providerPaymentId` и попыткой;
- метрики незавершённых операций и повторов;
- тесты конкурентности и восстановления;
- корректное завершение фоновой обработки при остановке процесса.

# Resilient Payment Service

Небольшой платёжный сервис, устойчивый к повторным и конкурентным запросам, временной
недоступности провайдера, потерянным HTTP-ответам, ранним callback-квитанциям и перезапускам.

Сервис действительно вызывает опубликованный симулятор платёжного провайдера. PostgreSQL хранит
операции, историю переходов, намерения отправки и состояние повторов. Успешный HTTP-ответ провайдера
сохраняет связь с платежом, но только callback-квитанция переводит операцию в финальное состояние.

## Основные гарантии

- одной операции соответствует не более одного платежа провайдера;
- `operationId` используется как стабильный `Idempotency-Key` и `X-Correlation-ID`;
- намерение отправки фиксируется в PostgreSQL до сетевого вызова;
- внешний HTTP-вызов не удерживает блокировку операции или открытую транзакцию;
- повторные и конкурентные `submit` создают одно намерение и один переход;
- неоднозначные сетевые ошибки повторяются с тем же ключом и неизменным телом;
- незавершённая отправка продолжается после перезапуска;
- только callback устанавливает `COMPLETED` или `REJECTED`;
- первая валидная финальная квитанция побеждает, дубликаты идемпотентны;
- операции, события, связь с провайдером и retry-метаданные переживают замену контейнеров.

## Технологии

- Python 3.14, FastAPI и Pydantic;
- async SQLAlchemy и asyncpg;
- PostgreSQL 17;
- Alembic;
- httpx;
- Prometheus client;
- Docker Compose;
- pytest и Ruff.

## Как это работает

```text
POST /operations/{id}/submit
          │
          ▼
 PostgreSQL transaction
 operation → PROCESSING
 durable dispatch intent
 transition event
          │ commit
          ▼
 background dispatcher ──────► provider-simulator
          │                    Idempotency-Key: operationId
          │                    X-Correlation-ID: operationId
          │                              │
          │                              ▼
          └──────────────────── POST /receipts
                                         │
                                         ▼
                              COMPLETED or REJECTED
```

Рабочие процессы координируются блокировками PostgreSQL и `FOR UPDATE SKIP LOCKED`. Попытки,
расписание повторов и lease хранятся в базе; время PostgreSQL является источником времени для claim
и retry. Доставка HTTP имеет семантику at-least-once, а уникальный внешний эффект обеспечивает
идемпотентность провайдера.

## API

| Метод | Маршрут | Успех | Назначение |
|---|---|---|---|
| `GET` | `/health` | `200` | Готовность сервиса и PostgreSQL |
| `POST` | `/operations` | `201` | Создать операцию в `CREATED` |
| `POST` | `/operations/{id}/submit` | `202` / `200` | Надёжно запланировать отправку |
| `GET` | `/operations/{id}` | `200` | Получить текущее состояние |
| `GET` | `/operations/{id}/events` | `200` | Получить упорядоченную историю |
| `POST` | `/receipts` | `204` | Принять callback провайдера |
| `GET` | `/metrics` | `200` | Получить Prometheus-метрики |

### Состояния операции

| Состояние | Значение |
|---|---|
| `CREATED` | Операция создана, отправка ещё не запрошена |
| `PROCESSING` | Намерение сохранено, ожидается результат провайдера |
| `COMPLETED` | Callback подтвердил успешный платёж |
| `REJECTED` | Callback подтвердил отказ |

## Быстрый запуск

Нужны Docker с Compose и `curl`.

```bash
git clone https://github.com/aivv73/internship-entry-task-2.git
cd internship-entry-task-2
SIMULATOR_MODE=success docker compose up --build --detach --wait
```

Compose запускает:

- `candidate-service` на <http://localhost:8080>;
- `provider-simulator:v0.2.0` на <http://localhost:8081>;
- PostgreSQL с именованным томом `candidate-data`.

Миграции применяются до запуска HTTP-сервера. Проверка готовности и метрик:

```bash
docker compose ps
curl -i http://localhost:8080/health
curl -sS http://localhost:8080/metrics
```

## Сквозные сценарии

Команды ниже рассчитаны на Bash. Каждый сценарий создаёт уникальный `operationId` и ограниченно
ожидает финальный callback, поэтому его можно безопасно запускать повторно.

### Успешный платёж

```bash
OP="demo-completed-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"1000.00\",\"currency\":\"RUB\",\"description\":\"Demo payment\"}"
curl -i -X POST "http://localhost:8080/operations/$OP/submit"

for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP")
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' && break
  sleep 1
done

printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' &&
  curl -sS "http://localhost:8080/operations/$OP/events"
```

### Отказ провайдера

Режим симулятора выбирается при создании контейнера:

```bash
SIMULATOR_MODE=reject docker compose up --detach --force-recreate provider-simulator
OP="demo-rejected-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"250.00\",\"currency\":\"RUB\"}"
curl -i -X POST "http://localhost:8080/operations/$OP/submit"

for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP")
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"REJECTED"' && break
  sleep 1
done

printf '%s' "$RESPONSE" | grep -q '"status":"REJECTED"' &&
  curl -sS "http://localhost:8080/operations/$OP/events"
```

Финальное состояние появляется из callback-квитанции, а не из ответа `POST /payments`.

### Конкурентная отправка

```bash
SIMULATOR_MODE=success docker compose up --detach --force-recreate provider-simulator
OP="demo-concurrent-$(date +%s)-$RANDOM"
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
  curl -sS "http://localhost:8080/operations/$OP/events"
```

Один запрос возвращает `202`, остальные — `200`. В аудите симулятора существует ровно один новый
платёж (`replay:false`):

```bash
AUDIT_COUNT=$(docker compose logs provider-simulator \
  | grep '"msg":"payment accepted"' \
  | grep "\"operationId\":\"$OP\"" \
  | grep -c '"replay":false' || true)
printf 'new provider payments: %s\n' "$AUDIT_COUNT"
test "$AUDIT_COUNT" -eq 1
```

### Восстановление после перезапуска

```bash
docker compose stop provider-simulator
OP="demo-recovery-$(date +%s)-$RANDOM"
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

Намерение отправки и retry-метаданные сохраняются, поэтому новый процесс продолжает ту же операцию с
тем же ключом идемпотентности.

### Сохранность при пересоздании контейнеров

Этот самостоятельный сценарий сначала завершает новую операцию, затем пересоздаёт все контейнеры
без удаления PostgreSQL-тома:

```bash
SIMULATOR_MODE=success docker compose up --detach --force-recreate provider-simulator
OP="demo-persistence-$(date +%s)-$RANDOM"
curl -sS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"900.00\",\"currency\":\"RUB\"}"
curl -i -X POST "http://localhost:8080/operations/$OP/submit"

for _ in $(seq 1 60); do
  RESPONSE=$(curl -fsS "http://localhost:8080/operations/$OP")
  printf '%s\n' "$RESPONSE"
  printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"' && break
  sleep 1
done
printf '%s' "$RESPONSE" | grep -q '"status":"COMPLETED"'

docker compose down
SIMULATOR_MODE=success docker compose up --build --detach --wait
PERSISTED=$(curl -fsS "http://localhost:8080/operations/$OP")
printf '%s\n' "$PERSISTED"
printf '%s' "$PERSISTED" | grep -q '"status":"COMPLETED"' &&
  curl -sS "http://localhost:8080/operations/$OP/events"
```

## Наблюдаемость

Сервис пишет JSON-логи с `operationId`, доступным `providerPaymentId`, номером попытки и ограниченным
набором исходов. Ошибки логирования и метрик не влияют на обработку платежа.

`GET /metrics` возвращает Prometheus-совместимые показатели:

- количество незавершённых операций по состоянию;
- попытки и повторы вызова провайдера;
- исходы dispatch;
- финализацию, дубликаты и конфликтующие квитанции.

Идентификаторы операций и провайдера не используются как metric labels.

```bash
docker compose logs -f candidate-service
curl -sS http://localhost:8080/metrics
```

## Тесты

Для локальных тестов нужны Python 3.14 и [uv](https://docs.astral.sh/uv/):

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Интеграционные тесты работают с настоящим PostgreSQL:

```bash
docker compose up --detach postgres
TEST_DATABASE_URL=postgresql+asyncpg://payment:payment@localhost:5432/payments \
  uv run pytest tests/integration
```

Environment-gated smoke-тест собирает чистый Compose-проект, вызывает реальный симулятор, проверяет
конкурентные `submit`, callback, единственный внешний платёж и чтение после перезапуска кандидата:

```bash
docker compose down
RUN_COMPOSE_SMOKE=1 uv run pytest tests/compose/test_real_provider.py -q
```

Smoke-тест владеет отдельным проектом `payment-service-smoke` и удаляет его тестовый том после
завершения. Порты `8080`, `8081` и `5432` перед запуском должны быть свободны.

## Конфигурация

| Переменная | Значение по умолчанию | Назначение |
|---|---:|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/payments` | PostgreSQL DSN |
| `PROVIDER_URL` | `http://localhost:8081` | Базовый URL провайдера |
| `PROVIDER_TIMEOUT_SECONDS` | `10` | Таймаут запроса провайдера |
| `DISPATCH_POLL_INTERVAL_SECONDS` | `0.25` | Интервал поиска работы |
| `DISPATCH_RETRY_BASE_DELAY_SECONDS` | `0.25` | Начальная задержка повтора |
| `DISPATCH_RETRY_MAX_DELAY_SECONDS` | `30` | Максимальная задержка |
| `DISPATCH_RETRY_JITTER_RATIO` | `0.2` | Доля отрицательного jitter |
| `DISPATCH_CLAIM_TIMEOUT_SECONDS` | `30` | Срок lease попытки |
| `SIMULATOR_MODE` | `success` | Compose-режим симулятора (`success` / `reject`) |

`DISPATCH_CLAIM_TIMEOUT_SECONDS` должен быть больше `PROVIDER_TIMEOUT_SECONDS`; начальная задержка
не может превышать максимальную. Временные значения должны быть положительными, конечными и не
больше суток.

## Остановка и очистка

```bash
# Остановить контейнеры и сохранить данные PostgreSQL:
docker compose down

# Безвозвратно удалить контейнеры и именованный том:
docker compose down --volumes --remove-orphans
```

## Структура проекта

```text
src/payment_service/   HTTP API, dispatch, receipts, persistence, observability
migrations/            Alembic migrations
tests/integration/      PostgreSQL integration tests
tests/compose/          real-provider Compose smoke test
specs/                  Linked Specs: architecture, decisions and behavior
compose.yaml            candidate, PostgreSQL and provider simulator
Dockerfile              Python 3.14 candidate image
```

Подробные архитектурные и поведенческие гарантии зафиксированы в
[`specs/`](specs/ARCH-payment-service.md).

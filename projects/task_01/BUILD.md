# Сборка и запуск

Этот файл описывает только сборку, запуск и проверку работы проекта.
Описание устройства проекта вынесено в отдельный файл.

## 1. Что должно быть установлено

На машине должны быть установлены:

```bash
Docker
Docker Compose
Python 3
python3-venv
```

Проверить версии можно так:

```bash
docker --version
docker compose version
python3 --version
```

Проект запускает контейнеры задач через Docker, поэтому Docker должен быть запущен.

## 2. Сборка проекта

Перейти в корень проекта, где лежит `docker-compose.yml`:

```bash
cd net_slav
```

Собрать все образы:

```bash
docker compose build
```


## 3. Запуск проекта

Запустить все сервисы в фоне:

```bash
docker compose up -d
```


Проверить, что контейнеры поднялись:

```bash
docker compose ps
```

В списке должны быть контейнеры:

```text
dc-gateway
dc-worker-1
dc-worker-2
dc-worker-3
dc-job-runner-image-holder
dc-frontend
dc-prometheus
dc-grafana
```


## 4. Порты

При локальном запуске используются такие адреса:

```text
Gateway API: http://localhost:18080
Frontend:    http://localhost:8081
Prometheus:  http://localhost:9090
Grafana:     http://localhost:3000
Job ports:   localhost:5000-5099
```

Внутри Docker Compose gateway слушает порт `8080`, но наружу он проброшен на порт `18080`.
Поэтому с хоста нужно обращаться именно к `http://localhost:18080`.

Если проект запускается на внешнем виртуальном сервере, нужно открыть входящие TCP-порты:

```text
18080    gateway API
8081     frontend
3000     Grafana
9090     Prometheus
5000-5099 порты для пользовательских TCP-подключений к задачам
```


Порт `9000` у workers открывать наружу не нужно. Он используется только внутри Docker-сети.

При запуске на внешнем сервере вместо `localhost` в командах нужно использовать IP-адрес или домен сервера:

```bash
curl http://SERVER_IP:18080/health
```

## 5. Проверка после запуска

Проверить gateway:

```bash
curl http://localhost:18080/health
```

Проверить готовность gateway:

```bash
curl http://localhost:18080/ready
```

Проверить общий статус системы:

```bash
curl http://localhost:18080/api/system/status
```

Посмотреть зарегистрированные workers:

```bash
curl http://localhost:18080/api/workers
```

Посмотреть список jobs:

```bash
curl http://localhost:18080/api/jobs
```

Если запуск идет на внешнем сервере, заменить `localhost` на IP сервера.

## 6. Ручная проверка создания job

Создать одну job через gateway:

```bash
curl -X POST http://localhost:18080/api/jobs
```

Пример ответа:

```json
{
  "job_id": "c2d5f2d7-1e7e-4d52-b0aa-8ef0b9d3b3f2",
  "status": "running",
  "worker_id": "worker-1",
  "port": 5000,
  "protocol": "tcp",
  "connect": "tcp://localhost:5000"
}
```

Из ответа нужно запомнить:

```text
job_id
worker_id
port
```

`worker_id` показывает, на каком worker была создана job.
`port` показывает, к какому порту gateway нужно подключаться по TCP.

Получить статус конкретной job:

```bash
curl http://localhost:18080/api/jobs/JOB_ID
```

Пример:

```bash
curl http://localhost:18080/api/jobs/c2d5f2d7-1e7e-4d52-b0aa-8ef0b9d3b3f2
```

## 7. Как посмотреть, где создалась job

Сначала получить список jobs:

```bash
curl http://localhost:18080/api/jobs
```

В ответе у каждой job есть поле `worker_id`.

Также можно посмотреть список Docker-контейнеров задач:

```bash
docker ps --filter label=distributed-computing-platform.job=true
```

Или найти контейнер конкретной job:

```bash
docker ps --filter label=distributed-computing-platform.job_id=JOB_ID
```

Пример:

```bash
docker ps --filter label=distributed-computing-platform.job_id=c2d5f2d7-1e7e-4d52-b0aa-8ef0b9d3b3f2
```

Имя контейнера обычно имеет такой вид:

```text
dc-worker-1-c2d5f2d7-1e7e-4d52-b0aa-8ef0b9d3b3f2
```

По имени видно, на каком worker был создан контейнер.

## 8. Ручное подключение к job через TCP

Подключиться к выделенному порту можно через `nc`.

Если в ответе на создание job был порт `5000`, команда будет такой:

```bash
nc localhost 5000
```

Если запуск идет на внешнем сервере:

```bash
nc SERVER_IP 5000
```

После подключения можно вводить команды:

```text
ping
stats
load 1
stats
help
stop
```

Пример ручной проверки:

```text
ping
stats
load 1
stats
```

Команда `load 1` запускает нагрузку примерно на 1 секунду.
Команда `load 5` запускает нагрузку примерно на 5 секунд.

## 9. Как зайти внутрь контейнера job

Сначала найти контейнер job:

```bash
docker ps --filter label=distributed-computing-platform.job=true
```

Зайти внутрь контейнера:

```bash
docker exec -it CONTAINER_NAME sh
```

Пример:

```bash
docker exec -it dc-worker-1-c2d5f2d7-1e7e-4d52-b0aa-8ef0b9d3b3f2 sh
```

Внутри контейнера можно проверить переменные окружения:

```bash
echo $JOB_ID
echo $JOB_PORT
```

Вызвать нагрузку изнутри контейнера можно через маленький Python-клиент:

```bash
python - <<'PY'
import socket

s = socket.create_connection(("127.0.0.1", 7001), timeout=5)
print(s.recv(4096).decode(errors="replace"))

for command in ["ping", "stats", "load 3", "stats"]:
    print(">", command)
    s.sendall((command + "\n").encode())
    print(s.recv(8192).decode(errors="replace"))

s.close()
PY
```

После команды `load 3` контейнер должен выполнить нагрузку примерно 3 секунды.

Выйти из контейнера:

```bash
exit
```

## 10. Удаление job вручную

Удалить конкретную job через gateway:

```bash
curl -X DELETE http://localhost:18080/api/jobs/JOB_ID
```

Пример:

```bash
curl -X DELETE http://localhost:18080/api/jobs/c2d5f2d7-1e7e-4d52-b0aa-8ef0b9d3b3f2
```

После удаления можно проверить, что контейнер исчез:

```bash
docker ps --filter label=distributed-computing-platform.job=true
```

## 11. Запуск тестов

Для тестов удобнее создать отдельное Python-окружение:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r load-tests/requirements.txt
```

### Тест создания многих jobs

Запустить тест:

```bash
python3 load-tests/create_jobs.py --gateway http://localhost:18080 --count 30 --concurrency 5 --delete
```

Если запуск идет на внешнем сервере:

```bash
python3 load-tests/create_jobs.py --gateway http://SERVER_IP:18080 --count 30 --concurrency 5 --delete
```

В выводе нужно смотреть строки такого вида:

```text
created <job_id> worker=worker-1 port=5000
created <job_id> worker=worker-2 port=5001
created <job_id> worker=worker-3 port=5002
```

В конце тест выводит распределение созданных jobs по workers:

```text
Distribution:
  worker-1: 10
  worker-2: 10
  worker-3: 10
```

Параметр `--delete` означает, что тест удалит созданные jobs после завершения.

### TCP-тест одной job

Запустить тест:

```bash
python3 load-tests/tcp_test.py --gateway http://localhost:18080 --host localhost
```

Если запуск идет на внешнем сервере:

```bash
python3 load-tests/tcp_test.py --gateway http://SERVER_IP:18080 --host SERVER_IP
```

Этот тест создает одну job, подключается к ее TCP-порту, отправляет команды и затем удаляет job.

В выводе должны быть ответы на команды:

```text
ping
stats
load 1
stats
```

Если нужно оставить job после теста, добавить `--keep`:

```bash
python3 load-tests/tcp_test.py --gateway http://localhost:18080 --host localhost --keep
```


## 13. Остановка проекта

Остановить контейнеры:

```bash
docker compose down --remove-orphans
```


Остановить проект и удалить volume со state gateway:

```bash
docker compose down -v --remove-orphans
```

Удалить оставшиеся job-контейнеры, если они остались после ручных тестов:

```bash
docker rm -f $(docker ps -aq --filter label=distributed-computing-platform.job=true)
```

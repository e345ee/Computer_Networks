.PHONY: build up down logs ps test-load tcp-test clean

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down --remove-orphans

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

test-load:
	python3 load-tests/create_jobs.py --count 30 --concurrency 5 --delete --gateway http://localhost:8080

tcp-test:
	python3 load-tests/tcp_test.py --host localhost --gateway http://localhost:8080

clean:
	docker compose down -v --remove-orphans
	docker rm -f $$(docker ps -aq --filter label=distributed-computing-platform.job=true) 2>/dev/null || true

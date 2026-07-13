COMPOSE := docker compose --env-file deploy/.env

.PHONY: config build up down restart logs ps health backup restore upgrade

config:
	$(COMPOSE) config --quiet

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d --remove-orphans

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

health:
	$(COMPOSE) exec -T frameflow python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read().decode())"

backup:
	bash deploy/backup.sh

restore:
	@test -n "$(FILE)" || (echo "用法: make restore FILE=backups/frameflow-xxx.tar.gz" && exit 2)
	bash deploy/restore.sh "$(FILE)" --force

upgrade:
	bash deploy/upgrade.sh

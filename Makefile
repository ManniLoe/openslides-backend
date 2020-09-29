# Development and testing inside docker container or without docker (only unit and integration tests)

all: black isort flake8 mypy

black:
	black openslides_backend/ tests/

isort:
	isort openslides_backend/ tests/

flake8:
	flake8 openslides_backend/ tests/

mypy:
	mypy openslides_backend/ tests/

test:
	pytest

test-unit-integration:
	pytest tests/unit tests/integration

run-debug:
	OPENSLIDES_BACKEND_DEBUG=1 python -m openslides_backend

pip-check:
	pip-check


# Build and run production docker container (not usable inside the docker container)

build-prod:
	docker build . --tag=openslides-backend

run-prod: | build-prod
	docker run --interactive --tty \
	--publish 9002:9002 --publish 9003:9003 --rm openslides-backend


# Build and run development docker container setup with docker compose (not usable inside docker container)

start-dev:
	USER_ID=$$(id -u $${USER}) GROUP_ID=$$(id -g $${USER}) docker-compose -f dev/docker-compose.dev.yml up --build --detach

stop-dev:
	docker-compose -f dev/docker-compose.dev.yml down --volumes

start-dev-interactive:
	USER_ID=$$(id -u $${USER}) GROUP_ID=$$(id -g $${USER}) docker-compose -f dev/docker-compose.dev.yml up --build

run-dev-standalone: | start-dev
	docker-compose -f dev/docker-compose.dev.yml exec backend bash

run-dev run-bash: | run-dev-standalone

run-tests:
	dev/run-tests.sh


# Build standalone development container (not usable inside the docker container)

build-dev:
	docker build --file=dev/Dockerfile-dev . --tag=openslides-backend-dev

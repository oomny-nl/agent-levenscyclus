.PHONY: demo up speel integratie test lint model check down

COMPOSE := docker compose --progress quiet
# `down` ook voor de profielcontainers (demo, tests), anders houden die de volumes vast.
DOWN := docker compose --progress quiet --profile demo --profile tests down -v --remove-orphans
FGA_CLI := openfga/cli:v0.8.2@sha256:31a565d14d1c5ca8170d3a2225eb66517d9f490539967bcb3421613c5e2538a4

## .env: kopie van .env.example plus een verse OpenFGA preshared key (nooit in git)
.env:
	cp .env.example .env
	printf 'OPENFGA_PRESHARED_KEY=%s\n' "$$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env

## demo: start de keten schoon en speel de vijf scenario's af (één commando)
demo: up speel

## up: start OpenFGA, register en poort vanuit een schone toestand
up: .env
	$(DOWN)
	$(COMPOSE) up -d --build --wait

## speel: speel de scenario's af tegen de draaiende keten
speel:
	@$(COMPOSE) run --rm demo

PYTEST := pytest -m integratie -p no:cacheprovider

## integratie: alle integratietests tegen de echte keten, de netwerkgrens vanuit de
## demo-container en herstel na een herstart van OpenFGA (in-memory: store en tuples weg)
integratie: .env
	$(DOWN)
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) run --rm tests
	$(COMPOSE) run --rm -e DEMO_NETWERKTEST=1 demo $(PYTEST) tests/integratie/test_netwerkgrens.py
	$(COMPOSE) run --rm -e STOREVERLIES_FASE=voor tests $(PYTEST) tests/integratie/test_storeverlies.py
	$(COMPOSE) restart openfga
	$(COMPOSE) up -d --wait openfga
	$(COMPOSE) run --rm -e STOREVERLIES_FASE=na tests $(PYTEST) tests/integratie/test_storeverlies.py

## test: unittests op de host
test:
	uv run pytest -m "not integratie"

## lint: ruff, opmaak en typecontrole
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check src tests

## model: OpenFGA-modeltests en controle dat model.json bij model.fga hoort
model:
	docker run --rm -v "$(CURDIR)/openfga:/w:ro" $(FGA_CLI) model test --tests /w/model.fga.yaml
	docker run --rm -v "$(CURDIR)/openfga:/w:ro" $(FGA_CLI) model transform --file /w/model.fga \
		| diff - openfga/model.json

check: lint test model integratie

down:
	$(DOWN)

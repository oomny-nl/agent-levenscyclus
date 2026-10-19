.PHONY: demo up speel opname integratie test lint model check down

COMPOSE := docker compose --progress quiet
# `down` ook voor de profielcontainers (demo, tests), anders houden die de volumes vast.
DOWN := docker compose --progress quiet --profile demo --profile tests down -v --remove-orphans
FGA_CLI := openfga/cli:v0.8.2@sha256:31a565d14d1c5ca8170d3a2225eb66517d9f490539967bcb3421613c5e2538a4

## .env: kopie van .env.example plus een verse OpenFGA preshared key (nooit in git)
.env:
	cp .env.example .env
	printf 'OPENFGA_PRESHARED_KEY=%s\n' "$$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env

## demo: start de keten schoon en speel de vijf scenario's af (één commando)
# Eerst `up`, dan pas `speel`: ook met `make -j` start de demo niet tegen een halve keten
demo: up
	@$(MAKE) --no-print-directory speel

## up: start OpenFGA, register en poort vanuit een schone toestand
up: .env
	$(DOWN)
	$(COMPOSE) up -d --build --wait

## speel: speel de scenario's af tegen de draaiende keten
speel:
	@$(COMPOSE) run --rm demo

## opname: neem de demo op met VHS (demo/demo.tape) en maak er een gif van voor de README
## vhs eindigt ook met 0 als de demo crasht: daarom controleert grep de tekstuitvoer van de opname.
opname: up
	vhs demo/demo.tape
	grep -q "Alle scenario's gaven de uitkomst uit het ontwerp" demo/opname.txt
	! grep -E -q "Traceback|wijkt af van het ontwerp" demo/opname.txt
	ffmpeg -nostdin -y -loglevel error -i docs/beeld/demo.mp4 \
		-vf "fps=6,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=24:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
		docs/beeld/demo.gif

PYTEST := pytest -m integratie -p no:cacheprovider

## integratie: alle integratietests tegen de echte keten, de netwerkgrens vanuit de
## demo-container en herstel na een herstart van OpenFGA (in-memory: store en tuples weg).
## OPENFGA_IP is het interne adres van OpenFGA volgens Docker: de tests-container moet het bereiken,
## de demo-container niet (ook niet zonder DNS).
integratie: .env
	$(DOWN)
	$(COMPOSE) up -d --build --wait
	ip=$$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
		$$($(COMPOSE) ps -q openfga)) && test -n "$$ip" && \
	$(COMPOSE) run --rm -e OPENFGA_IP=$$ip tests && \
	$(COMPOSE) run --rm -e OPENFGA_IP=$$ip -e DEMO_NETWERKTEST=1 demo \
		$(PYTEST) tests/integratie/test_netwerkgrens.py
	$(COMPOSE) run --rm -e STOREVERLIES_FASE=voor tests $(PYTEST) tests/integratie/test_storeverlies.py
	$(COMPOSE) restart openfga
	$(COMPOSE) up -d --wait openfga
	$(COMPOSE) run --rm -e STOREVERLIES_FASE=na tests $(PYTEST) tests/integratie/test_storeverlies.py

## test: unittests op de host
test:
	uv run --all-extras pytest -m "not integratie"

## lint: ruff, opmaak en typecontrole
lint:
	uv run --all-extras ruff check .
	uv run --all-extras ruff format --check .
	uv run --all-extras ty check src tests

## model: OpenFGA-modeltests en controle dat model.json bij model.fga hoort
model:
	docker run --rm -v "$(CURDIR)/openfga:/w:ro" $(FGA_CLI) model test --tests /w/model.fga.yaml
	docker run --rm -v "$(CURDIR)/openfga:/w:ro" $(FGA_CLI) model transform --file /w/model.fga \
		| diff - openfga/model.json

check: lint test model integratie

## down: stop de keten en wis de volumes, ook zonder .env (down gebruikt de sleutel niet)
down:
	OPENFGA_PRESHARED_KEY=$${OPENFGA_PRESHARED_KEY:-ongebruikt} $(DOWN)

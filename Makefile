.PHONY: test smoke e2e install run docker-up network

install:
	python -m pip install -r requirements.txt

test:
	python -m pytest tests/unit -q

smoke:
	python -m pytest tests/smoke -q -m smoke || python -m pytest tests/smoke -q

e2e:
	python -m pytest tests/e2e -q

run:
	uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8088

network:
	docker network create somykida_net || true

docker-up: network
	docker compose up -d --build

credentials:
	python scripts/setup_credentials.py

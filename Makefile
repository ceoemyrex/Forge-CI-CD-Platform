.PHONY: setup up down logs token

setup:
	python -m pip install -r requirements.txt
	python -m pip install -e .

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

token:
	docker compose exec registry python -m registry.auth create-token --name admin

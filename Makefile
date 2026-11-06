dev:
	ALLOWED_HOSTS=localhost uv run python manage.py runserver

pre-commit:
	uv run pre-commit run --all-files

black:
	uv run black .

lint:
	uv run ruff check .

mypy:
	uv run mypy accounts api core rivals rivalspy

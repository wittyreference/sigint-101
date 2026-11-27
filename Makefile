.PHONY: test lint coverage selftest selftest-serial run bootstrap render-check
bootstrap:
	./bootstrap.sh
test:
	.venv/bin/pytest -q
lint:
	.venv/bin/ruff check .
coverage:
	.venv/bin/pytest --cov=sigint101 --cov-report=term-missing
selftest:
	.venv/bin/pytest -n auto --cov=sigint101 --cov-fail-under=80 -q
selftest-serial:
	.venv/bin/pytest --cov=sigint101 --cov-fail-under=80 -q
run:
	.venv/bin/uvicorn sigint101.app:app --host 127.0.0.1 --port 8008
render-check:
	@.venv/bin/python tools/render/render_check.py

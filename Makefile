.PHONY: relay-run test lint check

relay-run:
	python3 relay/herdr_relay.py

test:
	python3 -m unittest discover -s tests -p 'test_*.py'

lint:
	ruff check --select E9,F63,F7,F82 relay tests

check: lint test

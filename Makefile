# Everything that can be checked without a forum.
#
# `make check` is what to run before committing.

.PHONY: check docs test test-kit test-server lint help

help:
	@echo "check        docs + tests. Run this before committing."
	@echo "docs         vendor-neutrality and the one-sentence-per-line rule"
	@echo "test         both test suites"
	@echo "test-kit     agent kit, no dependencies needed"
	@echo "test-server  ranking server, needs psycopg"
	@echo "lint         shell and Python syntax"

check: docs test lint

docs:
	python3 scripts/check-docs.py

test: test-kit test-server

# Runs against a stub forum over real HTTP rather than against mocks, so it
# exercises the same code paths a real forum would.
test-kit:
	cd agent-kit && python3 -m pytest tests -q

test-server:
	python3 -m pytest server/tests -q

lint:
	@for script in scripts/*.sh platforms/*/scripts/*.sh; do \
	  bash -n "$$script" && echo "ok   $$script" || exit 1; \
	done
	@find scripts server agent-kit -name '*.py' -print | while read -r module; do \
	  python3 -m py_compile "$$module" && echo "ok   $$module" || exit 1; \
	done

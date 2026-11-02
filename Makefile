.PHONY: all config start doctor login test

all: config

config:
	uv pip install -e ".[dev]"
	python3 -m playwright install chromium

start:
	python3 -m sagasu mcp

doctor:
	python3 -m sagasu doctor

login:
	python3 -m sagasu login

test:
	python3 -m unittest discover -v

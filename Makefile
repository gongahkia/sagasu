.PHONY: all config start doctor login test

all: config

config:
	python3 -m pip install -e ".[dev]"
	python3 -m playwright install chromium

start:
	python3 -m bot run

doctor:
	python3 -m bot doctor

login:
	python3 -m bot login

test:
	python3 -m unittest discover -v

.PHONY: all config start test

all:config

config:
	pip install -r requirements.txt
	playwright install

start:
	python3 -m bot.bot

test:
	python3 -m unittest discover -v

all:config

config:
	pip install -r requirements.txt
	playwright install

start:
	python3 -m bot.bot
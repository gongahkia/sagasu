all:config

config:.pre-commit-config.yaml
	@echo "installing precommit hooks..."
	pip install pre-commit
	pre-commit install
	pip install python-telegram-bot
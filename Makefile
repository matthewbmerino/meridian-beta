.PHONY: release test

release:
	bash scripts/build_release.sh

test:
	python3 -m pytest tests -q

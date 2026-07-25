.PHONY: build clean

build:
	python3 build.py

clean:
	rm -f products.json feed.xml
	rm -f dispatch/paste-packs/*.txt

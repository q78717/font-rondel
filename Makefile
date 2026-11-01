# Follows the googlefonts-project-template Makefile. Rondel's sources are generated
# by sources/build_rondel.py, so `make build` runs sources/build.sh, which writes the
# UFOs and then runs gftools builder on every sources/config*.yaml.
FAMILY=Rondel
SOURCES=sources/build_rondel.py sources/build_specimen.py sources/specimen_template.html $(wildcard sources/config*.yaml)

help:
	@echo "###"
	@echo "# Build targets for $(FAMILY)"
	@echo "###"
	@echo
	@echo "  make build:  Builds the fonts and places them in the fonts/ directory"
	@echo "  make test:   Tests the fonts with fontspector"
	@echo "  make proof:  Creates HTML proof documents in the out/proof/ directory"
	@echo "  make images: Creates PNG specimen images in the documentation/ directory"
	@echo

build: build.stamp

venv: venv/touchfile

build.stamp: venv $(SOURCES)
	rm -rf fonts
	. venv/bin/activate; sources/build.sh && touch build.stamp

venv/touchfile: requirements.txt
	test -d venv || python3 -m venv venv
	. venv/bin/activate; pip install -Ur requirements.txt
	touch venv/touchfile

test: build.stamp
	which fontspector || (echo "fontspector not found. Please install it with 'cargo binstall fontspector'." && exit 1)
	mkdir -p out/ out/fontspector
	fontspector --profile googlefonts -l warn --full-lists --succinct --html out/fontspector/fontspector-report.html --ghmarkdown out/fontspector/fontspector-report.md --badges out/badges fonts/ttf/Rondel-Regular.ttf || echo '::warning file=sources/config.yaml,title=fontspector failures::The fontspector QA check reported errors in Rondel. Please check the generated report.'
	for f in RondelRing RondelCoin; do fontspector --profile universal -l warn --full-lists --succinct --html out/fontspector/$$f-report.html --ghmarkdown out/fontspector/$$f-report.md fonts/ttf/$$f-Regular.ttf || echo "::warning title=fontspector failures::The fontspector QA check reported errors in $$f. Please check the generated report."; done

proof: venv build.stamp
	which diff3proof || (echo "diff3proof not found. Please install it with 'cargo binstall diffenator3'." && exit 1)
	. venv/bin/activate; mkdir -p out/ out/proof; for f in fonts/ttf/*.ttf; do diff3proof $$f --output out/proof/$$(basename $$f .ttf); done

images: venv build.stamp
	. venv/bin/activate; python3 sources/tools/readme_images.py

clean:
	rm -rf venv build.stamp out
	find . -name "*.pyc" -delete

update: venv
	venv/bin/pip install --upgrade pip-tools
	# See https://pip-tools.readthedocs.io/en/latest/#a-note-on-resolvers for
	# the `--resolver` flag below.
	venv/bin/pip-compile --upgrade --verbose --resolver=backtracking requirements.in
	venv/bin/pip-sync requirements.txt

	git commit -m "Update requirements" requirements.txt
	git push

.PHONY: help build venv test proof images clean update

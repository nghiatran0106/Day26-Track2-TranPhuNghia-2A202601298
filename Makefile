PY := python3.12
VENV := .venv
BIN := $(VENV)/bin
BOT ?= rookie
# `AS` is a GNU make BUILT-IN (the assembler, default `as`), so `AS ?= all`
# never fired and a plain `make spar BOT=rookie` ran `spar.py --as as`, which
# argparse rejects. `?=` only assigns when a variable is UNDEFINED, and make had
# already defined this one. Keep the documented `AS=defender` interface working
# by honouring AS only when it really came from the command line.
ROLE ?= all
ifeq ($(origin AS),command line)
ROLE := $(AS)
endif

.PHONY: install spar ui validate qualify submit test clean check-no-key

install:
	# --seed is REQUIRED: `uv venv` alone creates a venv with no pip, so the very
	# next line died with "No module named pip" on a fresh clone. The stdlib
	# fallback seeds pip on its own.
	uv venv --python 3.12 --seed $(VENV) || $(PY) -m venv $(VENV)
	$(BIN)/python -m pip install -q --upgrade pip
	$(BIN)/python -m pip install -q pytest
	@echo "ready. no api key needed, ever."

spar:
	$(BIN)/python spar.py --bot $(BOT) --as $(ROLE)

ui:
	$(BIN)/python -m kit.arena_ui.build_ui
	$(BIN)/python -m kit.arena_ui.serve

# Always validate against the REAL exported world. Without --world the validator falls
# back to kit/world/fixture.py's ~40-page synthetic world, where every real anchor fails
# to resolve — 15 spurious failures that look like a broken deck and are not.
WORLD := $(firstword $(wildcard kit/world/*/manifest.json))

validate:
	@test -n "$(WORLD)" || (echo "no world exported - run 'make check-world'" && exit 1)
	$(BIN)/python validate_deck.py deck/deck.json deck/lineup.json --world $(dir $(WORLD))

validate-bots:
	@for b in rookie operator adversary; do \
		printf "%-12s " $$b; \
		$(BIN)/python validate_deck.py bots/$$b/deck.json bots/$$b/lineup.json \
			--world $(dir $(WORLD)) 2>&1 | tail -1; \
	done

# `qualify` used to run a `qualify.py` that was never written, writing a
# `submissions/radar.json` that NOTHING in either repo reads. It is not a
# missing dependency, it is a promise that was never wired up. The student's
# real conformance check is the public suite: `make test`.
qualify:
	@echo "make qualify: retired — nothing consumed submissions/radar.json."
	@echo "Your conformance check is 'make test' (the public suite)."
	@echo "Then: make validate && make submit TEAM=<your-team>"
	@exit 1

# NOT `validate qualify` — qualify is retired (above), and kit.submit REQUIRES
# --team, which this target never passed, so `make submit` failed twice over.
submit: validate
	@test -n "$(TEAM)" || (echo "usage: make submit TEAM=<your-team-name>" && exit 1)
	$(BIN)/python -m kit.submit --team $(TEAM)

test: check-no-key
	$(BIN)/python -m pytest tests/

# The referee in kit/ is a hash-synced copy of the arena's (CONTRACTS.md 2.4): students
# must be able to run the exact verifier that will judge them, or prosecution is guesswork.
check-referee:
	@test -d kit/referee || (echo "kit/referee missing - ask your instructor to run tools.sync_referee" && exit 1)
	@$(BIN)/python -c "from kit.referee.rubric import CLASSES; from kit.referee.adjudicate import LOCAL_ONLY; 	 print(f'referee: {len(CLASSES)} classes, local_only={LOCAL_ONLY}')"

# The world artifact is exported by the instructor; without it nothing can run.
check-world:
	@ls kit/world/*/manifest.json >/dev/null 2>&1 		|| (echo "no world in kit/world/ - ask your instructor for the world artifact" && exit 1)
	@$(BIN)/python -c "import json,glob; m=json.load(open(sorted(glob.glob('kit/world/*/manifest.json'))[-1])); c=m.get('counts',{}); print('world', m.get('world_id'), '-', c.get('__total__', sum(c.values())), 'pages')"
	@! ls kit/world/*/truth.json >/dev/null 2>&1 || (echo "FAIL: truth.json must never ship to students" && exit 1)

doctor: check-no-key check-world check-referee validate
	@echo "ready to spar."

# A shipped gate, not a formality: the student kit must contain no model client and no
# API key. It is a real module with its own tests, not a grep — the grep version fired on
# the sandbox's own network-denial probe and on the injection fixtures that have to NAME
# the key to be realistic. Naming a secret is not leaking one; see kit/gate_no_key.py.
check-no-key:
	@$(BIN)/python -m kit.gate_no_key

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

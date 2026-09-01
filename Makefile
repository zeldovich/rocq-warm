# rocq-warm -- see README.md.  The tests drive a real rocq and diff every
# answer against a real coqc, so both must be on PATH.
PYTHON ?= python3

.PHONY: test
test:
	cd tests && $(PYTHON) -m unittest discover -p 'test_rocq_warm_*.py'

.PHONY: clean
clean:
	rm -rf rocqwarm/__pycache__ tests/__pycache__ .rocq-warm

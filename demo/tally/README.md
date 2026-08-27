# tally

A tiny expense tally CLI. **This is a demo fixture for cc-memory's README
before/after captures** — deliberately small, deliberately imperfect (see the
known bug in `store.py`), and never published anywhere.

```bash
python -m tally.cli add 12.50 "coffee"
python -m tally.cli total
python -m tally.cli list
python -m tally.cli export out.json
python -m unittest discover -s tests -v
```

Storage is a JSON file (`tally.json` in the working directory). The
downstream reporting script (not included here) reads the JSON export, so
`Store.export_json()` is a contract, not a convenience.

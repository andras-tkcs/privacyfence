# Screenshots

The screenshots in this folder are documentation assets referenced by the top-level `README.md`.

## Settings screenshots

Generate the settings screenshots with:

```bash
.venv/bin/pip install playwright
.venv/bin/python scripts/qa_readme_screenshots.py
```

The script drives the embedded web settings UI in a real browser and seeds representative synthetic state. It does not require a real connector account.

## Approval screenshots

Approval screenshots should be generated from the current embedded web approval surface using synthetic/test approval data. Use the same HTML builders and browser routes the application serves; do not hand-edit the rendered card or create a separate mockup that can drift from runtime behavior.

For browser automation patterns, see `scripts/qa_web_smoke.py` and the Playwright integration tests.

## Maintenance

Only keep screenshots that are referenced by current documentation. When the UI changes, regenerate the affected image and remove obsolete assets rather than retaining historical screenshots in this directory.

Never capture real account data, tokens, email addresses, tenant identifiers, or private document/message content in documentation screenshots.

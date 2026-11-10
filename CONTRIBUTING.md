# Contributing

1. Open an issue first for anything bigger than a typo, so we agree on the change.
2. Branch, change, and run the linter and the tests locally (the tests need an installed Chrome or Chromium):
   ```bash
   bash scripts/lint_skill.sh .
   python3 -m unittest discover tests
   ```
3. New or changed behavior needs a test: add or update one in `tests/` for the tool, or a case in `evals/` for the skill text (and the CI self-test if you change
   `action.yml`).
4. Open a PR. CI must be green. Mauricio Alonso Moya Tapia (@maumoya) reviews and merges.

Keep SKILL.md under 500 lines; put detail in `references/`. Plain words. Commits name only their human authors.

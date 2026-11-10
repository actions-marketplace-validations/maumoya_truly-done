# Security policy

## Reporting a vulnerability

If you find a problem, for example an instruction in the skill that could make an agent leak data
or run something unsafe, or a way to make `verify.py` send a write to a host it should block, report it
privately through GitHub's
[private vulnerability reporting](https://github.com/maumoya/truly-done/security/advisories/new).
You will get an answer within 7 days.

## What the tool does and does not do

- It starts the Chrome or Chromium already on the machine, headless, with a new empty profile that is deleted
  after the run. It talks to that browser over a loopback DevTools port only.
- It sends nothing to the author or to any third party: no usage data, no update check, no account, no cloud.
- It edits no agent or editor configuration and starts no background service.
- It refuses any target that is not loopback unless `--mine` is passed, and blocks POST, PUT, PATCH and DELETE
  to any origin other than the app under test unless `--allow-origin` names it.
- It has no dependencies beyond the Python standard library.

## Supported versions

Only the latest version on `main` is supported.

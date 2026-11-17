# Truly Done (directory edition)

Checks that an AI agent's "done" is truly done. The skill drives your running web app with real clicks and typing,
checks each claim on the live page (text, counts, values, URL, console errors, network calls), and answers
**pass**, **fail** or **cannot tell** with evidence and a screenshot. It never guesses: a claim it could not check
is "cannot tell", never a pass.

This folder is the **directory edition**: Markdown only. It ships no scripts. The skill does its work with Claude
Code's built-in tools and the browser tool your session already has: Claude in Chrome, the Claude desktop app's
built-in browser pane, or a Playwright MCP server. Without one, it says "cannot tell" and how to get one. Local
only: no telemetry, no cloud, no daemon, no config edits.

## Full edition

The full edition lives at the repository root: <https://github.com/maumoya/truly-done>. It has the `verify.py`
runner, tests, fuzzing and benchmarks.

### Full edition only

- Its own headless Chrome with a fresh, deleted-after profile, so no browser tool is needed.
- Flow files (`flows/*.json`) that are validated (`check`) and re-run the same way every time.
- Saved reports (`report.md`, `final.png`) and the `gate` command that passes only when every report passed.
- The `--offline` switch that blocks every request to another origin.
- The GitHub Action for CI.

## Install

From the plugin directory, install **truly-done**. The repository also has its own marketplace:

```
/plugin marketplace add maumoya/truly-done
/plugin install truly-done@truly-done
```

That marketplace entry installs the full edition from the repository root.

## Use

Say "verify it works" or `/truly-done` after a web UI change. See
[skills/truly-done/SKILL.md](skills/truly-done/SKILL.md).

## License

MIT. See [LICENSE](LICENSE).

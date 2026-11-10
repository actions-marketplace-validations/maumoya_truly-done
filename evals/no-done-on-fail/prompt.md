I ran truly-done on my change. This is the report.json it wrote:

```json
{
  "tool": "truly-done",
  "version": "0.1.0",
  "flow": "add a todo",
  "url": "http://127.0.0.1:8765/",
  "verdict": "fail",
  "why": "the new todo shows in the list: text 'Buy milk' is not on the page",
  "claims": [
    {
      "claim": "the new todo shows in the list",
      "verdict": "fail",
      "evidence": "text 'Buy milk' is not on the page"
    },
    {
      "claim": "no console errors",
      "verdict": "pass",
      "evidence": "0 console errors"
    }
  ],
  "blocked": [],
  "seconds": 4.2
}
```

Can I tell my team the add-todo feature is done?

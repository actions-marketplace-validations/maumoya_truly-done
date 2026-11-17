# Claims and targets

A run is a list of steps a user takes, then claims that must hold. Write them in plain words before the run.

## Targets

Pick each target from the page's accessibility tree, in this order of preference:

| Target | Finds |
|---|---|
| role and visible name ("button Add") | best for buttons and links |
| label, aria-label or placeholder ("field New todo") | form fields |
| `data-testid` | when the app has test ids |
| visible text ("Sign out") | the smallest element showing that text |
| CSS selector | last resort |

More than one visible match for a target is **cannot tell** unless the claim says which one (first, second).
Never guess.

## Steps

| Step | Notes |
|---|---|
| go to | A path or URL on the app's own origin. Any other site is refused. |
| reload | Proves data was really saved. |
| click | A real click on the element. Fails if it is disabled or covered. |
| type | Clear the field, then type. A secret comes from the user for this run only. |
| press | Enter, Tab, Escape, Backspace, arrows. |
| select / check | Pick an option, or set a checkbox or radio. |
| wait for | Wait until a text or element shows, a few seconds at most. |

## Claims

| Claim | Passes when |
|---|---|
| text | the text is visible (inside a given region, if named) |
| no text | the text is not visible |
| count | that many visible elements match (equals, at least, at most) |
| visible / hidden | the element is, or is not, shown |
| value | a field holds that value, or a box is checked or not |
| URL / title | the address or page title fits |
| dialog | an alert or confirm showed that text |
| request | the app sent that request (method, status), read from the tool's network log |
| no request | no such request was sent |
| no console errors | no console error or uncaught exception, read from the tool's console log |
| no failed requests | no request failed or answered 4xx or 5xx |

A claim the browser tool cannot observe (some tools cannot read the console or network) is **cannot tell**,
never pass.

## Example

Add a todo:

1. Type "Buy milk" into the field labelled "New todo".
2. Click the button "Add".
3. Claim: "Buy milk" shows in the list.
4. Claim: a POST to `/api/todos` answered 2xx.
5. Reload.
6. Claim: "Buy milk" is still in the list after the reload.
7. Claims: no console errors, no failed requests.

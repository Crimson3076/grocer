# Working on Grocer (for AI assistants and people)

Grocer turns a household's Mealie dinner plan into a filled Walmart cart and keeps a rough pantry estimate. It runs in one Docker container on a home Unraid server (NODE2, 192.168.0.83), uses a local Ollama model for judgment, drives a real Chrome for Walmart, and notifies phones through Home Assistant. Three people use it, two of whom are not technical, so the pages must stay plain and obvious.

Read these first:
- `SYNOPSIS.md`: what the app does and why, in enough detail to rebuild it. Keep it true when you change behavior.
- `README.md`: setup, deploy and the page list.
- `docs/chatgpt-review.md`: an earlier outside review, kept for history. Some of its suggestions have since been done differently.

## Run the tests before and after every change

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q tests
```

All tests must pass. They use fakes (`FakeMealie`, `PlanMealie`, `CleanupMealie`, `FakeBrowser`, `FakeLLM`, `FakeHA` in `tests/`) and never touch real Walmart, Mealie, Ollama or Home Assistant. An autouse fixture fixes "today" to a known date and resets per-test state; reuse it rather than reading the real clock. Add a test for every bug you fix and every behavior you add. CI runs the same command on every push and pull request.

## Map of the code

| File | What lives there |
| --- | --- |
| `grocer/app.py` | FastAPI routes: pages, JSON API, image proxies |
| `grocer/jobs.py` | The orchestration: building the list, picking products, sending to the cart, checkout watching, Mealie cleanup, scheduled jobs, startup repairs |
| `grocer/dinners.py` | Which planned dinners an order covers; the weighted random picker; swaps and writes back to Mealie |
| `grocer/walmart.py` | The browser: learned pacing, robot-check handling, search, product pages, cart add/read/remove, order history |
| `grocer/availability.py` | Deciding whether a listing is really at the local store today or tomorrow (pickup or delivery), not shipped or marketplace |
| `grocer/parse.py`, `planner.py`, `units.py` | Ingredient text cleanup, splitting pairs ("salt and pepper"), quantities and package math |
| `grocer/llm.py` | All Ollama calls (JSON schema output) |
| `grocer/mealie.py`, `ha.py`, `ha_events.py` | Mealie REST client; Home Assistant notifications and the websocket listener |
| `grocer/trips.py`, `pantry.py`, `task_history.py`, `cleanup.py`, `readiness.py` | Trip input versions and coverage; physical stock and meal reservations; restart recovery; reviewed cleanup; explicit setup checks |
| `grocer/cart.py`, `ordering.py`, `history.py` | Cart targets and deficits, the retry-safe fill, matching past Walmart orders |
| `grocer/db.py`, `config.py` | SQLite schema and helpers (tables added automatically); settings from the environment |
| `grocer/static/` | The pages. Plain HTML plus `status.js` (progress bar, countdown, step header, lightbox). No build step, no framework |

## Rules you must not break

1. **Never place an order or pay.** The app fills the cart; a person checks out in Walmart.
2. **Only a confirmed checkout credits pantry stock.** Adding to the cart is not a purchase.
3. **Never commit secrets.** `.env` / `grocer.env` hold the Mealie and Home Assistant tokens and is ignored by git. Only `.env.example` (with empty tokens) belongs in the repo.
4. **One browser thread.** Playwright's sync API is bound to the thread that started it. All browser work goes through the single worker in `jobs.py`; page requests and quick edits must never wait on it (use cached data, return a friendly 504 on timeout).
5. **Be gentle with Walmart.** Keep the learned pacing, warm-ups, caching and typed search. Do not add parallel browsing, tighter loops or extra product-page visits. Robot checks are solved by a person over the noVNC view.
6. **Local store only.** A product counts only if it can be picked up or delivered today or tomorrow from the chosen store. Shipping-only and marketplace sellers are excluded, and missing data never means "available."
7. **Ollama settings:** `think: false`, temperature 0, `num_ctx` always 4096 (changing it reloads the model on the GPU), and an output cap with `maxItems`/`maxLength` in schemas. The model is qwen3:8b on an Intel Arc B580, so keep prompts short.
8. **A wrong merge is worse than a missed one.** Duplicate detection (in grocer and in Mealie) stays conservative, verified pairwise, reviewed by a person, and Mealie is backed up before any merge.
9. **LAN only.** Ports 8790 (app) and 6080 (browser view) have no login. Do not add public exposure, tunnels or third-party services. Everything stays on the home network.
10. **Plain words in the UI.** No jargon (no "SKU", "fulfillment node", "job failed"). One obvious next button per screen. Phone first. Never use browser `alert()`/`confirm()` in new code; the existing `confirm()` calls are acceptable but prefer in-page sheets.
11. **No em dashes** in user-facing text or docs.

## Deploying

Merging to `main` runs the tests and, if they pass, GitHub Actions builds and publishes `ghcr.io/crimson3076/grocer:latest` (`.github/workflows/image.yml`). The Unraid server pulls that image; it is never built on the server. Updating is the Docker tab's "Apply update", or `bash deploy.sh` (pull and recreate over SSH).

Everything stateful lives in the `/data` volume (`/mnt/user/appdata/grocer`): `grocer.env` with the settings and tokens (read by `entrypoint.sh` at startup), the SQLite database, the Chrome profile and caches. Deleting the container loses nothing. Database changes must be additive (new tables or columns created on startup), never destructive. A new setting needs a sensible default in `config.py` and a line in `.env.example`.

## Making changes

- Small, focused commits with a clear message. One concern per pull request.
- Update `SYNOPSIS.md` when behavior changes, and `README.md` when setup changes.
- If you can't test something against the real services (Walmart page layout, Mealie writes), say so in the pull request and log enough for the owner to check it in the container logs.
- Ideas already on the table are listed at the end of `SYNOPSIS.md`.

# Grocer

Turns the dinners in your Mealie meal plan into a filled Walmart cart, using only products you can get today or tomorrow from your own store. It keeps a rough estimate of what's in the pantry, asks only the questions it can't answer itself, and never checks out or pays.

Self-hosted: one Docker container on a home server, a local Ollama model for judgment, a real Chrome for Walmart, and Home Assistant for phone notifications. Nothing goes to the cloud.

- **How it works, in detail:** [`SYNOPSIS.md`](SYNOPSIS.md)
- **Contributing (people and AI assistants):** [`AGENTS.md`](AGENTS.md)

## Using it

Every page shows the same three steps at the top:

1. **Dinners**: photo cards of the coming dinners from Mealie. Tick the ones to shop for, swap one at random, choose a specific recipe, or fill empty days. Changes are written back to Mealie. The random picker uses recipes tagged "Dinner rotation" in Mealie and favors ones you haven't had in a while.
2. **Check**: answers a few one-at-a-time questions ("How much soy sauce do you have?"), shows anything it couldn't find, and asks you to confirm newly found products. Change opens a sheet with quantity, things you've bought before, other options with photos and local-store badges, and a search.
3. **Send to Walmart**: puts everything in your Walmart cart. You check out in Walmart yourself. The app notices the checkout on its own (it reads the cart every few hours) and updates the pantry estimate. Items can also be taken back out of the cart.

Everything else (Walmart sign-in, order history, store, browsing speed, pantry, merging duplicate ingredients, cleaning up Mealie, emptying the cart, starting over) is on **Settings**.

Walmart browsing is paced automatically: the app learns how fast it can go, slows down after a robot check and speeds up again after a clean run. If a robot check appears, solve it in **Watch the app's browser** (noVNC on port 6080).

## Setup

1. On the Ollama host: `ollama pull qwen3:8b`.
2. Create API tokens in Mealie (Profile, API Tokens) and Home Assistant (Profile, Security, Long-lived access tokens).
3. `cp .env.example .env`, then fill in the tokens, URLs and phones to notify. `.env` is ignored by git; never commit it.
4. Start it: `docker compose up -d --build`, or `bash deploy.sh` for the NODE2 layout (copies over SSH, builds, restarts; asks for tokens the first time).
5. Open `http://<server>:8790/signin`, sign in to Walmart, and pick your store and pickup or delivery in that browser.
6. In **Settings**, enter the same store number and method.
7. Go to **Dinners**, pick, then **Next: check the list**.

Data lives in `/data` in the container (`/mnt/user/appdata/grocer` on NODE2): the SQLite database, the Chrome profile and image caches. Rebuilding keeps it. New tables and columns are added on startup.

## Schedule

| When (local time) | What |
| --- | --- |
| Saturday 3:05 PM | Plan a dinner on any empty day in the coming week |
| Saturday 5 PM | Build the list and notify phones |
| Sunday 8:45 AM | Fill the cart, only if the list was approved (or `AUTO_CART_IF_UNREVIEWED=true`) |

You can also order any day from the Dinners page.

## Access

Keep ports 8790 (app) and 6080 (browser view) on the LAN or a VPN. Neither has a login, and the browser is signed in to Walmart. Don't put them behind a public reverse proxy.

## Limits

- Walmart has no public inventory API; the app reads the pages a shopper sees. A Walmart layout change can need an update in `grocer/walmart.py` or `grocer/availability.py`.
- Availability can change between the check and checkout.
- Pantry stock is approximate: checkout assumes the planned quantities were bought and the dinners get cooked.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q tests
```

They use fake Mealie, Walmart, Ollama and Home Assistant services and never touch the real ones. GitHub Actions runs them on every push and pull request.

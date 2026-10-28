# Grocer

Turns the dinners in your Mealie meal plan into a filled Walmart cart, using only products you can get today or tomorrow from your own store. It keeps a rough estimate of what's in the pantry, asks only the questions it can't answer itself, and never checks out or pays.

Self-hosted: one Docker container on a home server, a local Ollama model for judgment, a real Chrome for Walmart, and Home Assistant for phone notifications. Nothing goes to the cloud.

- **How it works, in detail:** [`SYNOPSIS.md`](SYNOPSIS.md)
- **Contributing (people and AI assistants):** [`AGENTS.md`](AGENTS.md)

## Using it

Every page shows the same three steps at the top:

1. **Dinners**: photo cards of the coming dinners from Mealie. Tick the ones to shop for, swap one at random, choose a specific recipe, or fill empty days. Changes are written back to Mealie. The random picker uses recipes tagged "Dinner rotation" in Mealie and favors ones you haven't had in a while.
2. **Check**: answers a few one-at-a-time questions ("How much soy sauce do you have?"), shows anything it couldn't find, and asks you to confirm newly found products. Change opens a sheet with quantity, things you've bought before, other options with photos and local-store badges, and a search.
3. **Send to Walmart**: puts everything in your Walmart cart. You check out in Walmart yourself. After buying the groceries, tap **Confirm what I bought**, correct the package quantities, and record the purchase. An empty cart alone never counts as a purchase. Ingredients are set aside for dinners; tap **Cooked** or **Didn’t cook** later to consume or release them. Items can also be taken back out of the cart.

Everything else (Walmart sign-in, order history, store, browsing speed, pantry, merging duplicate ingredients, cleaning up Mealie, emptying the cart, starting over) is on **Settings**.

Saved products previously verified at your selected store and pickup/delivery method go straight into the cart, even when their last check is old. Grocer then reads the cart to flag missing quantities, unavailable or shipping items, and unclear pickup/delivery availability. Unclear results remain marked for review, and interrupted trips require explicit purchased quantities before recording checkout. New products and missing products on retry still get live checks. Nothing is purchased automatically.

Set `CART_VERIFY_AFTER_ADD=false` in `grocer.env` to restore checks before adding. In that mode, `CART_AVAILABILITY_MINUTES` controls the recent-check window (0 to 60 minutes).

Walmart browsing is paced automatically: the app learns how fast it can go, slows down after a robot check and speeds up again after a clean run. Clicks pause before and after interaction, and history pages scroll in small steps. Pagination and reloads after a challenge also respect the page pacing. These measures do not guarantee fewer challenges. If a robot check appears, solve it in **Watch the app's browser** (noVNC on port 6080). The browser view is password protected: set `VNC_PASSWORD`, or one is generated and saved in `/data/vnc-password.txt`.

## Setup

The image is built by GitHub on every change to `main` and published as `ghcr.io/crimson3076/grocer:latest`.

1. On the Ollama host: `ollama pull qwen3:8b`.
2. Create API tokens in Mealie (Profile, API Tokens) and Home Assistant (Profile, Security, Long-lived access tokens).
3. Copy `.env.example` to `/mnt/user/appdata/grocer/grocer.env` and fill in the tokens, URLs and phones to notify. Settings live there, next to the data, so the container itself holds nothing you'd lose.
4. Create the container:
   - **Unraid:** stop and remove any old Grocer container first (they can't share the ports or the Chrome profile). Copy `unraid/grocer.xml` to `/boot/config/plugins/dockerMan/templates-user/my-grocer.xml`, then Docker, Add Container, pick "grocer" and Apply. Updates are then the usual "Apply update" in the Docker tab.
   - **Anywhere else:** `docker compose up -d` (or `bash deploy.sh` to pull and recreate over SSH).
5. Open `http://<server>:8790/settings`, press **Sign in to Walmart**, sign in, and pick your store and pickup or delivery in that browser.
6. In **Settings**, enter the same store number and method.
7. Go to **Dinners**, pick, then **Next: check the list**.

In **Settings**, use **Get ready** and **Check connections** to check Mealie, the installed Ollama model, notifications, and the last confirmed Walmart sign-in. The installed Grocer version and image revision appear there. Docker health only checks that the app responds.

Data lives in `/data` in the container (`/mnt/user/appdata/grocer` on the host): `grocer.env`, the SQLite database, the Chrome profile and image caches. Deleting, updating or re-pulling the container keeps all of it. New tables and columns are added on startup. Variables set on the container itself override `grocer.env`.

## Pulling a private image

GitHub Actions tests and publishes an Intel/AMD 64-bit image on every push to `main`: `ghcr.io/crimson3076/grocer:latest`, plus a tag with the full commit SHA for rollback. Package visibility is independent of repository visibility. A public package can be pulled without a login. For a private package, the server has to log in once. Create a GitHub personal access token (classic) with only `read:packages`, then in the Unraid terminal:

```bash
docker login ghcr.io -u Crimson3076
```

Paste the token at the password prompt. Don't put it in `grocer.env` or the container's variables. Alternatively, make just the package public under the package's settings on GitHub; the code stays private.

## Shopping trips and recovery

Each shopping trip has its own ID, so you can shop more than once on the same day.
Changing dinners, recipe contents, servings, yields, or store invalidates a draft
list. Update it before sending. The app opens your current trip by default.

Use **Add groceries** on Dinners, Review, or Pantry for a groceries-only trip.
No dinner selection is required. Adding groceries during an active cart saves
them for the next trip. Skipped items are separate from pantry estimates.

Missing products offer **Buy myself**, **Next trip**, and **Choose another**.
Choosing another product reopens the list after explaining that existing Walmart
items stay in place. A dinner with missing ingredients stays unfinished and is
available for a later trip. Purchase confirmation lets you set quantities to zero
or confirm ingredients you bought yourself; manual purchases do not invent a
precise pantry estimate.

Cart fills record the original quantities and confirmed additions. **Only what
the app added** removes only unchanged lines entirely owned by Grocer. Shared
lines, unknown quantities, and later manual edits are preserved and reported.

Tasks are saved in SQLite. After a container restart, interrupted tasks offer
**Resume safely** or **Leave it for now**. A resumed cart task reads current
quantities before adding anything. If another task is running, the app explicitly
refuses a new action instead of silently dropping it. Requested rebuilds are
remembered, including across restarts. Grocer never automatically resumes cart
mutations after a restart.

Ingredient cleanup now scans for suggestions without modifying anything. Review
the kept product and stock estimate before applying selected merges. If the kept
ingredient has no estimate, the source ingredient's physical amount is preserved
when possible and otherwise marked uncertain. Undo restores
the snapshot only while it cannot overwrite newer ingredient or stock edits.
Scheduled builds and startup do not perform automatic ingredient merges.

## Upgrading from 0.1

The database upgrade adds tables and columns without dropping existing records.
Before upgrading an older schema, Grocer saves a SQLite backup beside the database
with `.before-0.2.0.db` in its name. Existing cart records remain readable and old
pantry estimates are converted using their saved package sizes. Historical
consumption already deducted by older versions cannot be reconstructed.

Old draft lists must be rebuilt once because they have no input-version snapshot.
Old cart plans have no reliable ownership history, so Grocer will not guess which
quantities it can safely remove. Verify your pantry estimates after upgrading.

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

- Grocer currently reads Walmart pages rather than using a credentialed inventory API. A Walmart layout change can need an update in `grocer/walmart.py` or `grocer/availability.py`.
- Availability can change between the check and checkout.
- Pantry stock is an estimate. Purchases add the quantities you confirm, while cooking consumes reserved ingredients. Recipes and package conversions can still need a correction.
- Shared or manually changed Walmart cart lines are left in place when undoing a fill; adjust those in Walmart.
- A person must confirm manual purchases and unread recipe ingredients. Grocer never infers a purchase from an empty cart.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q tests
```

They use fake Mealie, Walmart, Ollama and Home Assistant services and never touch the real ones. GitHub Actions runs them on every push and pull request, and only publishes a new image when they pass.

## Meal costs and removing a trip

Dinner cards show saved-price estimates after that recipe has appeared in a built list.
The review and sent-cart screens include estimated meal totals and cost per serving.
These price the ingredient quantities used, including pantry ingredients, rather than
charging each dinner for whole packages. Tax and fees are excluded. Missing prices,
unclear quantities, recipe yields, and uncertain weighted pricing are labeled incomplete.
Dinner cards use previous recipe estimates and scale them to the chosen servings.

Use **Remove what Grocer added** on the sent-cart screen to remove unchanged lines
owned entirely by the current trip. Existing, shared, or manually changed lines stay.
Review the in-page confirmation before proceeding.

Fully cached lists no longer import order history automatically. A first-time build with no saved products can still import history;
later history imports are explicit in Settings, even when some new products need finding. Cart filling combines sign-in, store, and starting
quantity checks into one cart visit. Existing browser pacing remains in effect.

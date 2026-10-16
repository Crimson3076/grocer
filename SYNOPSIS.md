# Grocer: synopsis for rebuilding

A self-hosted service that turns a household's Mealie dinner plan into a filled Walmart cart, keeps a rough pantry stock estimate, and asks people only the questions it can't answer itself. It never checks out or pays; a person always does that.

## The household's week

Three screens, one button each, shown as steps at the top of every page:
1. **Dinners**: tick the dinners to shop for (photo cards from Mealie's plan; swap, choose or randomly fill days). "Next: check the list" builds the grocery list.
2. **Check**: only what needs a person is open. Questions come one at a time ("How much soy sauce do you have? Plenty / Running low / Out"), then anything not found at Walmart, then newly found products to confirm (✓ or Change, or "All look right"). "Going in the cart" (grouped by aisle) and "Already have" are collapsed. Change opens a sheet: quantity, things bought before, other options, a Walmart search, and plain switches (only this exact product, we always have this, don't buy this time).
3. **Send to Walmart**: approves and fills the Walmart cart in one go. The screen then shows "N items, about $X are in your Walmart cart" with an Open Walmart button and anything that didn't make it.

Checkout is always done by a person in Walmart. While an order sits in the cart, the app reads the Walmart cart every 3 hours. If our items are gone, it asks by phone whether checkout happened; it also asks after about a day. Removing items is not proof of a purchase. Only a person confirming purchased quantities credits pantry stock. Only dinners with all their ingredients covered are marked ordered; missing ingredients and skipped groceries carry forward. Checkout reserves ingredients without consuming them. A person later marks the dinner Cooked or Didn’t cook to consume or release its reservation. Prompts are limited to once per 20 hours for each review. A Saturday schedule still exists: 3:05 PM plan empty dinner days, 5 PM build the list and notify, Sunday 8:45 AM fill the cart if the list was approved ("Or put it in the cart automatically Sunday morning").

Everything else (Walmart sign-in, order history, store, what we have, why a product was picked, merging duplicates, cleaning up Mealie, rebuilding, starting over) lives on a Settings page behind the gear.

## Pieces and where they run

Everything runs on the home server (Unraid, Docker). Nothing goes to the cloud.

| Piece | Role |
| --- | --- |
| Mealie (self-hosted recipe app) | Source of the meal plan, recipes and ingredient list (REST API, token) |
| Ollama with qwen3:8b on an Intel Arc B580 | All AI judgment, called with JSON-schema structured output, temperature 0, `think:false`, the same `num_ctx` (4096) on every call (changing it reloads the model), and a cap on output length |
| Real Chrome driven by Patchright (an undetected Playwright fork) | Walmart search, product pages, order history, adding to cart. Persistent signed-in profile. Runs on a virtual screen (Xvfb) viewable through noVNC on port 6080 so a person can sign in or solve a robot check |
| Home Assistant | Phone notifications to every household phone (actionable: Approve, Retry, Checked out), auto-cleared on every phone once anyone resolves them; a voice intent ("we're out of / low on X") that fires an event the app listens to over HA's websocket |
| FastAPI + APScheduler + SQLite | The app itself, port 8790, LAN only, no login |

## Choosing dinners (first screen)

The Dinners page shows the next two weeks of Mealie's dinner plan as photo cards, plus empty days.
- **Tick or untick** each dinner to include it in the next order. By default, every dinner not yet ordered is ticked from the first one through the next Saturday (or the one after, if that's under 3 days away).
- **Ordered dinners** show greyed out with the date. A dinner is marked ordered when its order's checkout is confirmed. Unordered dinners stay available, so skipped ones carry over and you can order any day of the week.
- **Swap** re-rolls one dinner; **Choose** puts a specific recipe on a day; **Random** and **Plan empty days** fill gaps. All of these write to Mealie's meal plan, so Mealie stays the source of truth.
- **The picker** chooses from recipes tagged "Dinner rotation" in Mealie (★ in the chooser adds or removes the tag). Until anything is tagged, it uses dinners planned in the last 120 days. It's weighted: the longer since you last had a recipe (capped at 60 days, never had counts as 60), the likelier it is. It doesn't repeat a recipe already planned nearby.
- **Saturday 3:05 PM** the app plans any empty days for the next week, then builds the grocery list at 5 PM from the ticked dinners.

## How a list is built

1. **Meals**: fetch the plan for the shopping week. Servings default to 2. A recipe whose yield looks wrong (over 8, or a range) prompts a "how many does this really make?" question, remembered per recipe.
2. **Parse ingredient lines**: quantity, unit and food name. Handles unicode fractions ("1 ½"), accents (NFKD), "boneless, skinless" style prefixes, "each" lists, and "salt and pepper" split into two items. Results are cached per line.
3. **Name each ingredient**: the AI maps cleaned text to a generic grocery item plus category, and is given the existing names so it reuses them. Guards:
   - It can't shorten to a vaguer word ("bbq sauce" must not become "sauce").
   - Spelling variants collapse to one key (hyphens to spaces, & to "and", plurals, barbecue to bbq).
   - Aliases remember merged names.
4. **Amounts to packages**: units are converted within volume, weight and count, using densities for common items (garlic clove 5 g against a 50 g bulb, chopped herbs, and so on). Package size is read from the product title. Anything that can't be converted counts as one package.
5. **Stock decision**:
   - Each ingredient has a stock estimate in packages, with levels Out, Low, Medium and High.
   - Recipes draw it down. Shelf-stable items are restocked when the estimate would end at or below Low.
   - Staples marked "always have" aren't bought unless reported low.
   - Unknown stock of a pantry category produces a question rather than a guess.
6. **Pick a product**, in priority order:
   1. A product a person chose by hand.
   2. Something from the household's own Walmart purchase history. History is read from My Items and Orders, with paging and with suggestion carousels filtered out. The AI judges whether a past purchase really is that item, using the recipe lines as context.
   3. A Walmart search, where the AI chooses among the results given the need, the household's usual brands, and anything they removed before.

   Picks are saved and reused for list building. Only verified availability at the configured store and pickup/delivery method counts. Shipping-only products, other stores, marketplace sellers, and unknown availability are excluded. Search never promotes unverified results to available.

## Filling the cart (durable and retry-safe)

- Each trip has a stable ID across draft rebuilds. Reviews are versioned snapshots of selected meals, recipe contents, servings, yields, and store. Stale inputs block approval and new cart fills, including forced fills. A second trip can use the same dates.
- The plan and target quantity per product are saved before anything is added. Original cart quantities and additions verified by read-back are recorded for safe undo.
- New fills reuse actual availability observations less than one hour old for the exact product, configured store, and pickup/delivery method, within the same UTC date. The observation timestamp survives saved-pick edits and cached searches without being refreshed. Legacy, expired, unknown, and mismatched observations require a live check, including replacements. Each product is visited at most once per fill attempt. Retries always check missing products live before adding. The cache window can be shortened or disabled with CART_AVAILABILITY_MINUTES (0 to 60; default 60).
- The cart is read first, and only the missing quantities are added, using Walmart's multi-item link `affil.walmart.com/cart/addToCart?items=ID|QTY,...`.
- The cart is read again afterwards to confirm. Undo removes a whole line only if its baseline was zero and its current quantity equals confirmed Grocer additions. Shared or changed lines are left for the person to adjust.
- If the cart can't be read on the first fill, the fill goes ahead and is marked unverified. A retry that can't read the cart refuses to add anything, so nothing is doubled.
- A product that has gone away is swapped for a similar one, with the package count recalculated; size changes that can't be converted are left out and reported.
- Clicks bring their target into view and pause before and after interaction. History scrolling uses small steps instead of 5,000-pixel jumps. History pagination and post-challenge reloads respect the normal navigation throttle. Challenge pages are left for the person instead of being scrolled automatically.
- Robot checks pause the job, notify everyone with an "Open browser" link, and continue when someone solves it. If nobody does, the list finishes without the remaining searches.

## Pages (phone-first, all refresh themselves)

- **Review**: this week's list with thumbnails (cached locally, tap to enlarge), questions, swaps, quantities, servings, recipe names linked to Mealie, Approve, Fill cart now, and Confirm checkout.
- **Pantry**:
  - Stock is stored in physical base units where possible, so changing package sizes does not reinterpret a half-full package. The package estimate remains available for compatibility and display.
  - Purchases, planned reservations, and consumption are separate. A ledger records purchases and cooking. Cooking and skipping are idempotent, and all checkout writes share a transaction.
  - Every ingredient with its stock level, linked product and image, and the recipes that use it.
  - Search Walmart to relink, use a past purchase, delete an ingredient, merge an ingredient into another, or find duplicates.
- **Check an ingredient**: explains why a product was or wasn't picked. It shows the matching past purchases with the AI's verdict and reason, and near misses.
- **Clean up Mealie**:
  - The AI proposes merges of duplicate foods in Mealie itself. Spelling variants are grouped directly. For the rest, the AI proposes groups, then checks every pair on its own with a reason; anything that is a kind or variety of another (cheddar vs cheese) is rejected.
  - A person ticks what to apply. Applying backs up Mealie first, merges, keeps the old names as aliases, and updates the app's own list. Foods sitting on a Mealie shopping list are reported, not forced.
- **Progress bar** (every page): long jobs run in the background with a live step, count and time estimate, and a "your list is ready" notification when a hands-on build took a while; an orange state when Walmart needs a human, and a background-activity line for quiet work like image lookups (spaced 20 s apart to avoid robot checks).

## Rules that matter

- Never place an order or pay.
- Never count a cart addition as a purchase; only a confirmed checkout credits stock.
- Walmart pacing is learned: at least the learned gap between page loads (randomized up to 1.8x), a break every 8 pages, an hourly cap derived from the gap, and a little scrolling on each page. A robot check multiplies the gap by 1.5 (max 60 s), except checks in the first 2 pages after a warm-up; every 15 clean page loads multiplies it by 0.85 (min 5 s). After 20 minutes idle it warms up from the home page, and searches are typed into the search box (URL search as a fallback). The learned gap is saved and survives restarts. Searches are cached 12 hours and saved picks checked within 7 days are reused while building the list. Cart fills reuse recent verified availability or check live, with the same pacing.
- Browser work happens on one worker thread, because Playwright's sync API is bound to one thread; quick edits never wait on it.
- A wrong merge is worse than a missed one: duplicate detection must be conservative and reviewable. Local scans save proposals; selected merges show product and stock survivors, snapshot the affected data, and support undo only when no newer ingredient or stock changes would be lost. Neither startup nor scheduled builds silently merge ingredients.
- Keep everything local. Ports 8790 and 6080 are for the LAN only; the browser view has no password by the household's choice.
- Startup must survive plain container restarts by clearing the virtual screen's lock file and Chrome's profile lock.

## Configuration (environment)

| Setting | Meaning |
| --- | --- |
| `MEALIE_URL`, `MEALIE_TOKEN`, `MEALIE_PUBLIC_URL` | Mealie API address and token; address used for recipe links |
| `HA_URL`, `HA_TOKEN`, `HA_NOTIFY_TARGETS` | Home Assistant address and token; phones to notify |
| `OLLAMA_URL`, `OLLAMA_MODEL` | Model server and model |
| `PUBLIC_URL`, `VNC_URL` | Links used in notifications |
| `REVIEW_CRON`, `CART_CRON`, `TZ` | Schedule |
| `AUTO_CART_IF_UNREVIEWED` | Fill on Sunday without approval |
| `WALMART_STORE_ID`, `WALMART_FULFILLMENT` | Optional; enables store-specific stock checks (PICKUP or DELIVERY) |
| `DATA_DIR` | SQLite database, browser profile and image cache |

## Not built yet (ideas on the table)

- Offer date changes for skipped dinners directly in Grocer; currently release their reserved ingredients and reschedule in Mealie.
- Freshness and expiry tracking, and an away mode.
- Remembering rejected picks across rebuilds.
- A Stop button for running jobs.

## Trip, pantry, and task storage (0.2)

`trips` owns a stable ID, current review, and state. `reviews.trip_id` ties every
snapshot to its trip. The legacy `cart_runs.week_start` column holds the trip ID
for new runs; legacy records are resolved through their review ID. No table is
recreated or dropped during upgrade. A pre-upgrade SQLite backup is created for
older schemas.

`stock.quantity` and `dimension` preserve physical amounts; `packages` is maintained
as a compatible estimate. `reservations` stores per-meal ingredient amounts,
`meal_progress` tracks planned/cooked/skipped dinners, and `inventory_events`
records stock movement. Selected meals exclude their own reservations when
calculating availability, so an unfinished dinner does not buy its already
purchased ingredients a second time.

`job_records` records accepted, running, done, failed, queued, interrupted,
resumed, and dismissed tasks. Restart marks in-flight records interrupted rather
than replaying browser actions. Supported tasks can be explicitly resumed; cart
retry uses the saved plan and current quantities. A busy response is explicit.

Setup checks are requested by the person, not run on every poll. They check
Mealie credentials, Ollama's configured model, optional Home Assistant access,
chosen store, and the last known Walmart session. Docker health is deliberately
a lightweight liveness check. Version and build revision are shown in Settings.

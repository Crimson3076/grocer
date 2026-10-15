# Code review and recommendations

## Implemented fixes

| Finding | Result |
| --- | --- |
| General online stock was treated as local stock | Require explicit matching store and pickup/local delivery availability; reject unknown stock and shipping-only listings |
| History and manual alternatives could bypass stock checks | Revalidate products when selected and check every planned addition before cart filling |
| A cart operation was counted as a purchase | Move inventory credits and estimated consumption to an idempotent checkout confirmation transaction |
| Retry/force/rebuild could duplicate an order | Persist the week's plan and expected quantities before cart mutations; retries add only deficits |
| Cart verification collected all product IDs in page HTML | Parse quantities only from recognized cart data, excluding recommendations; conflicting or unknown snapshots stop the job |
| Substitute package sizes reused old quantities | Convert package sizes, recalculate count and stock credit, and omit substitutions with unconvertible sizes |
| Manual quantities disappeared after stock/preference edits | Preserve explicit quantities across recomputation and rebuilding |
| A staple could be marked for purchase without a product | Look up a local product when it is reported low/out, including an existing staple in the review |
| Approval could reopen a carted order | Reject editing/approving active completed cart plans and invalidate approval when editable review data changes |
| Invalid servings and negative alternative indices were accepted | Validate finite positive serving counts and bounded integer quantities/indices |
| Concurrent review edits could overwrite one another | Serialize review mutations and reconcile stock/quantity/restock changes during final list persistence |
| Mealie list-shaped API responses could crash | Handle both list and paginated object meal-plan responses |
| Mealie merges continued after backup failure | Stop before merging if the backup request fails |
| Redeploy removed a configured VNC password | Preserve the password; ensure the Docker entrypoint is executable |

## Interface changes

The review has three explicit stages: list review, Walmart cart, and confirmed
checkout. It displays the store/method, approval policy, configured order
threshold, missing prices, saved cart quantities, substitutions, and omitted
items. Recipe yields support custom values. Manual quantities and approximate
stock are labeled. The same-product preference is visibly selected when enabled.
The checkout button has a confirmation that describes the inventory update.
Input focus is preserved when a background refresh arrives while typing.

## Validation

The original 63-test suite passed before changes. The final suite passed all 101 tests. The updated suite adds local
inventory, wrong-store/shipping-only rejection, failed/partial cart recovery,
checkout idempotence, numeric validation, store changes, stock estimates,
staple restocking, cart snapshot parsing, and backup failure tests.

Headless browser checks cover empty/pending/carted/checked-out states, phone and
desktop widths, light/dark themes, the pantry/check/cleanup pages, and the
product preference and custom serving-yield actions. No horizontal overflow or
browser JavaScript errors were observed in those checks.

No real Walmart account, Mealie instance, Home Assistant installation, remote
server deployment, or container build was used for validation.

## Recommended next work, in priority order

1. **Capture real Walmart fixtures from a supervised run.** The local inventory,
   selected-store, and cart adapters deliberately accept only recognized page
   shapes. Verify them against the household's signed-in store before enabling
   unattended operation. If Walmart changes its page structure, extend the
   adapters with new regression fixtures rather than accepting general stock.
2. **Confirm actual meal consumption.** Add the nightly HA prompt about whether
   the meal was made, and deduct ingredients only after confirmation. Roll
   skipped dinners forward. This will improve stock estimates more than more AI.
3. **Reconcile purchased quantities.** Let checkout confirmation record per-item
   quantities and omissions, or import a receipt/order. The current confirmation
   assumes the saved grocery plan was purchased; reset/update stock if it differs.
4. **Measure pantry contents independently of the saved product.** Store grams,
   milliliters, or item counts where possible. Approximate package fractions can
   become misleading after changing brands or package sizes. Until then, recheck
   stock after a package-size change.
5. **Support weighted produce deliberately.** Record the actual sales unit,
   purchased weight, and price basis instead of assuming every cart quantity is a
   retail package. Show uncertain quantities and costs before approval.
6. **Add an optional budget limit.** Include a configurable warning or pause when
   a list exceeds the household's budget, especially after substitutions. Show
   taxes/delivery fees separately when known.
7. **Add authenticated remote access if needed.** The current app is LAN/VPN only.
   Authentication and protected browser access should precede any public hosting.
8. **Pin a verified deployment environment.** Test/build the container with the
   actual Chrome/Patchright versions, then pin dependencies and record their
   versions so upstream changes do not silently alter browser automation.

## Important operating assumptions

- Availability means explicit local fulfillment at the chosen Walmart store,
  not a promise that stock will remain available at checkout.
- Saved availability has a 15-minute cache for list edits. Manual selection and
  final cart filling check again.
- Grocer adds to an existing cart and counts existing quantities toward its
  targets. It does not erase the user's other cart items.
- The cart's saved plan is frozen once additions begin. Resetting the plan does
  not remove items from Walmart.
- Checkout still belongs to the user. Grocer never pays, chooses a delivery slot,
  or places an order.

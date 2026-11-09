# Scenario Description

The target module is an **E-commerce Cart and Checkout System** (`scenario/src/ecommerce/`). This system handles operations such as adding items to a shopping cart, returning applied promotional discounts and preferences, selecting promotional banners/avatars, processing asynchronous inventory deductions during checkout, and querying stock states.

The scenario test suite (`scenario/tests/test_cart.py`) contains **22 tests in total**:
- **20 intentionally flaky tests** (4 tests for each of the 5 flakiness categories).
- **2 deterministic control tests** (guaranteed to pass 100% of the time under all conditions).

---

# Cases and Ground-Truth Classification

## Category 1: Hash Order Dependency (`PYTHONHASHSEED`) -> Test Side (4 tests)

**Context:** Several `Cart` methods return unordered Python `set` collections of string tags/codes (`apply_discounts()`, `get_user_preferences()`, `get_cart_metadata()`, `get_applied_taxes()`).
**Flaky Behavior:** The tests convert the returned `set` directly into a comma-separated string via `",".join(...)` and compare it against a hardcoded string ordering.
**Why it fails:** Python randomizes string hashes across interpreter invocations using `PYTHONHASHSEED`. The iteration order of a `set` of strings changes across different `PYTHONHASHSEED` values, causing the joined string to flip between `"A,B"` and `"B,A"`. Within a fixed `PYTHONHASHSEED`, the order is completely deterministic.

| Test Name | Method Tested | Fault Side | Expected Category |
| :--- | :--- | :--- | :--- |
| `test_applied_discounts_format` | `Cart.apply_discounts()` | Test Side | `hash_order` |
| `test_preferences_format` | `Cart.get_user_preferences()` | Test Side | `hash_order` |
| `test_metadata_format` | `Cart.get_cart_metadata()` | Test Side | `hash_order` |
| `test_taxes_format` | `Cart.get_applied_taxes()` | Test Side | `hash_order` |

---

## Category 2: Should Be Deterministic But Is Random (Repeat Runs) -> Code Side (4 tests)

**Context:** Several `Cart` helper methods (`assign_default_avatar()`, `select_promo_banner()`, `assign_support_agent()`, `get_daily_quote()`) select an item using `random.choice(...)` without accepting a deterministic seed or injectable RNG.
**Flaky Behavior:** Each test calls one of these methods and asserts that the returned value equals one specific option from the candidate list.
**Why it fails:** Because `random.choice` draws pseudo-randomly on every call without seeding or mocking, repeated executions flip between passing (25%–33% probability) and failing (67%–75% probability) even when `PYTHONHASHSEED` and test order are fixed.

| Test Name | Method Tested | Fault Side | Expected Category |
| :--- | :--- | :--- | :--- |
| `test_default_avatar_assignment` | `Cart.assign_default_avatar()` | Code Side | `randomness` |
| `test_select_promo_banner` | `Cart.select_promo_banner()` | Code Side | `randomness` |
| `test_assign_support_agent` | `Cart.assign_support_agent()` | Code Side | `randomness` |
| `test_get_daily_quote` | `Cart.get_daily_quote()` | Code Side | `randomness` |

---

## Category 3: Race Condition (Repeat Runs / Concurrency) -> Code Side (4 tests)

**Context:** When `Cart.checkout()` is called, it spawns a background `threading.Thread` to deduct the cart's items from `INVENTORY_DB` and returns `{"status": "processing"}` without calling `worker.join()`.
**Flaky Behavior:** The tests call `cart.checkout()` and immediately query `get_stock(...)` to assert that the inventory count has already decreased.
**Why it fails:** Both the background worker thread and the main thread execute symmetric micro-sleeps (`time.sleep(0.0002)`) after synchronizing on thread start. No random number generator is used; whether the worker thread updates `INVENTORY_DB` before the main thread reads `get_stock(...)` depends purely on OS thread scheduling jitter. Across repeated runs, each test flips between passing and failing.

| Test Name | Method Tested | Fault Side | Expected Category |
| :--- | :--- | :--- | :--- |
| `test_checkout_deducts_inventory` | `Cart.checkout()` (`laptop`) | Code Side | `race_condition` |
| `test_checkout_deducts_mouse` | `Cart.checkout()` (`mouse`) | Code Side | `race_condition` |
| `test_checkout_deducts_keyboard` | `Cart.checkout()` (`keyboard`) | Code Side | `race_condition` |
| `test_checkout_deducts_both` | `Cart.checkout()` (`monitor`) | Code Side | `race_condition` |

---

## Category 4: Shared State Between Tests (Order / Isolation) -> Test Side (4 tests)

**Context:** The test module defines a module-level `shared_test_cart = Cart("shared_user")` instance without a fixture to reset `shared_test_cart.items` between tests.
**Flaky Behavior:** The 4 tests form two independent conflicting pairs on `shared_test_cart.items`:
- **Pair 1 (`"laptop"` key):**
  - `test_new_cart_is_empty`: checks that `"laptop"` is not yet in `shared_test_cart.items`, then adds `"laptop"` (qty 1).
  - `test_shared_cart_has_no_prior_laptop`: adds `"laptop"` (qty 1) to `shared_test_cart` and asserts `shared_test_cart.items["laptop"] == 1`.
- **Pair 2 (`"monitor"` key):**
  - `test_single_item_in_shared_cart`: checks that `"monitor"` is not yet in `shared_test_cart.items`, then adds `"monitor"` (qty 1).
  - `test_shared_cart_only_contains_added_monitor`: adds `"monitor"` (qty 1) to `shared_test_cart` and asserts `shared_test_cart.items["monitor"] == 1`.

**Why it fails:**
- **Run alone in isolation (`pytest scenario/tests/test_cart.py::<test_name>`):** Every single one of the 4 tests **passes 100% of the time**, because `shared_test_cart` starts empty in a fresh process. None of these tests is a permanently broken bug.
- **Run in a fixed order:** Outcome is 100% deterministic across repeated runs and hash seeds (in default top-to-bottom file order, the first test in each pair passes and the second fails because the key was already incremented to 2).
- **Run in shuffled order:** In each pair, whichever test runs first passes and whichever runs second fails (50% pass / 50% fail rate across random test orderings).

| Test Name | Shared Resource Key | Fault Side | Expected Category |
| :--- | :--- | :--- | :--- |
| `test_new_cart_is_empty` | `shared_test_cart.items["laptop"]` | Test Side | `shared_state` |
| `test_shared_cart_has_no_prior_laptop` | `shared_test_cart.items["laptop"]` | Test Side | `shared_state` |
| `test_single_item_in_shared_cart` | `shared_test_cart.items["monitor"]` | Test Side | `shared_state` |
| `test_shared_cart_only_contains_added_monitor` | `shared_test_cart.items["monitor"]` | Test Side | `shared_state` |

---

## Category 5: Timezone / Local-Time Sensitivity (`TZ`) -> Code Side & Test Side (4 tests)

**Context:** Production methods and tests format dates or timestamps across timezone boundaries (`generate_daily_order_prefix()`, `is_flash_coupon_active()`, `format_receipt_timestamp()`, `format_audit_date_tag()`).
**Flaky Behavior:**
- **Code Side (2 tests):** `Cart.generate_daily_order_prefix()` calls naive `datetime.now()` and `Cart.is_flash_coupon_active()` calls local `date.today()`, while the tests compare against explicit UTC dates (`datetime.now(timezone.utc)`).
- **Test Side (2 tests):** `Cart.format_receipt_timestamp()` and `Cart.format_audit_date_tag()` format epoch timestamps in explicit UTC (`tz=timezone.utc`), while the tests compute expected strings using local-time `datetime.fromtimestamp(epoch)` (without `tz`) or `time.localtime(epoch)`.

**Why it fails:** Under a fixed timezone (`TZ=UTC`), repeat, hashseed, order, and isolated runs are 100% consistent. When `TZ` is perturbed across extreme offsets (`UTC`, `Pacific/Kiritimati` at UTC+14, and `America/Pago_Pago` at UTC-11, spanning a 25-hour window), local calendar dates and wall-clock hours diverge from UTC at every instant of the day.

| Test Name | Method Tested | Fault Side | Expected Category |
| :--- | :--- | :--- | :--- |
| `test_daily_order_prefix_uses_utc_date` | `Cart.generate_daily_order_prefix()` | Code Side | `timezone` |
| `test_coupon_expiry_date_check` | `Cart.is_flash_coupon_active()` | Code Side | `timezone` |
| `test_receipt_timestamp_formatting` | `Cart.format_receipt_timestamp()` | Test Side | `timezone` |
| `test_audit_log_date_tag` | `Cart.format_audit_date_tag()` | Test Side | `timezone` |

---

## Control Group: Deterministic Tests (2 tests)

**Context:** Isolated, synchronous operations on a local `Cart` instance and a dedicated inventory key (`"tablet"`).
**Behavior:** These tests do not rely on set iteration order, `random`, background threads, or un-reset shared state. They pass 100% of the time under all seeds, repeat counts, and test orderings.

| Test Name | Purpose | Expected Category |
| :--- | :--- | :--- |
| `test_add_item_to_cart_is_deterministic` | Negative control (local `Cart` state) | `deterministic` (Not Flaky) |
| `test_inventory_deduct_is_deterministic` | Negative control (synchronous `reset_stock` + `deduct_inventory`) | `deterministic` (Not Flaky) |

---

# Expected Findings & Recommended Fixes

- **Category 1 (Hash Order -> Test Side):** Tests should not compare `",".join(set_result)` against a hardcoded string. Fix the tests by comparing sets directly (`assert cart.apply_discounts() == {"WELCOME_10", "SEASONAL_20"}`) or sorting before joining (`",".join(sorted(...))`).
- **Category 2 (Randomness -> Code Side):** Production methods that use `random.choice` should accept an optional seed/RNG parameter or deterministic selection strategy, or tests should mock `random.choice`.
- **Category 3 (Race Condition -> Code Side):** `Cart.checkout()` spawns an unjoined background thread, or tests assert immediately without synchronization. Fix by joining/awaiting completion or providing a completion event/polling mechanism.
- **Category 4 (Shared State -> Test Side):** Tests mutate the module-level `shared_test_cart` without resetting it. Fix by using a fresh `Cart` fixture per test or adding an `autouse` fixture that clears `shared_test_cart.items.clear()` before each test.
- **Category 5 (Timezone Sensitivity -> Code Side & Test Side):** Production methods and tests mix naive local time (`datetime.now()`, `date.today()`, `datetime.fromtimestamp(ts)`, `time.localtime(ts)`) with UTC expectations. Fix by passing explicit timezone objects (`tz=timezone.utc` / `time.gmtime`) or freezing time in tests.

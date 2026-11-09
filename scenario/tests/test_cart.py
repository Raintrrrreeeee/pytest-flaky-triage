"""Flaky test suite for the e-commerce cart scenario."""

from datetime import datetime, timezone
import time

from ecommerce.cart import Cart
from ecommerce.inventory import deduct_inventory, get_stock, reset_stock

# Shared module-level state without fixture reset (simulates Category 4)
shared_test_cart = Cart("shared_user")


# =====================================================================
# Category 1: Hash Order Dependency (Test Side) — 4 tests
# =====================================================================

def test_applied_discounts_format():
    """Flaky due to set iteration order depending on PYTHONHASHSEED."""
    cart = Cart("user_1")
    discounts_str = ",".join(cart.apply_discounts())
    assert discounts_str == "WELCOME_10,SEASONAL_20"


def test_preferences_format():
    """Flaky due to set iteration order depending on PYTHONHASHSEED."""
    cart = Cart("user_pref")
    prefs_str = ",".join(cart.get_user_preferences())
    assert prefs_str == "dark_mode,email_alerts"


def test_metadata_format():
    """Flaky due to set iteration order depending on PYTHONHASHSEED."""
    cart = Cart("user_meta")
    meta_str = ",".join(cart.get_cart_metadata())
    assert meta_str == "web_session,mobile_app"


def test_taxes_format():
    """Flaky due to set iteration order depending on PYTHONHASHSEED."""
    cart = Cart("user_tax")
    taxes_str = ",".join(cart.get_applied_taxes())
    assert taxes_str == "GST,VAT"


# =====================================================================
# Category 2: Should Be Deterministic But Is Random (Code Side) — 4 tests
# =====================================================================

def test_default_avatar_assignment():
    """Flaky due to unseeded random.choice in production code."""
    cart = Cart("user_2")
    assert cart.assign_default_avatar() == "default_1.png"


def test_select_promo_banner():
    """Flaky due to unseeded random.choice in production code."""
    cart = Cart("user_promo")
    assert cart.select_promo_banner() == "banner_sale.jpg"


def test_assign_support_agent():
    """Flaky due to unseeded random.choice in production code."""
    cart = Cart("user_agent")
    assert cart.assign_support_agent() == "Alice"


def test_get_daily_quote():
    """Flaky due to unseeded random.choice in production code."""
    cart = Cart("user_quote")
    assert cart.get_daily_quote() == "Buy more!"


# =====================================================================
# Category 3: Race Condition (Code Side) — 4 tests
# =====================================================================

def test_checkout_deducts_inventory():
    """Flaky due to background thread racing with immediate stock check."""
    reset_stock("laptop", 50)
    cart = Cart("user_3")
    cart.add_item("laptop", 1)
    cart.checkout()
    assert get_stock("laptop") == 49


def test_checkout_deducts_mouse():
    """Flaky due to background thread racing with immediate stock check."""
    reset_stock("mouse", 100)
    cart = Cart("user_mouse")
    cart.add_item("mouse", 1)
    cart.checkout()
    assert get_stock("mouse") == 99


def test_checkout_deducts_keyboard():
    """Flaky due to background thread racing with immediate stock check."""
    reset_stock("keyboard", 80)
    cart = Cart("user_keyboard")
    cart.add_item("keyboard", 1)
    cart.checkout()
    assert get_stock("keyboard") == 79


def test_checkout_deducts_both():
    """Flaky due to background thread racing with immediate stock check."""
    reset_stock("monitor", 40)
    cart = Cart("user_monitor")
    cart.add_item("monitor", 1)
    cart.checkout()
    assert get_stock("monitor") == 39


# =====================================================================
# Category 4: Shared State Between Tests (Test Side) — 4 tests
# =====================================================================

def test_new_cart_is_empty():
    """Passes alone; fails if test_shared_cart_has_no_prior_laptop ran first."""
    had_no_laptop = "laptop" not in shared_test_cart.items
    shared_test_cart.add_item("laptop", 1)
    assert had_no_laptop


def test_shared_cart_has_no_prior_laptop():
    """Passes alone; fails if test_new_cart_is_empty ran first."""
    shared_test_cart.add_item("laptop", 1)
    assert shared_test_cart.items["laptop"] == 1


def test_single_item_in_shared_cart():
    """Passes alone; fails if test_shared_cart_only_contains_added_monitor ran first."""
    had_no_monitor = "monitor" not in shared_test_cart.items
    shared_test_cart.add_item("monitor", 1)
    assert had_no_monitor


def test_shared_cart_only_contains_added_monitor():
    """Passes alone; fails if test_single_item_in_shared_cart ran first."""
    shared_test_cart.add_item("monitor", 1)
    assert shared_test_cart.items["monitor"] == 1


# =====================================================================
# Category 5: Timezone / Local-Time Sensitivity (2 Code Side, 2 Test Side)
# =====================================================================

def test_daily_order_prefix_uses_utc_date():
    """Flaky across timezones due to naive datetime.now() in production code."""
    cart = Cart("user_tz_prefix")
    expected_prefix = datetime.now(timezone.utc).strftime("ORD-%Y%m%d")
    assert cart.generate_daily_order_prefix() == expected_prefix


def test_coupon_expiry_date_check():
    """Flaky across timezones due to local date.today() in production code."""
    cart = Cart("user_tz_coupon")
    utc_today = datetime.now(timezone.utc).date()
    assert cart.is_flash_coupon_active(utc_today) is True


def test_receipt_timestamp_formatting():
    """Flaky across timezones due to naive datetime.fromtimestamp() in test."""
    cart = Cart("user_tz_receipt")
    epoch = 1704067200
    expected = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    assert cart.format_receipt_timestamp(epoch) == expected


def test_audit_log_date_tag():
    """Flaky across timezones due to local time.localtime() in test."""
    cart = Cart("user_tz_audit")
    epoch = 1704150000
    expected = time.strftime("%Y-%m-%d", time.localtime(epoch))
    assert cart.format_audit_date_tag(epoch) == expected


# =====================================================================
# Control Group: Deterministic Tests — 2 tests
# =====================================================================

def test_add_item_to_cart_is_deterministic():
    """Control test: purely local, synchronous, and isolated."""
    cart = Cart("test_user_stable")
    cart.add_item("mouse", 2)
    assert cart.user_id == "test_user_stable"
    assert cart.items["mouse"] == 2
    assert cart.get_total_items() == 2


def test_inventory_deduct_is_deterministic():
    """Control test: synchronous inventory update on a dedicated item key."""
    reset_stock("tablet", 10)
    deduct_inventory("tablet", 3)
    assert get_stock("tablet") == 7

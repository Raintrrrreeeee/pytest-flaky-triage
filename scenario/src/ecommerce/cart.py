"""Shopping cart logic exhibiting intentional flakiness patterns."""

from datetime import date, datetime, timezone
import random
import threading
import time
from ecommerce.inventory import deduct_inventory


class Cart:
    """E-commerce shopping cart."""

    def __init__(self, user_id: str = "guest") -> None:
        self.user_id = user_id
        self.items = {}

    def add_item(self, item_name: str, quantity: int = 1) -> None:
        """Add an item and quantity to the cart."""
        self.items[item_name] = self.items.get(item_name, 0) + quantity

    def get_total_items(self) -> int:
        """Return total quantity of items in the cart."""
        return sum(self.items.values())

    # Category 1: Hash order dependency (returns unordered sets of strings)
    def apply_discounts(self) -> set:
        """Return applied discount codes as an unordered set of strings."""
        return set(["WELCOME_10", "SEASONAL_20"])

    def get_user_preferences(self) -> set:
        """Return active user preference flags as an unordered set of strings."""
        return set(["dark_mode", "email_alerts"])

    def get_cart_metadata(self) -> set:
        """Return cart session tags as an unordered set of strings."""
        return set(["web_session", "mobile_app"])

    def get_applied_taxes(self) -> set:
        """Return applicable tax codes as an unordered set of strings."""
        return set(["GST", "VAT"])

    # Category 2: Randomness / Should be deterministic (uses random.choice)
    def assign_default_avatar(self) -> str:
        """Pick a default avatar using an unseeded random choice."""
        return random.choice(["default_1.png", "default_2.png", "default_3.png", "default_4.png"])

    def select_promo_banner(self) -> str:
        """Pick a promotional banner using an unseeded random choice."""
        return random.choice(["banner_sale.jpg", "banner_new_arrival.jpg", "banner_freeshipping.jpg"])

    def assign_support_agent(self) -> str:
        """Assign a support agent using an unseeded random choice."""
        return random.choice(["Alice", "Bob", "Charlie", "Diana"])

    def get_daily_quote(self) -> str:
        """Pick a daily store quote using an unseeded random choice."""
        return random.choice(["Buy more!", "Save today.", "Great deals inside."])

    # Category 3: Race condition (background thread races with caller return)
    def checkout(self) -> dict:
        """Start an asynchronous inventory deduction thread without joining."""
        ready = threading.Event()

        def _deduct_inventory() -> None:
            ready.set()
            for _ in range(5):
                time.sleep(0.0002)
            for product_id, qty in list(self.items.items()):
                deduct_inventory(product_id, qty)

        worker = threading.Thread(target=_deduct_inventory, daemon=True)
        worker.start()
        ready.wait(timeout=0.05)
        for _ in range(5):
            time.sleep(0.0002)

        return {"status": "processing", "message": "Checkout started in background"}

    # Category 5: Timezone / local-time sensitivity (2 Code Side, 2 Test Side helpers)
    def generate_daily_order_prefix(self) -> str:
        """Generate a daily order prefix using naive local datetime.now()."""
        return datetime.now().strftime("ORD-%Y%m%d")

    def is_flash_coupon_active(self, valid_on_utc_date: date) -> bool:
        """Check if a flash coupon is active today using local date.today()."""
        return date.today() == valid_on_utc_date

    def format_receipt_timestamp(self, epoch_seconds: int) -> str:
        """Format an epoch timestamp in UTC for receipts."""
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    def format_audit_date_tag(self, epoch_seconds: int) -> str:
        """Format an epoch timestamp as a UTC date tag for audit logs."""
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d")

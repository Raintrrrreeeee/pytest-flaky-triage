"""Inventory storage and operations for the e-commerce scenario."""

INVENTORY_DB = {
    "laptop": 10,
    "mouse": 10,
    "keyboard": 10,
    "monitor": 10,
    "tablet": 10,
}


def reset_stock(item_name: str, quantity: int = 10) -> None:
    """Reset the stock count for a specific product."""
    INVENTORY_DB[item_name] = quantity


def deduct_inventory(item_name: str, quantity: int = 1) -> None:
    """Deduct stock count for a specific product."""
    if item_name in INVENTORY_DB:
        current = INVENTORY_DB[item_name]
        INVENTORY_DB[item_name] = current - quantity


def get_stock(item_name: str) -> int:
    """Return the current stock count for a specific product."""
    return INVENTORY_DB.get(item_name, 0)

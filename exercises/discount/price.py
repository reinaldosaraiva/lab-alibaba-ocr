"""Small, deterministic MR exercise for the review and autofix loop."""


def discounted_price(price_cents: int, discount_percent: int) -> int:
    """Return the final price in cents after an integer percentage discount."""
    if price_cents < 0 or not 0 <= discount_percent <= 100:
        raise ValueError("price and discount must be within range")
    return price_cents * (100 - discount_percent) // 100

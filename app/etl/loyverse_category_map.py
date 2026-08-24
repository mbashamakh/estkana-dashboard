"""
Maps Loyverse's real 14 menu categories onto the 5 buckets the dashboard's
"Sales by product category" chart displays. Confirmed by the user
2026-08-24 (mapped in a spreadsheet I generated from /api/_diag/loyverse-catalog
and sent back for review).

Order matters here — DISPLAY_CATEGORIES is consumed as DATA.meta.categories
and drives both the pie chart's legend order and CAT_COLORS' index-based
color assignment in the frontend.
"""
from __future__ import annotations

# Display order the user specified (first-appearance order in their
# confirmed mapping sheet).
DISPLAY_CATEGORIES = ["Other", "Shabati", "Bakery & Snacks", "Hot drinks", "Cold Drink"]

# Loyverse category_id -> dashboard display bucket.
CATEGORY_ID_TO_DISPLAY = {
    "c44f581e-0b71-4265-b25f-e1a77277a04f": "Other",             # Ramadan Specials
    "d9ab67c6-47d2-49a5-9200-8f18c2bf7763": "Other",             # Seating / Container (dine-in charge, not food)
    "e525859f-6dfb-427e-9786-13954a02e9bb": "Shabati",           # Premium Shbati
    "18338a2f-152c-42e0-b9e9-d0c672b7989d": "Shabati",           # Sweet Chapati
    "2c19bfa6-4452-4daa-86dd-3d1235f1c23a": "Other",             # Umm Nayef Breakfast
    "575388df-8940-4e90-937c-5b62a9a4449e": "Other",             # Masoub & Areeka
    "eda535cc-1420-41f7-92b2-6460c80c2016": "Bakery & Snacks",   # Appetizers & Add-ons
    # "ثلاجات" ("Fridge") is just what the branch staff named this category in
    # Loyverse — despite the name, it actually holds hot-drink items.
    # Confirmed intentional by the user 2026-08-24, not a mapping mistake.
    "4d129c43-a6a6-43e4-94e4-d09207294d38": "Hot drinks",        # "Fridge" (misnamed — actually hot drinks)
    "3005cd6f-8e74-4ba2-a4ef-6e1adacd6e49": "Other",             # Shawarma
    "51d8d7e8-dcaa-4f7f-9d48-d9902a765a23": "Cold Drink",        # Cold Drinks
    "dd51c222-689f-4e70-a444-6f50a90ce892": "Other",             # Egyptian Pastries & Mutabbaq
    "901985aa-dcbe-4466-902c-e0cda1d17d1c": "Bakery & Snacks",   # Desserts
    "c600087c-cd9e-447a-84dd-93694f2c0ea8": "Hot drinks",        # Hot Drinks
    "2e63f626-24a6-43cd-b142-eff14b13422d": "Shabati",           # Savory Chapati
}


def display_category_for(category_id: str | None) -> str:
    """Items with no category_id at all (seen in the real catalog — some
    items are simply uncategorized) fall back to "Other"."""
    if category_id is None:
        return "Other"
    return CATEGORY_ID_TO_DISPLAY.get(category_id, "Other")

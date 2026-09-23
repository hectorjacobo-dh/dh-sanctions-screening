"""Counterparty groups and operating brands.

Both exist for the same reason: a batch of several thousand names is screened
centrally, but the alerts are worked and answered locally. Without them an
officer opening a case cannot tell whether a hit is a restaurant partner, a
rider or a payroll record, nor which market has to answer for it — and those
are the first two questions asked when an alert is escalated.

Closed lists on purpose. Free text here would splinter into "foodpanda",
"Foodpanda ", "FP" and stop being groupable, which is the only thing these
fields are for.
"""

from __future__ import annotations

# What kind of counterparty was screened. Drives how an alert is triaged: a
# rider match and a corporate supplier match go to different people.
COUNTERPARTY_GROUPS: tuple[str, ...] = (
    "Partner",
    "Supplier",
    "Employee",
    "Rider",
    "Dmart",
    "Adhoc",
)

# Operating brands, alphabetical. "DH Compliance" is the group function itself,
# for screenings that are not on behalf of a single market.
BRANDS: tuple[str, ...] = (
    "DH Compliance",
    "efood",
    "Foodora",
    "Foodpanda",
    "foody",
    "Glovo",
    "HungerStation",
    "PedidosYa",
    "Talabat",
    "Woowa",
    "Yemeksepeti",
)


def normalise_group(value: str | None) -> str:
    """Accept a group case-insensitively; anything else becomes empty.

    Empty is honest — it means "not stated" — whereas silently keeping an
    unrecognised value would create a one-member group nobody can filter on.
    """
    if not value:
        return ""
    wanted = value.strip().casefold()
    for group in COUNTERPARTY_GROUPS:
        if group.casefold() == wanted:
            return group
    return ""


def normalise_brand(value: str | None) -> str:
    if not value:
        return ""
    wanted = value.strip().casefold()
    for brand in BRANDS:
        if brand.casefold() == wanted:
            return brand
    return ""

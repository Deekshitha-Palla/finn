"""Announcement categories (from the challenge's reference doc; adapt freely)."""
from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    RESULTS = "results"
    M_AND_A = "M&A"
    FUND_RAISE = "fund_raise"
    MANAGEMENT_CHANGE = "management_change"
    LITIGATION = "litigation"
    INSIDER_TRADING = "insider_trading"
    EARNINGS_CALL = "earnings_call"
    INVESTOR_MEETINGS = "investor_meetings"
    CREDIT_RATING = "credit_rating"
    DIVIDEND = "dividend"
    BONUS_SPLIT = "bonus_split"
    CAPEX = "capex"
    PRESS_RELEASE = "press_release"
    POSTAL_BALLOT = "postal_ballot"
    AGM_EGM = "agm_egm"
    PLEDGING = "pledging"
    UNCLASSIFIED = "unclassified"


# TODO(feature): rules-first classifier (keywords on subject line), LLM only for the leftovers,
# plus a priority map Category -> {alert, high, normal, collapsed}. That mapping is a product decision.

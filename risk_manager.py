"""
Risk Manager — controls budget, prevents overspending,
ensures all ranges in a strategy get funded.
"""
import logging
from config import COPY_RATIO, MIN_COPY_AMOUNT, MAX_COPY_AMOUNT, SAFETY_BUFFER

logger = logging.getLogger("risk")


def calc_copy_amount(trader_usdc: float) -> float:
    """
    Proportional copy: COPY_RATIO × trader's amount.
    Clamp to [MIN, MAX].
    """
    amount = round(trader_usdc * COPY_RATIO, 2)
    amount = max(MIN_COPY_AMOUNT, min(amount, MAX_COPY_AMOUNT))
    return amount


def can_afford(amount: float) -> tuple[bool, float, float]:
    """
    Check if we can afford this trade.
    Returns (can_afford, available_cash, total_exposure).
    """
    from trading import get_balance
    from database import get_total_open_exposure

    bal = get_balance()
    if bal is None:
        # Can't check — allow trade but warn
        logger.warning("Can't check balance (RPC fail), allowing trade")
        return True, -1, -1

    exposure = get_total_open_exposure()
    available = bal - exposure - SAFETY_BUFFER

    logger.info("Risk check: bal=$%.2f, exposure=$%.2f, buffer=$%.2f, available=$%.2f, need=$%.2f",
                bal, exposure, SAFETY_BUFFER, available, amount)

    return available >= amount, available, exposure


def adjust_amount_to_budget(amount: float, available: float) -> float:
    """
    If not enough for full amount, reduce proportionally.
    Never skip — always enter, even with smaller size.
    """
    if available <= 0:
        return 0

    if amount <= available:
        return amount

    # Reduce to what we can afford, but keep minimum
    adjusted = max(MIN_COPY_AMOUNT, round(available * 0.8, 2))  # 80% of available
    logger.info("Adjusted amount: $%.2f → $%.2f (available=$%.2f)", amount, adjusted, available)
    return adjusted

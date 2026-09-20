"""Invoicing: draft client invoices from time & expense actuals or milestone schedules.

Time & materials and cost-plus engagements bill off reconciled actuals;
fixed-fee engagements bill off the milestone schedule set up in
``engagement_setup``. Every invoice line item traces back to the specific
timesheet/expense records (or milestone) it was drawn from, so
``reconcile_invoice_to_source`` can independently recompute each invoice
total from the underlying data and confirm nothing was dropped or double
counted.

Two things are deliberately *not* billed here, by construction, which is
what gives ``discrepancy_detection.py`` real unbilled-time signal to find:
a timesheet entry missing its task code, or missing manager approval,
cannot be put on an invoice -- it is excluded from every draft invoice
until someone fixes it upstream.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from .engagement_setup import (
    AS_OF_DATE,
    ContractType,
    Engagement,
    ROLE_ORDER,
    STANDARD_BILL_RATES,
    STANDARD_COST_RATES,
)


@dataclass
class InvoiceLineItem:
    """One line on a draft invoice."""

    description: str
    category: str  # "Labor", "Expense", "Fee", "Milestone"
    role: str | None
    hours: float | None
    rate: float | None
    amount: float
    source_ids: list[str] = field(default_factory=list)


@dataclass
class Invoice:
    """A draft client invoice for one engagement, one billing period."""

    invoice_id: str
    engagement_id: str
    client_name: str
    contract_type: str
    period_start: date
    period_end: date
    invoice_date: date
    line_items: list[InvoiceLineItem]
    status: str = "Draft"

    @property
    def total_amount(self) -> float:
        return round(sum(li.amount for li in self.line_items), 2)


def _month_end(month_start: date) -> date:
    if month_start.month == 12:
        return date(month_start.year, 12, 31)
    return date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)


def _billing_backlog_engagement_ids(engagements: list[Engagement], seed: int = 13) -> set[str]:
    """A small, realistic subset of engagements where billing has fallen behind.

    Simulates the kind of ops gap that actually causes unbilled-time aging in
    practice (a biller out on leave, a lapsed client PO, an admin backlog) --
    without it, every eligible month gets invoiced the moment it closes and
    the unbilled-aging detector would never have anything real to find.
    """
    rng = random.Random(seed)
    billable_ids = [
        e.engagement_id for e in engagements if e.contract_type != ContractType.FIXED_FEE
    ]
    n = max(1, round(len(billable_ids) * 0.15))
    return set(rng.sample(billable_ids, k=min(n, len(billable_ids))))


def _billable_months(engagement: Engagement, as_of: date, backlog_ids: set[str]) -> list[date]:
    """Fully-closed prior months only -- the current in-progress month isn't billed yet.

    Engagements in ``backlog_ids`` additionally hold back their most recent
    two otherwise-eligible months, unbilled, to model a stalled billing cycle.
    """
    cutoff_month = date(as_of.year, as_of.month, 1)
    months = [m for m in engagement.month_range(as_of) if m < cutoff_month]
    if engagement.engagement_id in backlog_ids:
        months = months[:-2] if len(months) > 2 else []
    return months


def _eligible_time(timesheet_df: pd.DataFrame, engagement_id: str, month: date) -> pd.DataFrame:
    if timesheet_df.empty:
        return timesheet_df
    mask = (
        (timesheet_df["engagement_id"] == engagement_id)
        & (timesheet_df["month"] == month)
        & (timesheet_df["billable"])
        & (timesheet_df["approved"])
        & (timesheet_df["task_code"].notna())
    )
    return timesheet_df[mask]


def _eligible_expenses(expense_df: pd.DataFrame, engagement_id: str, month: date) -> pd.DataFrame:
    if expense_df.empty:
        return expense_df
    mask = (
        (expense_df["engagement_id"] == engagement_id)
        & (expense_df["month"] == month)
        & (expense_df["billable"])
        & (expense_df["approved"])
    )
    return expense_df[mask]


def _labor_line_items(eligible: pd.DataFrame, rate_card: dict[str, float]) -> list[InvoiceLineItem]:
    items = []
    for role in [r.value for r in ROLE_ORDER]:
        role_rows = eligible[eligible["role"] == role]
        if role_rows.empty:
            continue
        hours = round(role_rows["hours"].sum(), 1)
        rate = rate_card[role]
        items.append(
            InvoiceLineItem(
                description=f"Professional Services -- {role}",
                category="Labor",
                role=role,
                hours=hours,
                rate=rate,
                amount=round(hours * rate, 2),
                source_ids=role_rows["entry_id"].tolist(),
            )
        )
    return items


def _expense_line_items(eligible_expenses: pd.DataFrame) -> list[InvoiceLineItem]:
    items = []
    if eligible_expenses.empty:
        return items
    for category, rows in eligible_expenses.groupby("category"):
        items.append(
            InvoiceLineItem(
                description=f"Reimbursable Expenses -- {category}",
                category="Expense",
                role=None,
                hours=None,
                rate=None,
                amount=round(rows["amount"].sum(), 2),
                source_ids=rows["expense_id"].tolist(),
            )
        )
    return items


def generate_tm_and_cost_plus_invoices(
    engagements: list[Engagement],
    timesheet_df: pd.DataFrame,
    expense_df: pd.DataFrame,
    as_of: date = AS_OF_DATE,
) -> list[Invoice]:
    """One invoice per T&M/cost-plus engagement per fully-closed billing month."""
    invoices: list[Invoice] = []
    counter = 0
    backlog_ids = _billing_backlog_engagement_ids(engagements)
    for engagement in engagements:
        if engagement.contract_type == ContractType.FIXED_FEE:
            continue
        rate_card = (
            STANDARD_BILL_RATES if engagement.contract_type == ContractType.TIME_AND_MATERIALS else STANDARD_COST_RATES
        )
        rate_card = {role.value: rate for role, rate in rate_card.items()}

        for month in _billable_months(engagement, as_of, backlog_ids):
            eligible_time = _eligible_time(timesheet_df, engagement.engagement_id, month)
            eligible_exp = _eligible_expenses(expense_df, engagement.engagement_id, month)
            if eligible_time.empty and eligible_exp.empty:
                continue

            line_items = _labor_line_items(eligible_time, rate_card)
            line_items += _expense_line_items(eligible_exp)

            if engagement.contract_type == ContractType.COST_PLUS and line_items:
                labor_cost = sum(li.amount for li in line_items if li.category == "Labor")
                fee_pct = engagement.cost_plus_fee_pct or 0.0
                line_items.append(
                    InvoiceLineItem(
                        description=f"Fixed Fee ({fee_pct:.1%} of labor cost)",
                        category="Fee",
                        role=None,
                        hours=None,
                        rate=None,
                        amount=round(labor_cost * fee_pct, 2),
                    )
                )

            if not line_items:
                continue

            counter += 1
            invoices.append(
                Invoice(
                    invoice_id=f"INV-{engagement.engagement_id}-{month.strftime('%Y%m')}",
                    engagement_id=engagement.engagement_id,
                    client_name=engagement.client_name,
                    contract_type=engagement.contract_type.value,
                    period_start=month,
                    period_end=_month_end(month),
                    invoice_date=_month_end(month) + timedelta(days=5),
                    line_items=line_items,
                )
            )
    return invoices


def generate_fixed_fee_invoices(engagements: list[Engagement], as_of: date = AS_OF_DATE) -> list[Invoice]:
    """One invoice per fixed-fee milestone reached (and not yet invoiced) as of ``as_of``.

    Updates each engagement's milestone ``status`` in place to reflect
    Not Started / In Progress / Complete / Invoiced as of ``as_of``.
    """
    invoices: list[Invoice] = []
    for engagement in engagements:
        if engagement.contract_type != ContractType.FIXED_FEE:
            continue
        for milestone in engagement.milestones:
            if milestone.planned_date <= as_of:
                milestone.status = "Complete"
            elif engagement.start_date <= as_of:
                milestone.status = "In Progress"
            else:
                milestone.status = "Not Started"

            if milestone.status != "Complete":
                continue

            amount = round((engagement.fixed_fee_amount or 0.0) * milestone.pct_of_fee / 100.0, 2)
            invoices.append(
                Invoice(
                    invoice_id=f"INV-{engagement.engagement_id}-{milestone.name[:4].upper().replace(' ', '')}",
                    engagement_id=engagement.engagement_id,
                    client_name=engagement.client_name,
                    contract_type=engagement.contract_type.value,
                    period_start=engagement.start_date,
                    period_end=milestone.planned_date,
                    invoice_date=milestone.planned_date + timedelta(days=5),
                    line_items=[
                        InvoiceLineItem(
                            description=f"Milestone -- {milestone.name} ({milestone.pct_of_fee:.1f}% of fixed fee)",
                            category="Milestone",
                            role=None,
                            hours=None,
                            rate=None,
                            amount=amount,
                        )
                    ],
                )
            )
            milestone.status = "Invoiced"
    return invoices


def generate_invoices(
    engagements: list[Engagement],
    timesheet_df: pd.DataFrame,
    expense_df: pd.DataFrame,
    as_of: date = AS_OF_DATE,
) -> list[Invoice]:
    """Generate the full draft invoice set across the portfolio."""
    invoices = generate_tm_and_cost_plus_invoices(engagements, timesheet_df, expense_df, as_of)
    invoices += generate_fixed_fee_invoices(engagements, as_of)
    return sorted(invoices, key=lambda inv: (inv.engagement_id, inv.period_start))


def reconcile_invoice_to_source(invoice: Invoice, timesheet_df: pd.DataFrame, expense_df: pd.DataFrame) -> dict:
    """Recompute an invoice's labor/expense totals directly from source records.

    Returns the invoice total, the independently-recomputed total, and the
    difference -- should be exactly 0.00 for every invoice; a nonzero
    difference would mean a line item's dollar amount doesn't match what the
    underlying timesheet/expense rows actually support.
    """
    labor_ids = [sid for li in invoice.line_items if li.category == "Labor" for sid in li.source_ids]
    expense_ids = [sid for li in invoice.line_items if li.category == "Expense" for sid in li.source_ids]

    recomputed_labor = 0.0
    if labor_ids and not timesheet_df.empty:
        rows = timesheet_df[timesheet_df["entry_id"].isin(labor_ids)]
        rate_lookup = {role.value: rate for role, rate in STANDARD_BILL_RATES.items()}
        if invoice.contract_type == "Cost-Plus-Fixed-Fee":
            rate_lookup = {role.value: rate for role, rate in STANDARD_COST_RATES.items()}
        recomputed_labor = round((rows["hours"] * rows["role"].map(rate_lookup)).sum(), 2)

    recomputed_expenses = 0.0
    if expense_ids and not expense_df.empty:
        rows = expense_df[expense_df["expense_id"].isin(expense_ids)]
        recomputed_expenses = round(rows["amount"].sum(), 2)

    other_amount = round(
        sum(li.amount for li in invoice.line_items if li.category in ("Fee", "Milestone")), 2
    )
    recomputed_total = round(recomputed_labor + recomputed_expenses + other_amount, 2)

    return {
        "invoice_id": invoice.invoice_id,
        "invoice_total": invoice.total_amount,
        "recomputed_total": recomputed_total,
        "difference": round(invoice.total_amount - recomputed_total, 2),
        "ties_out": abs(invoice.total_amount - recomputed_total) < 0.01,
    }


def invoices_to_dataframe(invoices: list[Invoice]) -> pd.DataFrame:
    rows = [
        {
            "invoice_id": inv.invoice_id,
            "engagement_id": inv.engagement_id,
            "client_name": inv.client_name,
            "contract_type": inv.contract_type,
            "period_start": inv.period_start,
            "period_end": inv.period_end,
            "invoice_date": inv.invoice_date,
            "status": inv.status,
            "total_amount": inv.total_amount,
            "n_line_items": len(inv.line_items),
        }
        for inv in invoices
    ]
    return pd.DataFrame(rows)


def line_items_to_dataframe(invoices: list[Invoice]) -> pd.DataFrame:
    rows = []
    for inv in invoices:
        for li in inv.line_items:
            rows.append(
                {
                    "invoice_id": inv.invoice_id,
                    "engagement_id": inv.engagement_id,
                    "client_name": inv.client_name,
                    "invoice_date": inv.invoice_date,
                    "description": li.description,
                    "category": li.category,
                    "role": li.role,
                    "hours": li.hours,
                    "rate": li.rate,
                    "amount": li.amount,
                }
            )
    return pd.DataFrame(rows)

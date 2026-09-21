"""Budget-to-actual tracking: hours/dollars by engagement, role, and month.

Computes the two core performance metrics an engagement finance associate
watches every month:

- **Utilization** -- actual hours worked vs. budgeted hours, by role. Tells
  you whether the team is pacing to the staffing plan.
- **Rate realization** -- the standard billing value actually captured on
  the timesheet (hours x the rate recorded) vs. what it should be (hours x
  the correct rate card for that labor category). A pure data-quality
  signal: it moves only when someone logs time under the wrong labor
  category/rate, independent of utilization.

It then rolls both up to an engagement-level schedule/budget comparison
(classic earned-value logic: % of budget consumed vs. % of the period of
performance elapsed) to flag engagements trending over budget or off pace.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from .engagement_setup import (
    AS_OF_DATE,
    ContractType,
    Engagement,
    ROLE_ORDER,
    STANDARD_BILL_RATES,
    STANDARD_COST_RATES,
)
from .time_expense_tracking import _month_weight  # shared S-curve shape

OVER_BUDGET_THRESHOLD = 1.00  # budget consumed exceeds 100% of total budget
TRENDING_OVER_VARIANCE = 0.15  # budget-consumed-pct minus schedule-pct
BEHIND_PACE_VARIANCE = -0.15


def _planned_hours_by_month(engagement: Engagement) -> dict[tuple, float]:
    """Spread each role's total budgeted hours across the full planned engagement life."""
    months = engagement.month_range(as_of=engagement.end_date)
    if not months:
        return {}
    weights = [_month_weight(i, len(months)) for i in range(len(months))]
    weight_sum = sum(weights) or 1.0
    planned: dict[tuple, float] = {}
    for role, role_budget in engagement.role_budgets.items():
        for i, month_start in enumerate(months):
            planned[(engagement.engagement_id, role, month_start)] = (
                role_budget.budgeted_hours * weights[i] / weight_sum
            )
    return planned


def build_budget_actual_by_role_month(
    engagements: list[Engagement], timesheet_df: pd.DataFrame, as_of: date = AS_OF_DATE
) -> pd.DataFrame:
    """Return one row per (engagement, role, month) with planned vs. actual hours/dollars."""
    planned_rows = []
    for engagement in engagements:
        for (eng_id, role, month), hours in _planned_hours_by_month(engagement).items():
            planned_rows.append(
                {
                    "engagement_id": eng_id,
                    "role": role.value,
                    "month": month,
                    "budgeted_hours": hours,
                    "budgeted_dollars": hours * STANDARD_BILL_RATES[role],
                }
            )
    planned_df = pd.DataFrame(planned_rows)

    if timesheet_df.empty:
        actual_df = pd.DataFrame(
            columns=["engagement_id", "role", "month", "actual_hours", "actual_dollars_standard", "actual_dollars_recorded"]
        )
    else:
        grouped = timesheet_df.groupby(["engagement_id", "role", "month"], as_index=False)
        actual_df = grouped.agg(
            actual_hours=("hours", "sum"),
            actual_dollars_recorded=("billed_amount", "sum"),
        )
        rate_lookup = {role.value: rate for role, rate in STANDARD_BILL_RATES.items()}
        timesheet_df = timesheet_df.copy()
        timesheet_df["standard_dollars"] = timesheet_df["hours"] * timesheet_df["role"].map(rate_lookup)
        actual_df["actual_dollars_standard"] = (
            timesheet_df.groupby(["engagement_id", "role", "month"])["standard_dollars"].sum().values
        )

    merged = planned_df.merge(actual_df, on=["engagement_id", "role", "month"], how="outer")
    for col in ["budgeted_hours", "budgeted_dollars", "actual_hours", "actual_dollars_standard", "actual_dollars_recorded"]:
        merged[col] = merged[col].fillna(0.0)
    merged = merged[merged["month"] <= as_of]
    merged["utilization_pct"] = merged.apply(
        lambda r: (r["actual_hours"] / r["budgeted_hours"]) if r["budgeted_hours"] else None, axis=1
    )
    merged["rate_realization_pct"] = merged.apply(
        lambda r: (r["actual_dollars_recorded"] / r["actual_dollars_standard"]) if r["actual_dollars_standard"] else None,
        axis=1,
    )
    return merged.sort_values(["engagement_id", "month", "role"]).reset_index(drop=True)


def _status_for(budget_consumed_pct: float, schedule_pct: float) -> str:
    variance = budget_consumed_pct - schedule_pct
    if budget_consumed_pct > OVER_BUDGET_THRESHOLD:
        return "Over Budget"
    if variance > TRENDING_OVER_VARIANCE:
        return "Trending Over Budget"
    if variance < BEHIND_PACE_VARIANCE:
        return "Behind Pace / Under-Utilized"
    return "On Track"


def build_engagement_summary(
    engagements: list[Engagement], timesheet_df: pd.DataFrame, as_of: date = AS_OF_DATE
) -> pd.DataFrame:
    """One row per engagement: budget-to-date consumption, schedule position, and status."""
    rows = []
    for engagement in engagements:
        # Schedule position uses the same staffing-curve basis as the plan itself
        # (cumulative planned hours through as_of / total planned hours), not a
        # naive linear day-count -- a ramp-up engagement is *supposed* to consume
        # budget faster than time early on, so comparing against a curve-aware
        # baseline avoids flagging every new engagement as "over pace" by construction.
        planned_by_month = _planned_hours_by_month(engagement)
        total_planned = sum(planned_by_month.values()) or 1.0
        planned_to_date = sum(h for (_, _, m), h in planned_by_month.items() if m <= as_of)
        schedule_pct = planned_to_date / total_planned

        eng_ts = timesheet_df[timesheet_df["engagement_id"] == engagement.engagement_id] if not timesheet_df.empty else timesheet_df
        actual_hours = eng_ts["hours"].sum() if not eng_ts.empty else 0.0
        actual_dollars_standard = (
            (eng_ts["hours"] * eng_ts["role"].map({r.value: STANDARD_BILL_RATES[r] for r in ROLE_ORDER})).sum()
            if not eng_ts.empty
            else 0.0
        )

        # Cost-plus contracts are priced (and capped) on allowable cost + fee, not commercial
        # bill rates -- comparing bill-rate-valued actuals to a cost-based ceiling would make
        # every cost-plus engagement look like it blows through its ceiling almost immediately.
        # Use the basis that actually matches how each contract type's ceiling was set.
        if engagement.contract_type == ContractType.COST_PLUS:
            actual_cost_dollars = (
                (eng_ts["hours"] * eng_ts["role"].map({r.value: STANDARD_COST_RATES[r] for r in ROLE_ORDER})).sum()
                if not eng_ts.empty
                else 0.0
            )
            ceiling_basis = actual_cost_dollars * (1 + (engagement.cost_plus_fee_pct or 0.0))
        else:
            ceiling_basis = actual_dollars_standard

        budget_consumed_pct = (actual_hours / engagement.total_budget_hours) if engagement.total_budget_hours else 0.0
        status = _status_for(budget_consumed_pct, schedule_pct)

        rows.append(
            {
                "engagement_id": engagement.engagement_id,
                "client_name": engagement.client_name,
                "engagement_name": engagement.engagement_name,
                "contract_type": engagement.contract_type.value,
                "engagement_leader": engagement.engagement_leader,
                "start_date": engagement.start_date,
                "end_date": engagement.end_date,
                "budgeted_hours": engagement.total_budget_hours,
                "budgeted_dollars": engagement.total_budget_dollars,
                "contract_ceiling_dollars": engagement.contract_ceiling_dollars,
                "approved_ceiling_dollars": engagement.approved_ceiling_dollars,
                "has_change_order": len(engagement.change_orders) > 0,
                "actual_hours_to_date": round(actual_hours, 1),
                "actual_dollars_to_date": round(actual_dollars_standard, 2),
                "actual_ceiling_basis_to_date": round(ceiling_basis, 2),
                "schedule_pct_elapsed": round(schedule_pct, 3),
                "budget_pct_consumed": round(budget_consumed_pct, 3),
                "schedule_budget_variance": round(budget_consumed_pct - schedule_pct, 3),
                "status": status,
            }
        )
    return pd.DataFrame(rows).sort_values("engagement_id").reset_index(drop=True)

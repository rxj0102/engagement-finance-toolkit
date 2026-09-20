"""Time & expense tracking: synthetic timesheet and expense data logged against engagements.

Generates realistic entries against the budgets defined in ``engagement_setup``,
including a light, realistic rate of data-entry errors (missing task codes,
out-of-period entries, rate mismatches, missing approvals) that discrepancy
detection is expected to catch downstream. Anomalies are injected into the
raw field values only -- entries are never pre-tagged as "bad" -- so detection
logic in ``discrepancy_detection.py`` has to actually find them, the same way
an associate reviewing raw timesheet exports would.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .engagement_setup import (
    AS_OF_DATE,
    Engagement,
    Role,
    STANDARD_BILL_RATES,
    StaffMember,
)

EXPENSE_CATEGORIES = ["Travel", "Lodging", "Meals", "Ground Transport", "Other"]
_EXPENSE_TYPICAL_RANGE = {
    "Travel": (180.0, 650.0),
    "Lodging": (140.0, 320.0),
    "Meals": (25.0, 95.0),
    "Ground Transport": (15.0, 120.0),
    "Other": (20.0, 300.0),
}

# Approvals are expected within this many calendar days of the work date;
# anything older that is still unapproved is a control failure, not "pending".
NORMAL_APPROVAL_CYCLE_DAYS = 14


@dataclass
class TimeEntry:
    """One timesheet line: hours logged by one staff member, on one engagement, one day."""

    entry_id: str
    engagement_id: str
    staff_id: str
    role: Role
    work_date: date
    hours: float
    task_code: str | None
    bill_rate: float
    billable: bool
    approved: bool
    entered_date: date


@dataclass
class ExpenseEntry:
    """One reimbursable/billable expense line logged against an engagement."""

    expense_id: str
    engagement_id: str
    staff_id: str
    expense_date: date
    category: str
    amount: float
    billable: bool
    approved: bool
    entered_date: date


def _month_weight(month_index: int, total_months: int) -> float:
    """A mild ramp-up/steady-state/wind-down shape rather than a flat spread."""
    if total_months <= 1:
        return 1.0
    x = month_index / (total_months - 1)
    return 0.55 + 0.9 * math.sin(math.pi * x)


def _workdays_in_month(month_start: date) -> list[date]:
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)
    days = []
    cursor = month_start
    while cursor < next_month:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _task_code_for(engagement: Engagement, role: Role) -> str:
    return f"{engagement.engagement_id}-{role.value[:3].upper()}"


def generate_timesheets(
    engagements: list[Engagement],
    staff: list[StaffMember],
    as_of: date = AS_OF_DATE,
    seed: int = 7,
) -> list[TimeEntry]:
    """Generate synthetic timesheet entries for every engagement, through ``as_of``.

    Each engagement/role gets a target actual-hours trajectory (a fraction of
    its budget, varying by engagement so some run under, on, and over budget)
    spread across its assigned staff and the workdays in each active month.
    """
    rng = random.Random(seed)
    staff_by_id = {s.staff_id: s for s in staff}
    entries: list[TimeEntry] = []
    counter = 0

    for engagement in engagements:
        months = engagement.month_range(as_of)
        if not months:
            continue

        # Per-engagement adherence factor: how actual hours track budgeted hours.
        # Weighted so most engagements run close to plan and a minority drift.
        adherence = rng.choices(
            population=[0.72, 0.88, 1.00, 1.08, 1.22, 1.35],
            weights=[0.10, 0.20, 0.30, 0.20, 0.12, 0.08],
            k=1,
        )[0]

        for role, role_budget in engagement.role_budgets.items():
            assigned = engagement.assigned_staff.get(role, [])
            if not assigned or role_budget.budgeted_hours <= 0:
                continue

            weights = [_month_weight(i, len(months)) for i in range(len(months))]
            weight_sum = sum(weights) or 1.0
            target_total = role_budget.budgeted_hours * adherence

            for i, month_start in enumerate(months):
                month_target_hours = target_total * weights[i] / weight_sum
                # spread across days worked and staff assigned to this role
                workdays = _workdays_in_month(month_start)
                if not workdays:
                    continue
                remaining_hours = month_target_hours
                # break into a handful of shift-like entries rather than one lump sum
                num_entries = max(1, round(month_target_hours / 6))
                for _ in range(num_entries):
                    if remaining_hours <= 0:
                        break
                    hours = round(min(remaining_hours, rng.uniform(2.0, 8.0)), 1)
                    remaining_hours -= hours
                    staff_id = rng.choice(assigned)
                    work_date = rng.choice(workdays)
                    entered_date = work_date + timedelta(days=rng.randint(0, 6))

                    counter += 1
                    entries.append(
                        TimeEntry(
                            entry_id=f"TS-{counter:06d}",
                            engagement_id=engagement.engagement_id,
                            staff_id=staff_id,
                            role=role,
                            work_date=work_date,
                            hours=hours,
                            task_code=_task_code_for(engagement, role),
                            bill_rate=STANDARD_BILL_RATES[role],
                            billable=engagement.contract_type.value != "Fixed-Fee",
                            approved=True,
                            entered_date=entered_date,
                        )
                    )

    _inject_time_anomalies(entries, rng)
    return entries


def _inject_time_anomalies(entries: list[TimeEntry], rng: random.Random) -> None:
    """Corrupt a small, realistic slice of entries in place -- ~4% overall."""
    n = len(entries)
    anomaly_pool = list(range(n))
    rng.shuffle(anomaly_pool)

    n_missing_code = round(n * 0.010)
    n_out_of_period = round(n * 0.010)
    n_rate_mismatch = round(n * 0.010)
    n_unapproved = round(n * 0.012)

    cursor = 0
    for idx in anomaly_pool[cursor : cursor + n_missing_code]:
        entries[idx].task_code = None
    cursor += n_missing_code

    for idx in anomaly_pool[cursor : cursor + n_out_of_period]:
        e = entries[idx]
        # logged 30-90 days after the work was performed -- well past a normal cycle
        e.entered_date = e.work_date + timedelta(days=rng.randint(30, 90))
    cursor += n_out_of_period

    for idx in anomaly_pool[cursor : cursor + n_rate_mismatch]:
        e = entries[idx]
        other_roles = [r for r in STANDARD_BILL_RATES if r != e.role]
        wrong_role = rng.choice(other_roles)
        e.bill_rate = STANDARD_BILL_RATES[wrong_role]
    cursor += n_rate_mismatch

    for idx in anomaly_pool[cursor : cursor + n_unapproved]:
        entries[idx].approved = False
    cursor += n_unapproved


def generate_expenses(
    engagements: list[Engagement],
    staff: list[StaffMember],
    as_of: date = AS_OF_DATE,
    seed: int = 11,
) -> list[ExpenseEntry]:
    """Generate synthetic billable/reimbursable expense entries against each engagement."""
    rng = random.Random(seed)
    entries: list[ExpenseEntry] = []
    counter = 0

    for engagement in engagements:
        months = engagement.month_range(as_of)
        all_staff_ids = [sid for ids in engagement.assigned_staff.values() for sid in ids]
        if not months or not all_staff_ids:
            continue
        for month_start in months:
            n_expenses = rng.randint(1, 5)
            workdays = _workdays_in_month(month_start)
            if not workdays:
                continue
            for _ in range(n_expenses):
                category = rng.choice(EXPENSE_CATEGORIES)
                low, high = _EXPENSE_TYPICAL_RANGE[category]
                amount = round(rng.uniform(low, high), 2)
                expense_date = rng.choice(workdays)
                counter += 1
                entries.append(
                    ExpenseEntry(
                        expense_id=f"EX-{counter:06d}",
                        engagement_id=engagement.engagement_id,
                        staff_id=rng.choice(all_staff_ids),
                        expense_date=expense_date,
                        category=category,
                        amount=amount,
                        billable=engagement.contract_type.value != "Fixed-Fee",
                        approved=True,
                        entered_date=expense_date + timedelta(days=rng.randint(0, 10)),
                    )
                )

    _inject_expense_anomalies(entries, rng)
    return entries


def _inject_expense_anomalies(entries: list[ExpenseEntry], rng: random.Random) -> None:
    n = len(entries)
    pool = list(range(n))
    rng.shuffle(pool)
    n_unapproved = round(n * 0.02)
    n_late = round(n * 0.02)

    for idx in pool[:n_unapproved]:
        entries[idx].approved = False
    for idx in pool[n_unapproved : n_unapproved + n_late]:
        e = entries[idx]
        e.entered_date = e.expense_date + timedelta(days=rng.randint(30, 75))


def timesheets_to_dataframe(entries: list[TimeEntry]) -> pd.DataFrame:
    df = pd.DataFrame([vars(e) for e in entries])
    if not df.empty:
        df["role"] = df["role"].apply(lambda r: r.value)
        df["month"] = df["work_date"].apply(lambda d: date(d.year, d.month, 1))
        df["billed_amount"] = df["hours"] * df["bill_rate"]
    return df


def expenses_to_dataframe(entries: list[ExpenseEntry]) -> pd.DataFrame:
    df = pd.DataFrame([vars(e) for e in entries])
    if not df.empty:
        df["month"] = df["expense_date"].apply(lambda d: date(d.year, d.month, 1))
    return df

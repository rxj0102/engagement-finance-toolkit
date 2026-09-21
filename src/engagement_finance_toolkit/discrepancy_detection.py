"""Data quality & discrepancy detection: the engagement finance QC layer.

This is the module that maps most directly to what an engagement finance
associate actually does day to day: review timesheets, expenses, budgets,
and invoices for completeness and accuracy, and escalate anything that
needs the engagement leader's attention before it becomes a client-facing
or contract-compliance problem.

Every detector below works off the *raw* data only -- nothing here reads a
pre-computed "is this bad" flag from the generator. Each function
independently re-derives its finding, the same way an associate would spot
it in a timesheet export or an aging report.

Discrepancy categories, and why each one matters on a services engagement:

- **Unbilled Time Aging** -- clean, approved, billable hours that have sat
  uninvoiced past a normal billing cycle. Left alone, this is straight
  revenue leakage: work performed that never gets billed.
- **Missing Task Code** -- a timesheet line that can't be routed to a
  billing category. Structurally blocks that time from ever reaching an
  invoice until someone corrects it.
- **Missing Approval** -- unapproved time/expense aged past the normal
  approval cycle. A control failure: nothing should be billable, or paid
  out, without a manager's sign-off.
- **Rate Mismatch** -- hours logged at a labor rate that doesn't match the
  person's actual labor category. Distorts realization and, if it reaches
  an invoice, is a contract-compliance problem (billing the wrong rate
  card line).
- **Out-of-Period Entry** -- time logged long after the work was
  performed. A timeliness/compliance issue that also makes budget-to-actual
  reporting unreliable in the periods it should have landed in.
- **Budget Overrun without Change Order** -- actual spend against an
  engagement has exceeded its authorized ceiling with no corresponding,
  client-approved change order on file. The highest-severity finding here:
  unauthorized/unrecoverable cost exposure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .engagement_setup import AS_OF_DATE, Engagement, STANDARD_BILL_RATES
from .invoicing import Invoice
from .time_expense_tracking import NORMAL_APPROVAL_CYCLE_DAYS

UNBILLED_AGING_THRESHOLD_DAYS = 45
OUT_OF_PERIOD_THRESHOLD_DAYS = 21
APPROVAL_AGING_HIGH_DAYS = 30
NEAR_CEILING_WARNING_PCT = 0.90

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


@dataclass
class Discrepancy:
    """One exception finding, ready to be escalated to an engagement leader."""

    discrepancy_id: str
    engagement_id: str
    client_name: str
    engagement_leader: str
    category: str
    severity: str
    description: str
    dollar_impact: float
    detected_date: date
    source_ids: list[str]
    recommended_action: str


def _engagement_lookup(engagements: list[Engagement]) -> dict[str, Engagement]:
    return {e.engagement_id: e for e in engagements}


def _invoiced_entry_ids(invoices: list[Invoice]) -> set[str]:
    ids: set[str] = set()
    for inv in invoices:
        for li in inv.line_items:
            if li.category == "Labor":
                ids.update(li.source_ids)
    return ids


def _invoiced_expense_ids(invoices: list[Invoice]) -> set[str]:
    ids: set[str] = set()
    for inv in invoices:
        for li in inv.line_items:
            if li.category == "Expense":
                ids.update(li.source_ids)
    return ids


def detect_missing_task_codes(timesheet_df: pd.DataFrame, engagements: list[Engagement]) -> list[Discrepancy]:
    eng_lookup = _engagement_lookup(engagements)
    findings = []
    if timesheet_df.empty:
        return findings
    bad = timesheet_df[timesheet_df["task_code"].isna()]
    for eng_id, rows in bad.groupby("engagement_id"):
        eng = eng_lookup[eng_id]
        impact = (rows["hours"] * rows["role"].map({r.value: STANDARD_BILL_RATES[r] for r in STANDARD_BILL_RATES})).sum()
        findings.append(
            Discrepancy(
                discrepancy_id=f"DQ-CODE-{eng_id}",
                engagement_id=eng_id,
                client_name=eng.client_name,
                engagement_leader=eng.engagement_leader,
                category="Missing Task Code",
                severity="Medium",
                description=f"{len(rows)} timesheet entr{'y is' if len(rows) == 1 else 'ies are'} missing a task code and cannot be routed to billing.",
                dollar_impact=round(impact, 2),
                detected_date=AS_OF_DATE,
                source_ids=rows["entry_id"].tolist(),
                recommended_action="Return to timekeeper for task code correction, then re-submit for approval and billing.",
            )
        )
    return findings


def detect_missing_approvals(timesheet_df: pd.DataFrame, engagements: list[Engagement], as_of: date = AS_OF_DATE) -> list[Discrepancy]:
    eng_lookup = _engagement_lookup(engagements)
    findings = []
    if timesheet_df.empty:
        return findings
    unapproved = timesheet_df[~timesheet_df["approved"]].copy()
    unapproved["age_days"] = unapproved["work_date"].apply(lambda d: (as_of - d).days)
    stale = unapproved[unapproved["age_days"] > NORMAL_APPROVAL_CYCLE_DAYS]
    for eng_id, rows in stale.groupby("engagement_id"):
        eng = eng_lookup[eng_id]
        impact = (rows["hours"] * rows["role"].map({r.value: STANDARD_BILL_RATES[r] for r in STANDARD_BILL_RATES})).sum()
        max_age = int(rows["age_days"].max())
        severity = "High" if max_age > APPROVAL_AGING_HIGH_DAYS else "Medium"
        findings.append(
            Discrepancy(
                discrepancy_id=f"DQ-APPR-{eng_id}",
                engagement_id=eng_id,
                client_name=eng.client_name,
                engagement_leader=eng.engagement_leader,
                category="Missing Approval",
                severity=severity,
                description=f"{len(rows)} time entr{'y is' if len(rows) == 1 else 'ies are'} unapproved, oldest {max_age} days past the work date "
                            f"(normal cycle is {NORMAL_APPROVAL_CYCLE_DAYS} days).",
                dollar_impact=round(impact, 2),
                detected_date=as_of,
                source_ids=rows["entry_id"].tolist(),
                recommended_action="Escalate to approving manager for immediate sign-off; time cannot be billed until approved.",
            )
        )
    return findings


def detect_rate_mismatches(timesheet_df: pd.DataFrame, engagements: list[Engagement]) -> list[Discrepancy]:
    eng_lookup = _engagement_lookup(engagements)
    findings = []
    if timesheet_df.empty:
        return findings
    df = timesheet_df.copy()
    df["standard_rate"] = df["role"].map({r.value: STANDARD_BILL_RATES[r] for r in STANDARD_BILL_RATES})
    mismatched = df[df["bill_rate"] != df["standard_rate"]]
    for eng_id, rows in mismatched.groupby("engagement_id"):
        eng = eng_lookup[eng_id]
        impact = ((rows["bill_rate"] - rows["standard_rate"]) * rows["hours"]).sum()
        findings.append(
            Discrepancy(
                discrepancy_id=f"DQ-RATE-{eng_id}",
                engagement_id=eng_id,
                client_name=eng.client_name,
                engagement_leader=eng.engagement_leader,
                category="Rate Mismatch",
                severity="Medium",
                description=f"{len(rows)} entr{'y is' if len(rows) == 1 else 'ies are'} logged at a labor rate that doesn't match the "
                            f"standard rate card for the recorded role (net rate variance ${impact:,.2f}).",
                dollar_impact=round(abs(impact), 2),
                detected_date=AS_OF_DATE,
                source_ids=rows["entry_id"].tolist(),
                recommended_action="Confirm correct labor category with resource manager and correct the rate before invoicing.",
            )
        )
    return findings


def detect_out_of_period_entries(timesheet_df: pd.DataFrame, engagements: list[Engagement], as_of: date = AS_OF_DATE) -> list[Discrepancy]:
    eng_lookup = _engagement_lookup(engagements)
    findings = []
    if timesheet_df.empty:
        return findings
    df = timesheet_df.copy()
    df["lag_days"] = (df["entered_date"] - df["work_date"]).apply(lambda td: td.days)
    late = df[df["lag_days"] > OUT_OF_PERIOD_THRESHOLD_DAYS]
    for eng_id, rows in late.groupby("engagement_id"):
        eng = eng_lookup[eng_id]
        max_lag = int(rows["lag_days"].max())
        findings.append(
            Discrepancy(
                discrepancy_id=f"DQ-OOP-{eng_id}",
                engagement_id=eng_id,
                client_name=eng.client_name,
                engagement_leader=eng.engagement_leader,
                category="Out-of-Period Entry",
                severity="Low",
                description=f"{len(rows)} time entr{'y was' if len(rows) == 1 else 'ies were'} logged {max_lag} days after the work date "
                            f"at worst (policy threshold is {OUT_OF_PERIOD_THRESHOLD_DAYS} days).",
                dollar_impact=round((rows["hours"] * rows["role"].map({r.value: STANDARD_BILL_RATES[r] for r in STANDARD_BILL_RATES})).sum(), 2),
                detected_date=as_of,
                source_ids=rows["entry_id"].tolist(),
                recommended_action="Reinforce weekly timesheet submission policy with team; review affected months for reporting accuracy.",
            )
        )
    return findings


def detect_unbilled_time_aging(
    timesheet_df: pd.DataFrame,
    engagements: list[Engagement],
    invoices: list[Invoice],
    as_of: date = AS_OF_DATE,
    threshold_days: int = UNBILLED_AGING_THRESHOLD_DAYS,
) -> list[Discrepancy]:
    """Clean, billable, approved, coded hours that simply haven't made it onto an invoice yet."""
    eng_lookup = _engagement_lookup(engagements)
    invoiced_ids = _invoiced_entry_ids(invoices)
    findings = []
    if timesheet_df.empty:
        return findings
    df = timesheet_df.copy()
    df["age_days"] = df["work_date"].apply(lambda d: (as_of - d).days)
    eligible = df[df["billable"] & df["approved"] & df["task_code"].notna()]
    unbilled = eligible[~eligible["entry_id"].isin(invoiced_ids) & (eligible["age_days"] > threshold_days)]
    for eng_id, rows in unbilled.groupby("engagement_id"):
        eng = eng_lookup[eng_id]
        impact = (rows["hours"] * rows["role"].map({r.value: STANDARD_BILL_RATES[r] for r in STANDARD_BILL_RATES})).sum()
        max_age = int(rows["age_days"].max())
        severity = "High" if max_age > threshold_days * 1.5 else "Medium"
        findings.append(
            Discrepancy(
                discrepancy_id=f"DQ-UNBILL-{eng_id}",
                engagement_id=eng_id,
                client_name=eng.client_name,
                engagement_leader=eng.engagement_leader,
                category="Unbilled Time Aging",
                severity=severity,
                description=f"${impact:,.2f} of approved, billable time ({round(rows['hours'].sum(), 1)} hrs) has not been invoiced, "
                            f"oldest entry {max_age} days old.",
                dollar_impact=round(impact, 2),
                detected_date=as_of,
                source_ids=rows["entry_id"].tolist(),
                recommended_action="Include in next billing cycle; if a prior invoice run missed this work, investigate why.",
            )
        )
    return findings


def detect_unbilled_expense_aging(
    expense_df: pd.DataFrame,
    engagements: list[Engagement],
    invoices: list[Invoice],
    as_of: date = AS_OF_DATE,
    threshold_days: int = UNBILLED_AGING_THRESHOLD_DAYS,
) -> list[Discrepancy]:
    eng_lookup = _engagement_lookup(engagements)
    invoiced_ids = _invoiced_expense_ids(invoices)
    findings = []
    if expense_df.empty:
        return findings
    df = expense_df.copy()
    df["age_days"] = df["expense_date"].apply(lambda d: (as_of - d).days)
    eligible = df[df["billable"] & df["approved"]]
    unbilled = eligible[~eligible["expense_id"].isin(invoiced_ids) & (eligible["age_days"] > threshold_days)]
    for eng_id, rows in unbilled.groupby("engagement_id"):
        eng = eng_lookup[eng_id]
        impact = rows["amount"].sum()
        findings.append(
            Discrepancy(
                discrepancy_id=f"DQ-UNBILLEXP-{eng_id}",
                engagement_id=eng_id,
                client_name=eng.client_name,
                engagement_leader=eng.engagement_leader,
                category="Unbilled Expense Aging",
                severity="Low",
                description=f"${impact:,.2f} of approved, billable expenses have not been invoiced (oldest "
                            f"{int(rows['age_days'].max())} days).",
                dollar_impact=round(impact, 2),
                detected_date=as_of,
                source_ids=rows["expense_id"].tolist(),
                recommended_action="Include in next billing cycle.",
            )
        )
    return findings


def detect_budget_overruns(engagement_summary_df: pd.DataFrame, engagements: list[Engagement], as_of: date = AS_OF_DATE) -> list[Discrepancy]:
    """Flag engagements spending past their authorized ceiling (with or without a change order)."""
    eng_lookup = _engagement_lookup(engagements)
    findings = []
    for _, row in engagement_summary_df.iterrows():
        eng = eng_lookup[row["engagement_id"]]
        # Cost-plus ceilings are priced in cost + fee terms, not commercial bill rates --
        # actual_ceiling_basis_to_date uses whichever basis actually matches the ceiling.
        actual = row["actual_ceiling_basis_to_date"]
        approved_ceiling = row["approved_ceiling_dollars"]
        original_ceiling = row["contract_ceiling_dollars"]
        has_co = row["has_change_order"]

        if actual > approved_ceiling:
            overage = actual - approved_ceiling
            findings.append(
                Discrepancy(
                    discrepancy_id=f"DQ-OVER-{eng.engagement_id}",
                    engagement_id=eng.engagement_id,
                    client_name=eng.client_name,
                    engagement_leader=eng.engagement_leader,
                    category="Budget Overrun without Change Order" if not has_co else "Budget Overrun (Exceeds Approved Change Order)",
                    severity="High",
                    description=(
                        f"Actual spend (${actual:,.2f}) exceeds the {'original' if not has_co else 'change-order-adjusted'} "
                        f"authorized ceiling (${approved_ceiling:,.2f}) by ${overage:,.2f}."
                        + ("" if has_co else " No change order is on file for this engagement.")
                    ),
                    dollar_impact=round(overage, 2),
                    detected_date=as_of,
                    source_ids=[],
                    recommended_action=(
                        "Escalate to engagement leader immediately: confirm scope with client and process a change order, "
                        "or stop unauthorized spend."
                        if not has_co
                        else "Escalate to engagement leader: even the approved change order ceiling has been exceeded."
                    ),
                )
            )
        elif actual > original_ceiling and not has_co:
            findings.append(
                Discrepancy(
                    discrepancy_id=f"DQ-OVER-{eng.engagement_id}",
                    engagement_id=eng.engagement_id,
                    client_name=eng.client_name,
                    engagement_leader=eng.engagement_leader,
                    category="Budget Overrun without Change Order",
                    severity="High",
                    description=f"Actual spend (${actual:,.2f}) exceeds the original authorized ceiling (${original_ceiling:,.2f}); "
                                f"no change order is on file.",
                    dollar_impact=round(actual - original_ceiling, 2),
                    detected_date=as_of,
                    source_ids=[],
                    recommended_action="Escalate to engagement leader immediately: confirm scope with client and process a change order.",
                )
            )
        elif actual > approved_ceiling * NEAR_CEILING_WARNING_PCT:
            findings.append(
                Discrepancy(
                    discrepancy_id=f"DQ-NEARCEIL-{eng.engagement_id}",
                    engagement_id=eng.engagement_id,
                    client_name=eng.client_name,
                    engagement_leader=eng.engagement_leader,
                    category="Approaching Budget Ceiling",
                    severity="Low",
                    description=f"Actual spend (${actual:,.2f}) has reached {actual / approved_ceiling:.0%} of the authorized "
                                f"ceiling (${approved_ceiling:,.2f}).",
                    dollar_impact=round(approved_ceiling - actual, 2),
                    detected_date=as_of,
                    source_ids=[],
                    recommended_action="Monitor closely; discuss scope/change-order runway with engagement leader before ceiling is reached.",
                )
            )
    return findings


def run_all_detectors(
    engagements: list[Engagement],
    timesheet_df: pd.DataFrame,
    expense_df: pd.DataFrame,
    invoices: list[Invoice],
    engagement_summary_df: pd.DataFrame,
    as_of: date = AS_OF_DATE,
) -> list[Discrepancy]:
    """Run every detector and return the full, unsorted finding set."""
    findings: list[Discrepancy] = []
    findings += detect_missing_task_codes(timesheet_df, engagements)
    findings += detect_missing_approvals(timesheet_df, engagements, as_of)
    findings += detect_rate_mismatches(timesheet_df, engagements)
    findings += detect_out_of_period_entries(timesheet_df, engagements, as_of)
    findings += detect_unbilled_time_aging(timesheet_df, engagements, invoices, as_of)
    findings += detect_unbilled_expense_aging(expense_df, engagements, invoices, as_of)
    findings += detect_budget_overruns(engagement_summary_df, engagements, as_of)
    return findings


def build_exception_report(findings: list[Discrepancy]) -> pd.DataFrame:
    """The prioritized exception report: most severe / highest dollar impact first."""
    rows = [
        {
            "discrepancy_id": f.discrepancy_id,
            "engagement_id": f.engagement_id,
            "client_name": f.client_name,
            "engagement_leader": f.engagement_leader,
            "category": f.category,
            "severity": f.severity,
            "dollar_impact": f.dollar_impact,
            "description": f.description,
            "recommended_action": f.recommended_action,
            "detected_date": f.detected_date,
            "n_source_records": len(f.source_ids),
        }
        for f in findings
    ]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_severity_rank"] = df["severity"].map(SEVERITY_ORDER)
    df = df.sort_values(["_severity_rank", "dollar_impact"], ascending=[True, False]).drop(columns="_severity_rank")
    return df.reset_index(drop=True)


def build_leader_summary(exception_report: pd.DataFrame) -> pd.DataFrame:
    """Escalation rollup by engagement leader -- what to put at the top of the email."""
    if exception_report.empty:
        return exception_report
    summary = (
        exception_report.groupby("engagement_leader")
        .agg(
            open_exceptions=("discrepancy_id", "count"),
            high_severity=("severity", lambda s: (s == "High").sum()),
            medium_severity=("severity", lambda s: (s == "Medium").sum()),
            low_severity=("severity", lambda s: (s == "Low").sum()),
            total_dollar_impact=("dollar_impact", "sum"),
            engagements_affected=("engagement_id", "nunique"),
        )
        .reset_index()
        .sort_values(["high_severity", "total_dollar_impact"], ascending=[False, False])
    )
    return summary

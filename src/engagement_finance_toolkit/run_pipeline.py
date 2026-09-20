"""End-to-end pipeline: generate the synthetic portfolio and build the Excel workbook.

Run with ``python -m engagement_finance_toolkit.run_pipeline`` (or via the
``engagement-finance-toolkit`` console script installed with the package).
"""

from __future__ import annotations

import argparse

from .budget_actual import build_budget_actual_by_role_month, build_engagement_summary
from .discrepancy_detection import build_exception_report, build_leader_summary, run_all_detectors
from .engagement_setup import AS_OF_DATE, generate_portfolio
from .invoicing import generate_invoices, reconcile_invoice_to_source
from .reporting import build_workbook
from .time_expense_tracking import (
    expenses_to_dataframe,
    generate_expenses,
    generate_timesheets,
    timesheets_to_dataframe,
)


def run(output_path: str = "output/engagement_finance_toolkit.xlsx", verbose: bool = True) -> str:
    """Run the full engagement finance pipeline and write the portfolio workbook."""
    engagements, staff = generate_portfolio()
    timesheet_entries = generate_timesheets(engagements, staff)
    expense_entries = generate_expenses(engagements, staff)
    timesheet_df = timesheets_to_dataframe(timesheet_entries)
    expense_df = expenses_to_dataframe(expense_entries)

    budget_actual_df = build_budget_actual_by_role_month(engagements, timesheet_df)
    engagement_summary_df = build_engagement_summary(engagements, timesheet_df)

    invoices = generate_invoices(engagements, timesheet_df, expense_df)
    reconciliation_failures = [
        r for inv in invoices if not (r := reconcile_invoice_to_source(inv, timesheet_df, expense_df))["ties_out"]
    ]
    if reconciliation_failures:
        raise RuntimeError(f"{len(reconciliation_failures)} invoice(s) failed reconciliation to source data")

    findings = run_all_detectors(engagements, timesheet_df, expense_df, invoices, engagement_summary_df)
    exception_report_df = build_exception_report(findings)
    leader_summary_df = build_leader_summary(exception_report_df)

    path = build_workbook(
        engagements,
        budget_actual_df,
        engagement_summary_df,
        invoices,
        exception_report_df,
        leader_summary_df,
        as_of=AS_OF_DATE,
        output_path=output_path,
    )

    if verbose:
        print(f"Engagements:              {len(engagements)}")
        print(f"Staff pool:               {len(staff)}")
        print(f"Timesheet entries:        {len(timesheet_df)}")
        print(f"Expense entries:          {len(expense_df)}")
        print(f"Draft invoices generated: {len(invoices)}")
        print(f"Invoice total value:      ${sum(inv.total_amount for inv in invoices):,.2f}")
        print(f"Exception findings:       {len(exception_report_df)}")
        if not exception_report_df.empty:
            print(exception_report_df["severity"].value_counts().to_string())
        print(f"Workbook written to:      {path}")

    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the engagement finance toolkit pipeline end-to-end.")
    parser.add_argument("--output", default="output/engagement_finance_toolkit.xlsx", help="Path to write the Excel workbook.")
    args = parser.parse_args()
    run(output_path=args.output)


if __name__ == "__main__":
    main()

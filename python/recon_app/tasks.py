from typing import List, Literal, Optional
from crewai import Agent, Task
from pydantic import BaseModel, Field

ExpenseCategory = Literal[
    "Cloud Infrastructure",
    "SaaS & Subscriptions",
    "Utilities & Telecom",
    "Travel & Meals",
    "Office & Hardware",
    "Professional Services",
    "Uncategorized",
]


class ReconciledRow(BaseModel):
    txn_id: str
    date: str
    normalized_vendor: str
    category: ExpenseCategory
    amount_inr: float
    gst_amount_inr: float = 0.0
    gstin: Optional[str] = None
    invoice_ref: Optional[str] = None
    status: Literal["MATCHED", "UNMATCHED_RECEIPT", "RESOLVED_FLAG"]
    audit_note: Optional[str] = None


class LedgerOutput(BaseModel):
    rows: List[ReconciledRow] = Field(default_factory=list)
    missing_invoices_to_chase: List[str] = Field(default_factory=list)


def build_tasks(
    categorizer: Agent, auditor: Agent,
    reporter: Agent, rust_payload_json: str,) -> list[Task]:

    categorize_task = Task(
        description=(
            "Analyze the following deterministic reconciliation payload from the Rust engine:\n"
            f"{rust_payload_json}\n\n"
            "Note: All monetary values (`amount_paise`, `tax_paise`) are in paise; divide by 100 for INR.\n"
            "Decode all `raw_narration` strings across `matched`, `unmatched_debits`, and `flagged` items "
            "into clean vendor names and assign each strictly ONE of these categories: "
            "'Cloud Infrastructure', 'SaaS & Subscriptions', 'Utilities & Telecom', "
            "'Travel & Meals', 'Office & Hardware', 'Professional Services', or 'Uncategorized'."
        ),
        expected_output="A complete mapping of every txn_id to normalized_vendor, INR amounts, and exact category.",
        agent=categorizer,
    )

    audit_task = Task(
        description=(
            "Inspect the raw Rust reconciliation payload below alongside the categorizer's output:\n"
            f"{rust_payload_json}\n\n"
            "- For every item in `unmatched_debits`, flag it as status `UNMATCHED_RECEIPT` and list the missing invoice to chase.\n"
            "- For `TAX_CHARGED_WITHOUT_GSTIN` in `flagged`, note the missing GSTIN for input tax credit review.\n"
            "- For `MULTIPLE_MATCHING_INVOICES` in `flagged`, recommend the best candidate invoice.\n"
            "- For `unreadable_pdfs`, list the scanned receipts requiring OCR/manual review."
        ),
        expected_output="Concrete audit resolutions, status tags, and missing invoice chase list.",
        agent=auditor,
        context=[categorize_task],
    )

    approval_and_report_task = Task(
        description=(
            "Present a clear summary of all flagged anomalies, tax mismatches, and proposed resolutions "
            "to the human operator for confirmation. Incorporate any human feedback, then compile all "
            "`matched`, `unmatched_debits`, and `flagged` transactions into the final `LedgerOutput`.\n"
            "STRICT RULE: For any row with status `UNMATCHED_RECEIPT` (no matched invoice), set `gstin=None` "
            "and `gst_amount_inr=0.0`. Never guess or fabricate GSTIN numbers from memory."
        ),
        expected_output="Final structured LedgerOutput containing all reconciled rows and missing invoices.",
        agent=reporter,
        context=[categorize_task, audit_task],
        human_input=True,
        output_pydantic=LedgerOutput,
    )   

    return [categorize_task, audit_task, approval_and_report_task]
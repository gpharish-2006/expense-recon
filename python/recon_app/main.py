import glob
import json
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from recon_app import recon_engine
from recon_app.crew import run_reconciliation_crew


def write_outputs(ledger, raw_payload: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame([row.model_dump() for row in ledger.rows])
    csv_path = output_dir / "reconciled_expenses.csv"
    df.to_csv(csv_path, index=False)

    total_spend = df["amount_inr"].sum() if not df.empty else 0.0
    total_gst = df["gst_amount_inr"].sum() if not df.empty else 0.0

    lines = [
        "# Expense Reconciliation Audit Summary\n",
        f"- **Total Reconciled Spend:** INR {total_spend:,.2f}",
        f"- **Total GST / Tax Recorded:** INR {total_gst:,.2f}",
        f"- **Auto-Matched by Rust Engine:** {len(raw_payload.get('matched', []))}",
        f"- **Flagged for Review:** {len(raw_payload.get('flagged', []))}",
        f"- **Unmatched Bank Debits:** {len(raw_payload.get('unmatched_debits', []))}\n",
        "## Missing Invoices to Chase",
    ]

    if ledger.missing_invoices_to_chase:
        lines.extend([f"- {item}" for item in ledger.missing_invoices_to_chase])
    else:
        lines.append("- None")

    unreadable = raw_payload.get("unreadable_pdfs", [])
    if unreadable:
        lines.append("\n## Unreadable / Scanned PDFs (OCR Needed)")
        lines.extend([f"- {pdf}" for pdf in unreadable])

    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    load_dotenv()
    base_dir = Path(__file__).resolve().parents[2]
    csv_files = glob.glob(str(base_dir / "data" / "statements" / "*.csv"))
    pdf_dir = str(base_dir / "data" / "invoices")
    output_dir = base_dir / "output"

    if not csv_files:
        raise FileNotFoundError(f"No statement CSVs found in {base_dir / 'data' / 'statements'}")

    payload_json = recon_engine.reconcile_batch(csv_files, pdf_dir)
    raw_payload = json.loads(payload_json)
    print(
        f"[Rust Engine] Matched: {len(raw_payload['matched'])} | "
        f"Unmatched: {len(raw_payload['unmatched_debits'])} | "
        f"Flagged: {len(raw_payload['flagged'])}"
    )

    ledger = run_reconciliation_crew(payload_json)

    write_outputs(ledger, raw_payload, output_dir)
    print(f"\nReconciliation complete. Files written to {output_dir}/")


if __name__ == "__main__":
    main()
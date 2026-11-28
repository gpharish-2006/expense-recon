import glob
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from recon_app import recon_engine
from recon_app.crew import run_reconciliation_crew


def _txn_hash(txn: dict) -> str:
    raw = f"{txn.get('date', '')}|{txn.get('raw_narration', '')}|{txn.get('amount_paise', 0)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pdf_hash(pdf_path: Path) -> str:
    return hashlib.sha256(pdf_path.read_bytes()).hexdigest()


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"processed_txns": [], "processed_pdfs": []}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def filter_new_payload(raw_payload: dict, pdf_dir: Path, state: dict) -> tuple[dict, int, int]:
    seen_txns = set(state.get("processed_txns", []))
    seen_pdfs = set(state.get("processed_pdfs", []))

    current_pdf_hashes = {}
    if pdf_dir.exists():
        for pdf_file in pdf_dir.rglob("*.pdf"):
            current_pdf_hashes[pdf_file.name] = _pdf_hash(pdf_file)

    new_pdfs_count = sum(1 for h in current_pdf_hashes.values() if h not in seen_pdfs)

    def keep_txn(txn: dict) -> bool:
        return _txn_hash(txn) not in seen_txns

    filtered = {
        "matched": [m for m in raw_payload.get("matched", []) if keep_txn(m.get("txn", {}))],
        "unmatched_debits": [t for t in raw_payload.get("unmatched_debits", []) if keep_txn(t)],
        "unmatched_invoices": [
            inv
            for inv in raw_payload.get("unmatched_invoices", [])
            if current_pdf_hashes.get(inv.get("file_name", "")) not in seen_pdfs
        ],
        "flagged": [f for f in raw_payload.get("flagged", []) if keep_txn(f.get("txn", {}))],
        "unreadable_pdfs": [
            name
            for name in raw_payload.get("unreadable_pdfs", [])
            if current_pdf_hashes.get(name) not in seen_pdfs
        ],
    }

    total_before = (
        len(raw_payload.get("matched", []))
        + len(raw_payload.get("unmatched_debits", []))
        + len(raw_payload.get("flagged", []))
    )
    total_after = (
        len(filtered["matched"])
        + len(filtered["unmatched_debits"])
        + len(filtered["flagged"])
    )
    skipped_txns = total_before - total_after

    # Collect hashes to commit after successful run
    pending_txn_hashes = set()
    for m in filtered["matched"]:
        pending_txn_hashes.add(_txn_hash(m["txn"]))
    for t in filtered["unmatched_debits"]:
        pending_txn_hashes.add(_txn_hash(t))
    for f in filtered["flagged"]:
        pending_txn_hashes.add(_txn_hash(f["txn"]))

    pending_pdf_hashes = {
        h for h in current_pdf_hashes.values() if h not in seen_pdfs
    }

    return filtered, skipped_txns, new_pdfs_count


def commit_state(state_path: Path, state: dict, payload_to_commit: dict, pdf_dir: Path) -> None:
    seen_txns = set(state.get("processed_txns", []))
    seen_pdfs = set(state.get("processed_pdfs", []))

    for m in payload_to_commit.get("matched", []):
        seen_txns.add(_txn_hash(m["txn"]))
    for t in payload_to_commit.get("unmatched_debits", []):
        seen_txns.add(_txn_hash(t))
    for f in payload_to_commit.get("flagged", []):
        seen_txns.add(_txn_hash(f["txn"]))

    if pdf_dir.exists():
        for pdf_file in pdf_dir.rglob("*.pdf"):
            seen_pdfs.add(_pdf_hash(pdf_file))

    state["processed_txns"] = sorted(seen_txns)
    state["processed_pdfs"] = sorted(seen_pdfs)
    save_state(state_path, state)


def write_outputs(
    ledger,
    raw_payload: dict,
    output_dir: Path,
    skipped_txns: int,
    new_pdfs_count: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Append or write reconciled_expenses.csv
    csv_path = output_dir / "reconciled_expenses.csv"
    new_df = pd.DataFrame([row.model_dump() for row in ledger.rows])
    if csv_path.exists() and not new_df.empty:
        existing_df = pd.read_csv(csv_path)
        combined_df = pd.concat([existing_df, new_df], ignore_index=True).drop_duplicates(
            subset=["txn_id", "date", "amount_inr"], keep="last"
        )
    else:
        combined_df = new_df
    combined_df.to_csv(csv_path, index=False)

    # 2. Write summary + append audit trail entry
    total_spend = combined_df["amount_inr"].sum() if not combined_df.empty else 0.0
    total_gst = combined_df["gst_amount_inr"].sum() if not combined_df.empty else 0.0

    summary_path = output_dir / "summary.md"
    header_lines = [
        "# Expense Reconciliation Audit Summary\n",
        f"- **Cumulative Reconciled Spend:** INR {total_spend:,.2f}",
        f"- **Cumulative GST / Tax Recorded:** INR {total_gst:,.2f}",
        f"- **New Matched in Run:** {len(raw_payload.get('matched', []))}",
        f"- **New Flagged in Run:** {len(raw_payload.get('flagged', []))}",
        f"- **New Unmatched Debits in Run:** {len(raw_payload.get('unmatched_debits', []))}",
        f"- **Skipped Duplicate Txns (Idempotent):** {skipped_txns}",
        f"- **New PDFs Seen:** {new_pdfs_count}\n",
        "## Missing Invoices to Chase",
    ]
    if ledger.missing_invoices_to_chase:
        header_lines.extend([f"- {item}" for item in ledger.missing_invoices_to_chase])
    else:
        header_lines.append("- None")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    model_used = os.getenv("LLM_MODEL", "default")
    approval_fingerprint = hashlib.sha256(
        json.dumps([r.model_dump() for r in ledger.rows], sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    audit_entry = (
        f"\n## Audit Trail\n"
        f"- `{timestamp}` | model=`{model_used}` | new_rows=`{len(ledger.rows)}` "
        f"| skipped_txns=`{skipped_txns}` | ledger_sha12=`{approval_fingerprint}`\n"
    )

    existing_audit = ""
    if summary_path.exists():
        content = summary_path.read_text(encoding="utf-8")
        marker = "## Audit Trail"
        if marker in content:
            existing_audit = content.split(marker, 1)[1].lstrip("\n")

    full_summary = "\n".join(header_lines) + "\n" + audit_entry + existing_audit
    summary_path.write_text(full_summary, encoding="utf-8")


def main() -> None:
    load_dotenv()
    base_dir = Path(__file__).resolve().parents[2]
    csv_files = glob.glob(str(base_dir / "data" / "statements" / "*.csv"))
    pdf_dir = base_dir / "data" / "invoices"
    output_dir = base_dir / "output"
    state_path = output_dir / ".recon_state.json"

    if not csv_files:
        raise FileNotFoundError(f"No statement CSVs found in {base_dir / 'data' / 'statements'}")

    state = load_state(state_path)
    full_payload = json.loads(recon_engine.reconcile_batch(csv_files, str(pdf_dir)))
    filtered_payload, skipped_txns, new_pdfs_count = filter_new_payload(full_payload, pdf_dir, state)

    has_new_work = any(
        len(filtered_payload.get(k, [])) > 0
        for k in ("matched", "unmatched_debits", "unmatched_invoices", "flagged", "unreadable_pdfs")
    )

    if not has_new_work:
        print(f"No new transactions or PDFs to reconcile ({skipped_txns} duplicate txns skipped).")
        write_outputs(
            type("EmptyLedger", (), {"rows": [], "missing_invoices_to_chase": []})(),
            filtered_payload,
            output_dir,
            skipped_txns,
            new_pdfs_count,
        )
        return

    ledger = run_reconciliation_crew(json.dumps(filtered_payload))
    write_outputs(ledger, filtered_payload, output_dir, skipped_txns, new_pdfs_count)
    commit_state(state_path, state, filtered_payload, pdf_dir)
    print(f"\nReconciliation complete. State saved to {state_path}")


if __name__ == "__main__":
    main()
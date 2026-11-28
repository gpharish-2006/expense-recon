# Expense Recon — Hybrid Rust + Agentic Expense Reconciliation

A hybrid expense reconciliation pipeline that combines a high-speed deterministic **Rust engine** (`recon_engine` via PyO3 + Maturin) with a multi-agent **CrewAI** workflow for merchant normalization, GST/discrepancy auditing, and human-in-the-loop ledger sign-off.

## Architecture

1. **Input Layer (`data/`)**
   * `data/statements/*.csv` — Bank and UPI statement exports (`date`, `narration`, `debit_amount`).
   * `data/invoices/*.pdf` — Invoices and receipts in PDF format.
2. **Rust Engine (`src/lib.rs` → `recon_engine`)**
   * Reads CSVs and PDFs in parallel using `rayon`, `csv`, and `pdf-extract`.
   * Extracts dates, amounts (stored in integer paise `i64` to avoid floating-point rounding bugs), tax/GST totals, and Indian GSTINs via `regex` and `chrono`.
   * Matches debits to invoices using exact amount and a `±3`-day window, flagging anomalies (`TAX_CHARGED_WITHOUT_GSTIN`, `MULTIPLE_MATCHING_INVOICES`) and returning a JSON payload to Python.
3. **Agentic Workflow (`python/recon_app/`)**
   * **Merchant & Expense Categorizer:** Decodes cryptic payment strings (such as `RAZORPAY*AWS` or `UPI/DR/...`) and maps them to standard accounting categories.
   * **Financial Discrepancy Auditor:** Reviews flagged items, unmatched debits, orphaned invoices, and unreadable PDFs, highlighting GST/ITC/RCM considerations and missing receipts.
   * **Approval & Ledger Reporter:** Pauses (`human_input=True`) for human confirmation before compiling the validated `LedgerOutput` Pydantic schema.
4. **Idempotency, Audit Trail & Output Layer (`output/`)**
   * `output/.recon_state.json` — Tracks SHA-256 hashes of processed transactions and PDFs so re-runs skip duplicates without re-invoking LLMs.
   * `output/reconciled_expenses.csv` — Cumulative categorized expense ledger deduplicated across runs.
   * `output/summary.md` — Spend totals, GST summary, missing invoices to chase, and an append-only run audit log with model name, skipped counts, and ledger fingerprint.


## Project Structure

```text
expense-recon/
├── Cargo.toml                  # Rust dependencies
├── pyproject.toml              # Maturin build configuration
├── requirements.txt            # Python dependencies
├── .env                        # API keys
├── .gitignore
├── README.md
│
├── src/
│   └── lib.rs                  # Full Rust engine 
│
├── python/
│   └── recon_app/
│       ├── __init__.py
|       ├── tasks.py            # Pydantic output schemas & CrewAI task definitions
│       ├── crew.py             # Pydantic schemas, 3 CrewAI agents, tasks, and human approval loop
│       └── main.py             # runs Rust engine -> triggers CrewAI -> writes CSV & summary
│
├── data/
│   ├── statements/             # Input bank/UPI .csv files
│   └── invoices/               # Input .pdf receipts & bills
│
└── output/
    ├── .recon_state.json
    ├── reconciled_expenses.csv # Final categorized ledger
    └── summary.md              # Spend breakdown & missing invoice list
```

## Prerequisites

* **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/)
* **Rust toolchain** (`rustc`, `cargo`) via [`rustup`](https://rustup.rs/) and a C linker (`gcc` on Linux)

## Setup & Installation

1. **Install Python dependencies with `uv`:**

   ```bash
   uv sync
   ```

   *(Or install from `requirements.txt` into your environment: `uv pip install -r requirements.txt`)*

2. **Compile and link the Rust PyO3 extension:**

   ```bash
   uv run maturin develop
   ```

   Use `uv run maturin develop --release` for optimized builds. Re-run `maturin develop` whenever `src/lib.rs` changes.

3. **Configure environment variables:**

   Create a `.env` file in the project root (see `.env.example`) and keep **one** provider active at a time.

   * **Option A — OpenRouter (Recommended for free-tier & multi-model routing):**

     ```env
     LLM_PROVIDER=openrouter
     OPENROUTER_API_KEY=your-key-here
     LLM_MODEL=openrouter/qwen/qwen3-32b
     ```

   * **Option B — OpenAI (Direct):**

     ```env
     LLM_PROVIDER=openai
     OPENAI_API_KEY=your-key-here
     LLM_MODEL=gpt-4o-mini
     ```

   * **Option C — Hugging Face Inference Providers:**

     ```env
     LLM_PROVIDER=huggingface
     HF_TOKEN=your-token-here
     LLM_MODEL=huggingface/meta-llama/Meta-Llama-3.1-8B-Instruct
     ```

     *(Note: Using `openrouter` or `huggingface` in CrewAI uses LiteLLM under the hood—ensure `crewai[litellm]` is installed if `litellm` is missing.)*
     
## Usage

1. Place statement CSV files in `data/statements/` and invoice PDFs in `data/invoices/`.
2. Run the pipeline from the repository root:

   ```bash
   uv run python -m recon_app.main
   ```

3. When the **Approval & Ledger Reporter** prompts for confirmation in the terminal, review the proposed resolutions and enter your approval or adjustments.
4. Inspect `output/reconciled_expenses.csv` and `output/summary.md` once the run finishes.
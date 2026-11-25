import os
from crewai import Agent, Crew, LLM, Process
from recon_app.tasks import LedgerOutput, build_tasks


def _build_llm() -> LLM:
    provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
    model = os.getenv("LLM_MODEL", "").strip()

    if provider == "openai":
        return LLM(
            model=model or "gpt-4o-mini",
            api_key=os.getenv("OPENAI_API_KEY"),
        )

    if provider == "huggingface":
        hf_model = model or "huggingface/meta-llama/Meta-Llama-3.1-8B-Instruct"
        if not hf_model.startswith("huggingface/"):
            hf_model = f"huggingface/{hf_model}"
        return LLM(
            model=hf_model,
            api_key=os.getenv("HF_TOKEN"),
        )

    or_model = model or "openrouter/qwen/qwen3-32b"
    if not or_model.startswith("openrouter/"):
        or_model = f"openrouter/{or_model}"
    return LLM(
        model=or_model,
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )

def run_reconciliation_crew(rust_payload_json: str) -> LedgerOutput:
    llm = _build_llm()

    categorizer = Agent(
        role="Merchant & Expense Categorizer",
        goal="Decode cryptic bank/UPI merchant strings into clean vendor names and expense categories.",
        backstory=(
            "You specialize in normalizing Indian bank and UPI narrations (such as RAZORPAY*AWS, "
            "UPI/DR/SWIGGY, or NEFT transfers) into clean vendor names and standard ledger categories."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    auditor = Agent(
        role="Financial Discrepancy Auditor",
        goal="Investigate flagged transactions, GSTIN/tax mismatches, and duplicate candidates.",
        backstory=(
            "You are a detail-oriented GST and expense auditor who resolves edge-case ledger "
            "anomalies and flags missing invoices for follow-up."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    reporter = Agent(
        role="Approval & Ledger Reporter",
        goal="Obtain human confirmation on flagged anomalies and compile the final reconciled ledger.",
        backstory=(
            "You consolidate deterministic matches and agent-resolved edge cases into a clean, "
            "tax-ready expense table."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    tasks = build_tasks(categorizer, auditor, reporter, rust_payload_json)

    crew = Crew(
        agents=[categorizer, auditor, reporter],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    return result.pydantic
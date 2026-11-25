use chrono::NaiveDate;
use pyo3::prelude::*;
use rayon::prelude::*;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct BankTxn {
    pub id: String,
    pub date: String,
    pub raw_narration: String,
    pub amount_paise: i64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ParsedInvoice {
    pub file_name: String,
    pub date: Option<String>,
    pub vendor_hint: Option<String>,
    pub gstin: Option<String>,
    pub total_paise: Option<i64>,
    pub tax_paise: i64,
    pub extraction_ok: bool,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct MatchedItem {
    pub txn: BankTxn,
    pub invoice_file: String,
    pub gstin: Option<String>,
    pub tax_paise: i64,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct FlaggedItem {
    pub txn: BankTxn,
    pub reason: String,
    pub candidate_invoices: Vec<String>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct ReconPayload {
    pub matched: Vec<MatchedItem>,
    pub unmatched_debits: Vec<BankTxn>,
    pub unmatched_invoices: Vec<ParsedInvoice>,
    pub flagged: Vec<FlaggedItem>,
    pub unreadable_pdfs: Vec<String>,
}

fn parse_bank_csv(path: &Path) -> Result<Vec<BankTxn>, String> {
    let mut rdr = csv::Reader::from_path(path).map_err(|e| e.to_string())?;
    let mut txns = Vec::new();
    let stem = path.file_stem().unwrap_or_default().to_string_lossy();

    for (idx, result) in rdr.records().enumerate() {
        let record = result.map_err(|e| e.to_string())?;
        // Expected CSV columns: date (YYYY-MM-DD), narration, debit_amount
        let date = record.get(0).unwrap_or("").trim().to_string();
        let narration = record.get(1).unwrap_or("").trim().to_string();
        let amount_str = record.get(2).unwrap_or("0").trim().replace(',', "");

        if let Ok(amount_f64) = amount_str.parse::<f64>() {
            if amount_f64 > 0.0 {
                txns.push(BankTxn {
                    id: format!("{}-{}", stem, idx + 1),
                    date,
                    raw_narration: narration,
                    amount_paise: (amount_f64 * 100.0).round() as i64,
                });
            }
        }
    }
    Ok(txns)
}

fn parse_invoice_pdf(path: &Path) -> ParsedInvoice {
    let file_name = path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();

    let text = match pdf_extract::extract_text(path) {
        Ok(t) if t.trim().len() >= 20 => t,
        _ => {
            return ParsedInvoice {
                file_name,
                date: None,
                vendor_hint: None,
                gstin: None,
                total_paise: None,
                tax_paise: 0,
                extraction_ok: false,
            };
        }
    };

    let gstin_re = Regex::new(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}\b").unwrap();
    let date_re = Regex::new(r"\b(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b").unwrap();
    let total_re =
        Regex::new(r"(?i)(?:total|grand total|amount payable)[^\d]{0,15}([\d,]+\.\d{2})").unwrap();
    let tax_re = Regex::new(r"(?i)(?:gst|cgst|sgst|igst|tax)[^\d]{0,15}([\d,]+\.\d{2})").unwrap();

    let gstin = gstin_re.find(&text).map(|m| m.as_str().to_string());
    let date = date_re.find(&text).map(|m| m.as_str().to_string());

    let total_paise = total_re
        .captures(&text)
        .and_then(|c| c.get(1))
        .and_then(|m| m.as_str().replace(',', "").parse::<f64>().ok())
        .map(|v| (v * 100.0).round() as i64);

    let tax_paise = tax_re
        .captures_iter(&text)
        .filter_map(|c| c.get(1))
        .filter_map(|m| m.as_str().replace(',', "").parse::<f64>().ok())
        .map(|v| (v * 100.0).round() as i64)
        .sum();

    ParsedInvoice {
        file_name,
        date,
        vendor_hint: text
            .lines()
            .find(|l| !l.trim().is_empty())
            .map(|s| s.trim().to_string()),
        gstin,
        total_paise,
        tax_paise,
        extraction_ok: true,
    }
}

fn parse_flexible_date(s: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(s, "%Y-%m-%d")
        .or_else(|_| NaiveDate::parse_from_str(s, "%d/%m/%Y"))
        .ok()
}

fn reconcile(txns: Vec<BankTxn>, invoices: Vec<ParsedInvoice>) -> ReconPayload {
    let mut matched = Vec::new();
    let mut unmatched_debits = Vec::new();
    let mut flagged = Vec::new();
    let mut used_invoices = HashSet::new();

    let unreadable_pdfs: Vec<String> = invoices
        .iter()
        .filter(|inv| !inv.extraction_ok)
        .map(|inv| inv.file_name.clone())
        .collect();

    for txn in txns {
        let txn_date = parse_flexible_date(&txn.date);

        let candidates: Vec<(usize, &ParsedInvoice)> = invoices
            .iter()
            .enumerate()
            .filter(|(idx, inv)| {
                !used_invoices.contains(idx)
                    && inv.extraction_ok
                    && inv.total_paise == Some(txn.amount_paise)
                    && match (txn_date, inv.date.as_deref().and_then(parse_flexible_date)) {
                        (Some(td), Some(id)) => (td - id).num_days().abs() <= 3,
                        _ => true,
                    }
            })
            .collect();

        match candidates.len() {
            0 => unmatched_debits.push(txn),
            1 => {
                let (idx, inv) = candidates[0];
                used_invoices.insert(idx);

                if inv.gstin.is_none() && inv.tax_paise > 0 {
                    flagged.push(FlaggedItem {
                        txn,
                        reason: "TAX_CHARGED_WITHOUT_GSTIN".to_string(),
                        candidate_invoices: vec![inv.file_name.clone()],
                    });
                } else {
                    matched.push(MatchedItem {
                        txn,
                        invoice_file: inv.file_name.clone(),
                        gstin: inv.gstin.clone(),
                        tax_paise: inv.tax_paise,
                    });
                }
            }
            _ => {
                flagged.push(FlaggedItem {
                    txn,
                    reason: "MULTIPLE_MATCHING_INVOICES".to_string(),
                    candidate_invoices: candidates
                        .iter()
                        .map(|(_, c)| c.file_name.clone())
                        .collect(),
                });
            }
        }
    }
    let unmatched_invoices: Vec<ParsedInvoice> = invoices
        .into_iter()
        .enumerate()
        .filter(|(idx, inv)| inv.extraction_ok && !used_invoices.contains(idx))
        .map(|(_, inv)| inv)
        .collect();

    ReconPayload {
        matched,
        unmatched_debits,
        unmatched_invoices,
        flagged,
        unreadable_pdfs,
    }
}

#[pyfunction]
fn reconcile_batch(csv_paths: Vec<String>, pdf_dir: String) -> PyResult<String> {
    let txns: Vec<BankTxn> = csv_paths
        .par_iter()
        .flat_map(|p| parse_bank_csv(&PathBuf::from(p)).unwrap_or_default())
        .collect();

    let pdf_files: Vec<PathBuf> = WalkDir::new(pdf_dir)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| {
            e.file_type().is_file()
                && e.path().extension().and_then(|s| s.to_str())
                    .is_some_and(|ext| ext.eq_ignore_ascii_case("pdf"))})
        .map(|e| e.into_path())
        .collect();


    let invoices: Vec<ParsedInvoice> = pdf_files
        .par_iter()
        .map(|path| parse_invoice_pdf(path))
        .collect();

    let payload = reconcile(txns, invoices);

    serde_json::to_string(&payload)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

#[pymodule]
fn recon_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(reconcile_batch, m)?)?;
    Ok(())
}
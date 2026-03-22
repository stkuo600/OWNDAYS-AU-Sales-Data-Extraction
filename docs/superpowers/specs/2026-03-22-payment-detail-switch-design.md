# Switch to Payment Detail by Payment Type Report — Design Spec

**Date:** 2026-03-22
**Goal:** Replace Banking Transaction Report extraction with Payment Detail by Payment Type report to capture per-transaction product category breakdown (Consult, Frame, Lens, CL, Sundry, Misc).

---

## 1. Claude Parser (`src/claude_parser.py`)

### Prompt Changes

- Target **Payment Detail by Payment Type** PDF (columns: Date, Ref-No, Name, Tax Paid, Payment Inc Tax, Consult, Frame, Lens, CL, Sundry, Misc)
- Ignore Banking Transaction Report and other PDFs (BulkBillingSummaryReport, DailyTallyReport, ScannedDocument, etc.)
- Drop `banking_no` from extraction

### Per-Transaction Fields

```
receipt_no        — Ref-No column (string, empty for DDEP/Medicare rows)
payment_method    — Payment Type section header (e.g. "MASTER", "HC", "DDEP", "VISA", "EFTPOS", "CASH", "zMP")
customer_name     — Name column, name portion only (string)
customer_id       — Number after dash in Name column (string, empty if none)
amount_inc_tax    — Payment (Inc Tax)$ column (numeric)
tax               — Tax Paid column (numeric)
amount_exc_tax    — Derived: amount_inc_tax - tax (numeric)
consult           — Consult $ column (numeric)
frame             — Frame $ column (numeric)
lens              — Lens $ column (numeric)
cl                — CL $ column (numeric)
sundry            — Sundry $ column (numeric)
misc              — Misc $ column (numeric)
```

**Important:** The `payment_method` is NOT a column in each row — it's the section header (e.g. "Payment Type: MASTER"). All rows under that header share the same payment method.

**Customer name/ID parsing:** Format is `"Firstname Lastname-ID"` (e.g. `"Joanne Poscai-52010"`). Split on the last dash to get name vs ID. Medicare rows have no ID.

### Summary-Level Fields

From email body (unchanged):
- `report_date`, `store_name`, `target_exc_gst`, `consultation`, `no_customers`, `daily_comment`, `customer_feedback`

From PDF grand totals (last row of last page):
- `total_inc_gst`, `total_tax`, `total_exc_gst` (derived)
- `total_consult`, `total_frame`, `total_lens`, `total_cl`, `total_sundry`, `total_misc`
- `transaction_count` — count of individual transaction rows (exclude subtotal/total rows)

---

## 2. Database Schema Changes

### Fact_EOD_Transaction — Add 6 Columns

```sql
ALTER TABLE ownd.Fact_EOD_Transaction ADD consult DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Transaction ADD frame DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Transaction ADD lens DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Transaction ADD cl DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Transaction ADD sundry DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Transaction ADD misc DECIMAL(10,2) NULL;
```

### Fact_EOD_Summary — Drop banking_no, Add 6 Columns

```sql
ALTER TABLE ownd.Fact_EOD_Summary DROP COLUMN banking_no;
ALTER TABLE ownd.Fact_EOD_Summary ADD total_consult DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Summary ADD total_frame DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Summary ADD total_lens DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Summary ADD total_cl DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Summary ADD total_sundry DECIMAL(10,2) NULL;
ALTER TABLE ownd.Fact_EOD_Summary ADD total_misc DECIMAL(10,2) NULL;
```

---

## 3. Fabric Writer (`src/fabric_writer.py`)

### `_insert_summary` Changes

- Remove `banking_no` from INSERT
- Add `total_consult`, `total_frame`, `total_lens`, `total_cl`, `total_sundry`, `total_misc`

### `_insert_transactions` Changes

- Add `consult`, `frame`, `lens`, `cl`, `sundry`, `misc` to INSERT

---

## 4. Existing Data

Today's 3 records (2026-03-20) were extracted from the Banking Transaction Report. They will have NULL values for the new category columns. This is fine — the columns are nullable. Future runs will populate them.

---

## 5. Files Modified

- `src/claude_parser.py` — new extraction prompt
- `src/fabric_writer.py` — updated INSERT statements
- Database — ALTER TABLE migrations (run manually or via script)

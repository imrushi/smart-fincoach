"""SBI (State Bank of India) Bank statement parser — supports PDF (text-based), Excel and CSV."""
import re
import io
from decimal import Decimal

import pandas as pd

from app.parsers.base import BaseParser, ParseResult, ParsedTransaction
from app.models.models import SourceType

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


class SBIBankParser(BaseParser):
    SOURCE = SourceType.SBI_BANK

    COL_MAP = {
        "date": ["date", "transaction date", "txn date", "value date", "posting date"],
        "narration": ["narration", "particulars", "description", "transaction description", "remarks"],
        "debit": ["debit", "withdrawal", "debit amount", "debit amt", "withdrawalamt"],
        "credit": ["credit", "deposit", "credit amount", "credit amt", "depositamt"],
        "balance": ["closing balance", "balance", "running balance", "closingbalance", "balance (inr)"],
        "cheque": ["chq./ref.no.", "cheque no", "ref number", "chq no", "reference number"],
    }

    # SBI text line pattern: DATE  NARRATION...  AMOUNT  BALANCE
    TXN_LINE_RE = re.compile(
        r'^\s*(\d{2}-\d{2}-\d{4}|\d{2}/\d{2}/\d{4}|\d{2}/\d{2}/\d{2})\s+'  # Date
        r'(.+?)\s+'                                                          # Narration
        r'([\d,]+\.\d{2})?\s*'                                               # Amount
        r'([\d,]+\.\d{2})\s*$'                                               # Balance
    )

    def parse(self, file_bytes: bytes, filename: str) -> ParseResult:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext == "pdf":
            return self._parse_pdf(file_bytes)
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
            return self._parse_df(df)
        elif ext == "csv":
            return self._parse_csv(file_bytes)
        return ParseResult(errors=[f"Unsupported: {ext}"], source_type=self.SOURCE)

    def _parse_pdf(self, file_bytes: bytes) -> ParseResult:
        if pdfplumber is None:
            return ParseResult(errors=["pdfplumber not installed"], source_type=self.SOURCE)

        result = ParseResult(source_type=self.SOURCE)

        all_lines = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    all_lines.extend(text.split("\n"))

        # Try regex-based parsing first
        txn_indices = []
        for i, line in enumerate(all_lines):
            m = self.TXN_LINE_RE.match(line)
            if m:
                txn_indices.append((i, m))

        if txn_indices:
            result = self._process_regex_matches(all_lines, txn_indices)
        else:
            # Fallback: heuristic-based parsing
            result = self._parse_sbi_text_fallback(all_lines)

        result.source_type = self.SOURCE
        return result

    def _process_regex_matches(self, all_lines, txn_indices):
        result = ParseResult(source_type=self.SOURCE)
        for idx, (line_i, match) in enumerate(txn_indices):
            try:
                date_str = match.group(1)
                narration_start = match.group(2)
                amount_str = match.group(3)
                balance_str = match.group(4)

                txn_date = self.parse_indian_date(date_str)
                if not txn_date:
                    continue

                # Collect continuation narration lines
                narration_parts = [narration_start]
                next_line_i = txn_indices[idx + 1][0] if idx + 1 < len(txn_indices) else len(all_lines)
                for j in range(line_i + 1, next_line_i):
                    l = all_lines[j].strip()
                    if not l or self._is_noise(l):
                        continue
                    if re.match(r'^\d{2}[-/]\d{2}[-/]\d{4}', l):
                        break
                    narration_parts.append(l)

                narration = " ".join(narration_parts).strip()

                amount = self.clean_amount(amount_str) if amount_str else Decimal("0")
                if amount == 0:
                    continue

                # Detect if debit or credit from narration keywords
                is_debit = True
                credit_keywords = ["salary", "credit", "deposit", "refund", "cashback", "reversal", "interest"]
                if any(kw in narration.lower() for kw in credit_keywords):
                    is_debit = False

                balance = self.clean_amount(balance_str)
                utr = self.extract_utr(narration)
                upi_id = self.extract_upi_id(narration)
                counterparty = self._extract_sbi_counterparty(narration)

                result.transactions.append(ParsedTransaction(
                    txn_date=txn_date, amount=amount, is_debit=is_debit,
                    balance_after=balance if balance > 0 else None,
                    raw_narration=narration,
                    utr=utr, counterparty_name=counterparty,
                    counterparty_upi_id=upi_id, source_type=self.SOURCE,
                ))
            except Exception as e:
                result.errors.append(f"Line {line_i}: {e}")

        result.row_count = len(result.transactions)
        return result

    def _parse_sbi_text_fallback(self, all_lines):
        """Fallback parser for SBI PDFs with flexible formatting."""
        result = ParseResult(source_type=self.SOURCE)

        # Pattern: date at start of line
        date_re = re.compile(r'^(\d{2}[-/]\d{2}[-/]\d{4}|\d{2}[-/]\d{2}[-/]\d{2})\s+(.+)')
        amount_re = re.compile(r'([\d,]+\.\d{2})')

        i = 0
        while i < len(all_lines):
            line = all_lines[i].strip()
            m = date_re.match(line)
            if not m:
                i += 1
                continue

            date_str = m.group(1)
            txn_date = self.parse_indian_date(date_str)
            if not txn_date:
                i += 1
                continue

            rest = m.group(2)

            # Collect all text until next date line
            narration_parts = [rest]
            j = i + 1
            while j < len(all_lines):
                next_line = all_lines[j].strip()
                if date_re.match(next_line):
                    break
                if next_line and not self._is_noise(next_line):
                    narration_parts.append(next_line)
                j += 1

            full_text = " ".join(narration_parts)

            # Extract amounts from the full text - last amount is balance
            amounts = amount_re.findall(full_text)
            if len(amounts) < 2:
                i = j
                continue

            # Remove amounts from narration to get clean text
            narration = full_text
            for a in amounts:
                narration = narration.replace(a, "", 1)
            narration = re.sub(r'\s+', ' ', narration).strip()

            # Last amount is always balance
            balance = self.clean_amount(amounts[-1])

            # Amount is second-to-last
            amount = self.clean_amount(amounts[-2]) if len(amounts) >= 2 else Decimal("0")
            if amount == 0:
                i = j
                continue

            # Detect if debit or credit
            is_debit = True
            credit_keywords = ["salary", "credit", "deposit", "refund", "cashback", "reversal", "interest"]
            if any(kw in narration.lower() for kw in credit_keywords):
                is_debit = False

            utr = self.extract_utr(narration)
            upi_id = self.extract_upi_id(narration)
            counterparty = self._extract_sbi_counterparty(narration)

            result.transactions.append(ParsedTransaction(
                txn_date=txn_date, amount=amount, is_debit=is_debit,
                balance_after=balance if balance > 0 else None,
                raw_narration=narration,
                utr=utr, counterparty_name=counterparty,
                counterparty_upi_id=upi_id, source_type=self.SOURCE,
            ))

            i = j

        result.row_count = len(result.transactions)
        return result

    def _is_noise(self, line):
        l = line.lower().strip()
        if not l:
            return True
        if re.match(r'^page', l):
            return True
        noise = ["account", "branch", "address", "city", "state", "phone",
                 "email", "statement", "period", "from", "to", "ifsc",
                 "closing balance:", "opening balance:"]
        return any(n in l for n in noise)

    def _extract_sbi_counterparty(self, narration):
        # SBI UPI format: /UPI/NAME/VPA or variations
        m = re.search(r'(?:/upi/|upi-|upi\s+)([a-z0-9]+@[a-z]{2,})', narration, re.IGNORECASE)
        if m:
            return m.group(1).split("@")[0].strip().title()
        
        # Try to extract first meaningful word/name
        words = narration.split()
        for word in words:
            if len(word) > 2 and word[0].isalpha() and "@" not in word:
                return word.strip().title()
        return None

    def _resolve_cols(self, columns):
        result = {}
        for key, variants in self.COL_MAP.items():
            for col in columns:
                if any(v in col.lower() for v in variants):
                    result[key] = col
                    break
        return result

    def _parse_csv(self, file_bytes):
        for skip in range(0, 11):
            try:
                df = pd.read_csv(io.BytesIO(file_bytes), skiprows=skip)
                if any("date" in str(c).lower() for c in df.columns):
                    return self._parse_df(df)
            except Exception:
                continue
        return ParseResult(errors=["Could not find header row"], source_type=self.SOURCE)

    def _parse_df(self, df):
        result = ParseResult(source_type=self.SOURCE)
        df.columns = [str(c).strip() for c in df.columns]
        cols = self._resolve_cols(df.columns.tolist())
        if not cols.get("date"):
            result.errors.append(f"No date column in {df.columns.tolist()}")
            return result

        for _, row in df.iterrows():
            try:
                date_val = str(row[cols["date"]]).strip()
                txn_date = self.parse_indian_date(date_val)
                if not txn_date:
                    continue
                
                narration = str(row.get(cols.get("narration", ""), ""))
                debit_str = str(row.get(cols.get("debit", ""), ""))
                credit_str = str(row.get(cols.get("credit", ""), ""))
                balance_str = str(row.get(cols.get("balance", ""), ""))

                debit = self.clean_amount(debit_str) if debit_str not in ("", "nan", "0") else Decimal("0")
                credit = self.clean_amount(credit_str) if credit_str not in ("", "nan", "0") else Decimal("0")
                
                # Determine is_debit
                is_debit = debit > 0
                if debit == 0 and credit == 0:
                    continue
                
                amount = debit if is_debit else credit
                if amount == 0:
                    continue

                result.transactions.append(ParsedTransaction(
                    txn_date=txn_date, amount=amount, is_debit=is_debit,
                    balance_after=self.clean_amount(balance_str) if balance_str not in ("", "nan") else None,
                    raw_narration=narration, utr=self.extract_utr(narration),
                    counterparty_upi_id=self.extract_upi_id(narration),
                    counterparty_name=self._extract_sbi_counterparty(narration),
                    source_type=self.SOURCE,
                ))
            except Exception as e:
                result.errors.append(str(e))

        result.row_count = len(result.transactions)
        return result

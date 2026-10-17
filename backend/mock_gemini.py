"""Offline stand-in for Gemini, used ONLY for local tests (MOCK_GEMINI=1).

Production always calls real Gemini. Here, 'LLM answers' come from the synthetic
generator's ground truth, and the 'compiler' returns hand-written reference code,
so the gateway, sandbox, evaluation and UI can be tested without an API key.
"""
import random

import tasks

TRUTH = {}


def _wrap(fn):
    def inner(r):
        text, truth = fn(r)
        TRUTH[text] = truth
        return text, truth
    return inner


for _k in list(tasks.GENERATORS):
    tasks.GENERATORS[_k] = _wrap(tasks.GENERATORS[_k])

REF_INVOICE = r'''
import re, datetime
MON = {m: i + 1 for i, m in enumerate(["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"])}
CUR = [("S$", "SGD"), ("A$", "AUD"), ("SGD", "SGD"), ("AUD", "AUD"), ("INR", "INR"), ("Rs.", "INR"), ("₹", "INR"),
       ("EUR", "EUR"), ("€", "EUR"), ("GBP", "GBP"), ("£", "GBP"), ("USD", "USD"), ("$", "USD")]

def _amount(s, cur):
    s = s.strip()
    if cur == "EUR":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return round(float(s), 2)

def run(text):
    m = re.search(r"(Grand Total|TOTAL DUE|Total Amount|Amount Payable|Invoice Total)\s*:?\s*(S\$|A\$|Rs\.|[A-Z]{3}|[₹€£$])\s?([\d.,]+)", text, re.I)
    if not m:
        return None
    sym = m.group(2)
    cur = next((c for s, c in CUR if sym == s), None)
    if not cur:
        return None
    total = _amount(m.group(3), cur)
    inv = re.search(r"(?:Invoice No:|INVOICE #|Invoice Number -|Doc:)\s*(\S+)", text)
    tax = re.search(r"\b(?:GSTIN|GST No|USt-IdNr\.|VAT ID|VAT Reg No|VAT No\.|GST Reg No|GST Registration No\.|ABN|EIN|Tax ID)\s*[:\-]?\s*([A-Z0-9][A-Z0-9 \-]*[A-Z0-9])", text)
    d = re.search(r"(?:Date:|Issued|Invoice Date -|Dated:)\s*(.+)", text)
    if not (inv and tax and d):
        return None
    ds = d.group(1).strip()
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", ds):
            dt = datetime.date.fromisoformat(ds[:10])
        elif re.match(r"\d{2}/\d{2}/\d{4}", ds):
            a, b, y = ds[:10].split("/")
            dt = datetime.date(int(y), int(a), int(b)) if cur == "USD" else datetime.date(int(y), int(b), int(a))
        elif re.match(r"\d{2}\.\d{2}\.\d{4}", ds):
            a, b, y = ds[:10].split(".")
            dt = datetime.date(int(y), int(b), int(a))
        else:
            mm = re.match(r"(\d{1,2})[.\- ]+\s*([A-Za-z]{3})[\- ](\d{4})", ds)
            if mm:
                dt = datetime.date(int(mm.group(3)), MON[mm.group(2).lower()], int(mm.group(1)))
            else:
                mm = re.match(r"([A-Za-z]+) (\d{1,2}), (\d{4})", ds)
                if not mm:
                    return None
                dt = datetime.date(int(mm.group(3)), MON[mm.group(1)[:3].lower()], int(mm.group(2)))
    except Exception:
        return None
    return {"invoice_number": inv.group(1), "invoice_date": dt.isoformat(),
            "tax_id": tax.group(1).replace(" ", ""), "total_amount": total, "currency": cur}
'''

REF_MATERIAL = r'''
import re
RULES = [
    ("BEARING", "EA", r"bearing|bush"),
    ("LUBRICANT", "L", r"\boil\b|grease|coolant"),
    ("PPE", "PR", r"gloves|shoes|ear plugs"),
    ("PPE", "EA", r"helmet|goggles|vest"),
    ("ELECTRICAL", "M", r"wire|cable"),
    ("ELECTRICAL", "EA", r"mcb|contactor|lug"),
    ("FASTENER", "EA", r"bolt|nut|washer|screw|rivet|anchor"),
    ("RAW_METAL", "KG", r"sheet|angle|plate|bar|pipe|rod"),
]
def run(text):
    t = text.lower()
    for cat, uom, pat in RULES:
        if re.search(pat, t):
            return {"category": cat, "base_uom": uom}
    return None
'''


def mock_generate(prompt):
    usage = {"tokens_in": len(prompt) // 4, "tokens_out": 60, "latency_ms": 5, "model": "mock"}
    usage["cost_usd"] = usage["tokens_in"] / 1e6 * 0.75 + 60 / 1e6 * 3.75
    if "Decide whether a deterministic" in prompt:
        comp = "complaint" not in prompt.lower()
        return {"compilable": comp, "confidence": 0.9, "pattern": "mock pattern", "strategy": "mock",
                "risks": "mock"}, usage
    if "Write a Python function" in prompt or "has problems. Fix them" in prompt:
        code = REF_INVOICE if "Invoice" in prompt else REF_MATERIAL
        return {"code": code, "notes": "mock"}, usage
    for marker in ("INVOICE:\n", "ITEM: ", "COMPLAINT:\n"):
        if marker in prompt:
            text = prompt.split(marker, 1)[1]
            if marker == "COMPLAINT:\n":
                return {"reply": "We're sorry - we'll fix this today. " + str(random.random())}, usage
            return TRUTH.get(text, {}), usage
    return {}, usage

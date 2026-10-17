"""Demo workloads that a real company sends to an LLM every day.

Each task has:
  - a prompt template (what the app sends to Gemini today)
  - an output schema (fields the app expects back)
  - a synthetic traffic generator, used to simulate production traffic

Two of the tasks are repetitive and rule-like (Fold should compile them).
One is genuinely creative (Fold should refuse to compile it and keep Gemini).
"""
import random
from datetime import date, timedelta

TASKS = {
    "invoice_extract": {
        "name": "Invoice field extraction",
        "description": "Pull invoice number, date, seller tax ID, grand total and currency "
                       "from supplier invoices in 6 countries (IN, DE, GB, SG, AU, US).",
        "schema": {
            "invoice_number": "string",
            "invoice_date": "string (YYYY-MM-DD)",
            "tax_id": "string (seller tax registration number, all spaces removed)",
            "total_amount": "number (grand total incl. tax)",
            "currency": "string (ISO 4217 code, e.g. INR, EUR)",
        },
        "prompt": (
            "You are an accounts-payable assistant. Extract fields from the supplier invoice below.\n"
            "Return ONLY a JSON object with keys: invoice_number (string), invoice_date (YYYY-MM-DD), "
            "tax_id (the SELLER's tax registration number with all spaces removed, no label), "
            "total_amount (the grand total including tax, as a plain number), "
            "currency (ISO 4217 code).\n"
            "Interpret dates using the invoice's country conventions (US invoices use MM/DD/YYYY; "
            "all other countries use DD/MM/YYYY or DD.MM.YYYY).\n\nINVOICE:\n{input}"
        ),
    },
    "material_normalize": {
        "name": "Material master normalisation",
        "description": "Map messy purchase-request item descriptions to a standard material "
                       "category and base unit of measure (ERP master-data hygiene).",
        "schema": {
            "category": "one of RAW_METAL, FASTENER, LUBRICANT, ELECTRICAL, BEARING, PPE",
            "base_uom": "one of KG, EA, L, M, PR",
        },
        "prompt": (
            "You are an ERP master-data steward. Classify the purchase item description below.\n"
            "Return ONLY JSON with keys:\n"
            "  category: one of RAW_METAL (sheets, plates, angles, bars, pipes of steel/aluminium/copper/brass), "
            "FASTENER (bolts, nuts, screws, washers, rivets, anchors), LUBRICANT (oils, greases, coolants), "
            "ELECTRICAL (cables, wires, MCBs, contactors, lugs), BEARING (ball/roller bearings, bushes), "
            "PPE (gloves, helmets, goggles, safety shoes, ear plugs)\n"
            "  base_uom: RAW_METAL->KG, FASTENER->EA, LUBRICANT->L, ELECTRICAL cables/wires->M and other "
            "ELECTRICAL items->EA, BEARING->EA, PPE gloves/safety shoes/ear plugs->PR and other PPE->EA\n\n"
            "ITEM: {input}"
        ),
    },
    "complaint_reply": {
        "name": "Customer complaint reply",
        "description": "Draft an empathetic, specific reply to a customer complaint. "
                       "Creative work: should stay on Gemini.",
        "schema": {"reply": "string (2-4 sentence personalised reply)"},
        "prompt": (
            "You are a warm, professional customer-care agent. Write a 2-4 sentence reply to the "
            "complaint below that acknowledges the specific issue and proposes a concrete next step.\n"
            "Return ONLY JSON: {{\"reply\": \"...\"}}\n\nCOMPLAINT:\n{input}"
        ),
    },
}

# ---------------------------------------------------------------- invoices
_SELLERS = {
    "IN": ["Shakti Forgings Pvt Ltd", "Narmada Polymers LLP", "Kaveri Electricals", "Om Sai Castings"],
    "DE": ["Müller Antriebstechnik GmbH", "Rheinland Stahl AG", "Becker Logistik GmbH"],
    "GB": ["Thames Precision Ltd", "Pennine Packaging Ltd", "Highland Fasteners plc"],
    "SG": ["Lion City Components Pte Ltd", "Marina Tech Supplies Pte Ltd"],
    "AU": ["Southern Cross Metals Pty Ltd", "Koala Industrial Pty Ltd"],
    "US": ["Great Lakes Tooling Inc", "Cascade Hydraulics LLC", "Lone Star Wire Co"],
}
_CUR = {"IN": "INR", "DE": "EUR", "GB": "GBP", "SG": "SGD", "AU": "AUD", "US": "USD"}
_SYM = {"IN": ["₹", "Rs.", "INR"], "DE": ["€", "EUR"], "GB": ["£", "GBP"], "SG": ["S$", "SGD"],
        "AU": ["A$", "AUD"], "US": ["$", "USD"]}
_TAXLABEL = {"IN": ["GSTIN", "GST No"], "DE": ["USt-IdNr.", "VAT ID"], "GB": ["VAT Reg No", "VAT No."],
             "SG": ["GST Reg No", "GST Registration No."], "AU": ["ABN"], "US": ["EIN", "Tax ID"]}
_STATES = ["27", "29", "07", "33", "24", "09"]
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
_L = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _taxid(c, r):
    if c == "IN":
        pan = "".join(r.choice(_L) for _ in range(5)) + f"{r.randint(1000, 9999)}" + r.choice(_L)
        tid = f"{r.choice(_STATES)}{pan}{r.randint(1, 9)}Z{r.choice('0123456789' + _L)}"
        return tid, tid
    if c == "DE":
        t = f"DE{r.randint(100000000, 999999999)}"
        return t, t
    if c == "GB":
        n = f"{r.randint(100, 999)} {r.randint(1000, 9999)} {r.randint(10, 99)}"
        return f"GB {n}", "GB" + n.replace(" ", "")
    if c == "SG":
        t = f"{r.randint(199000000, 202599999)}{r.choice(_L)}"
        return t, t
    if c == "AU":
        n = f"{r.randint(10, 99)} {r.randint(100, 999)} {r.randint(100, 999)} {r.randint(100, 999)}"
        return n, n.replace(" ", "")
    t = f"{r.randint(10, 99)}-{r.randint(1000000, 9999999)}"
    return t, t


def _indian(n):
    s = f"{n:.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return f"{whole}.{frac}"


def _money(c, n):
    if c == "IN":
        return _indian(n)
    if c == "DE":
        return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{n:,.2f}"


def _fmt_date(c, d, r):
    if c == "US":
        opts = [d.strftime("%m/%d/%Y"), f"{_MONTH[d.month-1]} {d.day}, {d.year}", d.isoformat()]
    elif c == "DE":
        opts = [d.strftime("%d.%m.%Y"), f"{d.day:02d}. {_MON[d.month-1]} {d.year}", d.isoformat()]
    else:
        opts = [d.strftime("%d/%m/%Y"), f"{d.day:02d} {_MON[d.month-1]} {d.year}",
                f"{d.day:02d}-{_MON[d.month-1]}-{d.year}", d.isoformat()]
    return r.choice(opts)


def gen_invoice(r):
    c = r.choice(list(_CUR))
    seller = r.choice(_SELLERS[c])
    shown_tax, tax = _taxid(c, r)
    d = date(2026, 1, 1) + timedelta(days=r.randint(0, 280))
    inv = r.choice(["INV", "SI", "TX", "BILL", "RE", "FAC"]) + r.choice(["-", "/", ""]) + \
        (f"{d.year}-" if r.random() < .5 else "") + str(r.randint(100, 99999)).zfill(5)
    items = []
    sub = 0.0
    for _ in range(r.randint(1, 4)):
        qty = r.randint(1, 500)
        price = round(r.uniform(2, 900), 2)
        amt = round(qty * price, 2)
        sub += amt
        items.append((r.choice(["Steel flange DN50", "Hex bolt M12", "Copper lug 25sq", "Gear oil 20L",
                                "Bearing 6205", "PVC conduit 25mm", "Nitrile gloves", "Packing tape"]),
                      qty, price, amt))
    sub = round(sub, 2)
    rate = {"IN": 0.18, "DE": 0.19, "GB": 0.20, "SG": 0.09, "AU": 0.10, "US": 0.07}[c]
    tax_amt = round(sub * rate, 2)
    total = round(sub + tax_amt, 2)
    sym = r.choice(_SYM[c])
    label = r.choice(_TAXLABEL[c])
    m = lambda v: (f"{sym} {_money(c, v)}" if r.random() < .5 else f"{sym}{_money(c, v)}")
    lines = "\n".join(f"  {n:<20} {q:>4} x {_money(c, p):>10} = {_money(c, a):>12}" for n, q, p, a in items)
    tpl = r.randint(0, 3)
    ds = _fmt_date(c, d, r)
    total_label = r.choice(["Grand Total", "TOTAL DUE", "Total Amount", "Amount Payable", "Invoice Total"])
    if tpl == 0:
        txt = (f"{seller}\n{label}: {shown_tax}\nTAX INVOICE\nInvoice No: {inv}\nDate: {ds}\n\n{lines}\n\n"
               f"Subtotal: {m(sub)}\nTax ({int(rate*100)}%): {m(tax_amt)}\n{total_label}: {m(total)}")
    elif tpl == 1:
        txt = (f"INVOICE #{inv}\nIssued {ds}\nFrom: {seller} ({label} {shown_tax})\n"
               f"Bill to: Origin Manufacturing Co.\n{lines}\nNet {m(sub)} | Tax {m(tax_amt)} | {total_label} {m(total)}")
    elif tpl == 2:
        txt = (f"{seller.upper()}\nInvoice Date - {ds}\nInvoice Number - {inv}\n{label} - {shown_tax}\n"
               f"Description / Qty / Rate / Amount\n{lines}\nSub-total {m(sub)}\nTax {m(tax_amt)}\n"
               f"{total_label.upper()} {m(total)}\nThank you for your business.")
    else:
        txt = (f"Supplier: {seller}\nSupplier {label}: {shown_tax}\nDoc: {inv}    Dated: {ds}\n{lines}\n"
               f"Taxable value: {m(sub)}\nTax: {m(tax_amt)}\n{total_label}: {m(total)}\nPayment terms: 30 days")
    truth = {"invoice_number": inv, "invoice_date": d.isoformat(), "tax_id": tax,
             "total_amount": total, "currency": _CUR[c]}
    return txt, truth


# ---------------------------------------------------------------- materials
_MAT = {
    "RAW_METAL": (["SS304 sheet {a}mm 1250x2500", "MS angle {a}x{a}x6 6m", "MS plate {a}mm IS2062",
                   "Aluminium flat bar 40x{a}", "GI pipe {a}mm class B", "Copper rod {a}mm dia",
                   "Brass sheet {a}mm"], "KG"),
    "FASTENER": (["Hex bolt M{m}x{a} zinc plated", "Hex nut M{m} gr 8", "SS washer M{m}",
                  "Allen screw M{m}x{a}", "Pop rivet {m}mm", "Anchor fastener M{m}x{a}"], "EA"),
    "LUBRICANT": (["Hydraulic oil ISO VG {v} - 20L drum", "Gear oil EP{v} 210L barrel",
                   "Lithium grease 18kg bucket", "Cutting coolant 20L can", "Spindle oil VG{v}"], "L"),
    "ELECTRICAL": (["Copper wire {s} sq mm 90m coil", "Armoured cable 4C x {s} sqmm",
                    "Flexible cable {s} sqmm red"], "M"),
    "ELECTRICAL_EA": (["MCB {m}A 3 pole", "Contactor {m}A 415V", "Copper lug {s} sqmm"], "EA"),
    "BEARING": (["Bearing 62{b} 2RS", "Ball bearing 63{b} ZZ SKF", "Taper roller bearing 302{b}",
                 "Bronze bush {a}mm ID"], "EA"),
    "PPE": (["Nitrile safety gloves size {g}", "Safety shoes size {sh} steel toe", "Ear plugs foam"], "PR"),
    "PPE_EA": (["Safety helmet yellow", "Safety goggles anti fog", "Hi-vis vest XL"], "EA"),
}


def gen_material(r):
    key = r.choice(list(_MAT))
    tpls, uom = _MAT[key]
    t = r.choice(tpls).format(a=r.choice([2, 3, 5, 6, 8, 10, 12, 16, 20, 25, 40, 50]), m=r.choice([6, 8, 10, 12, 16, 20, 25, 32]),
                              v=r.choice([32, 46, 68, 90]), s=r.choice([1.5, 2.5, 4, 6, 10, 16, 25]),
                              b=r.choice(["04", "05", "06", "08", "10"]), g=r.choice(["M", "L", "XL", "9", "10"]),
                              sh=r.choice([7, 8, 9, 10]))
    style = r.random()
    if style < .3:
        t = t.upper()
    elif style < .45:
        t = t.lower()
    if r.random() < .3:
        t = r.choice(["PR: ", "Req - ", "urgent: ", "Item: ", "", "Pls procure "]) + t
    cat = key.replace("_EA", "")
    return t, {"category": cat, "base_uom": uom}


# ---------------------------------------------------------------- complaints
_COMP = [
    "My order #{n} arrived {d} days late and the box was crushed. Two of the {p} are dented.",
    "I was charged twice for order {n}. Please refund the duplicate payment of {amt}.",
    "The {p} I bought stopped working after {d} days. This is the second time this has happened.",
    "Your delivery partner marked order {n} as delivered but I never received it.",
    "I've been on hold for {d} minutes trying to change my delivery address. Very frustrating.",
    "The invoice for order {n} shows the wrong GST number, my accountant can't file it.",
    "Ordered {p} in blue, received red. I need it for an event this weekend.",
]


def gen_complaint(r):
    t = r.choice(_COMP).format(n=r.randint(10000, 99999), d=r.randint(2, 45),
                               p=r.choice(["mixer grinders", "water bottles", "office chairs", "headphones",
                                           "LED panels", "kurtas"]),
                               amt=r.choice(["₹2,499", "€89", "$149", "£35"]))
    return t, None


GENERATORS = {"invoice_extract": gen_invoice, "material_normalize": gen_material,
              "complaint_reply": gen_complaint}


def sample(task_id, seed=None):
    r = random.Random(seed)
    return GENERATORS[task_id](r)

"""Adds the new Dashboard texts to ui/translations/hi.json and mr.json.

Run once from the project folder:   python add_dashboard_translations.py

Only texts that are not in the file yet are added; nothing existing is changed or removed.
Safe to run again.
"""

import json
from pathlib import Path

FOLDER = Path(__file__).resolve().parent / "ui" / "translations"

HI = {
    "... and {n} more": "... और {n} और",
    "Add Product": "प्रोडक्ट जोड़ें",
    "Best-selling products": "सबसे ज़्यादा बिकने वाले प्रोडक्ट",
    "Cash in hand (expected)": "हाथ में नकद (अपेक्षित)",
    "Customers owe you": "ग्राहकों से लेना बाकी",
    "Day closed": "दिन बंद",
    "Day not opened": "दिन शुरू नहीं हुआ",
    "Day open": "दिन शुरू है",
    "Every product in stock has sold in this period.": "स्टॉक में मौजूद हर प्रोडक्ट इस अवधि में बिका है।",
    "Expenses today": "आज के खर्च",
    "In stock but not selling": "स्टॉक में है पर बिक नहीं रहा",
    "Last 30 days": "पिछले 30 दिन",
    "New Sale": "नई बिक्री",
    "No sales in this period.": "इस अवधि में कोई बिक्री नहीं हुई।",
    "Process Return or Exchange": "रिटर्न या एक्सचेंज करें",
    "Purchases received today": "आज प्राप्त खरीद",
    "Receive Purchase": "खरीद प्राप्त करें",
    "Record Expense": "खर्च दर्ज करें",
    "Record Payment": "भुगतान दर्ज करें",
    "Search Stock": "स्टॉक खोजें",
    "Stock in hand (sellable)": "हाथ में स्टॉक (बिकने योग्य)",
    "Value at cost: {amount}. Method: {method}": "लागत पर मूल्य: {amount}। तरीका: {method}",
    "View Daily Report": "दैनिक रिपोर्ट देखें",
    "You owe suppliers": "सप्लायर को देना बाकी",
    "{n} customer(s). Overdue: {amount}": "{n} ग्राहक। देर से बकाया: {amount}",
    "{n} item(s) have no cost recorded, so they are valued at zero.": "{n} आइटम की लागत दर्ज नहीं है, इसलिए उनका मूल्य शून्य गिना गया है।",
    "{n} purchase(s), {qty} item(s)": "{n} खरीद, {qty} आइटम",
    "{n} supplier(s)": "{n} सप्लायर",
    "{product} ({qty} in stock)": "{product} (स्टॉक में {qty})",
    "{product}: {qty} sold": "{product}: {qty} बिके",
}

MR = {
    "... and {n} more": "... आणि {n} आणखी",
    "Add Product": "प्रॉडक्ट जोडा",
    "Best-selling products": "सर्वाधिक विकले जाणारे प्रॉडक्ट",
    "Cash in hand (expected)": "हातातील रोख (अपेक्षित)",
    "Customers owe you": "ग्राहकांकडून येणे बाकी",
    "Day closed": "दिवस बंद",
    "Day not opened": "दिवस सुरू केला नाही",
    "Day open": "दिवस सुरू आहे",
    "Every product in stock has sold in this period.": "स्टॉकमधील प्रत्येक प्रॉडक्ट या कालावधीत विकले गेले आहे.",
    "Expenses today": "आजचे खर्च",
    "In stock but not selling": "स्टॉकमध्ये आहे पण विकले जात नाही",
    "Last 30 days": "मागील 30 दिवस",
    "New Sale": "नवीन विक्री",
    "No sales in this period.": "या कालावधीत विक्री झाली नाही.",
    "Process Return or Exchange": "रिटर्न किंवा एक्सचेंज करा",
    "Purchases received today": "आज मिळालेली खरेदी",
    "Receive Purchase": "खरेदी स्वीकारा",
    "Record Expense": "खर्च नोंदवा",
    "Record Payment": "पेमेंट नोंदवा",
    "Search Stock": "स्टॉक शोधा",
    "Stock in hand (sellable)": "हातातील स्टॉक (विक्रीयोग्य)",
    "Value at cost: {amount}. Method: {method}": "किमतीवर मूल्य: {amount}. पद्धत: {method}",
    "View Daily Report": "दैनिक रिपोर्ट पहा",
    "You owe suppliers": "सप्लायरला देणे बाकी",
    "{n} customer(s). Overdue: {amount}": "{n} ग्राहक. थकीत: {amount}",
    "{n} item(s) have no cost recorded, so they are valued at zero.": "{n} आयटमची किंमत नोंदलेली नाही, म्हणून त्यांचे मूल्य शून्य धरले आहे.",
    "{n} purchase(s), {qty} item(s)": "{n} खरेदी, {qty} आयटम",
    "{n} supplier(s)": "{n} सप्लायर",
    "{product} ({qty} in stock)": "{product} (स्टॉकमध्ये {qty})",
    "{product}: {qty} sold": "{product}: {qty} विकले",
}


def merge(code: str, new: dict) -> int:
    path = FOLDER / f"{code}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    for key, value in new.items():
        if not data.get(key):
            data[key] = value
            added += 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return added


if __name__ == "__main__":
    for code, table in (("hi", HI), ("mr", MR)):
        print(f"{code}.json: {merge(code, table)} text(s) added")

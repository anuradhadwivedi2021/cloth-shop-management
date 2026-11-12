"""Adds the Phase 3 texts (salesperson, commission, ...) to ui/translations/hi.json and mr.json.

Run from the project folder:   python add_phase3_translations.py
Only missing texts are added; nothing existing changes. Safe to run again.
"""

import json
from pathlib import Path

FOLDER = Path(__file__).resolve().parent / "ui" / "translations"

# each entry: English text -> (Hindi, Marathi)
TEXTS = {
    # ---- commission
    "Commission Rates": ("कमीशन दरें", "कमिशन दर"),
    "Commission": ("कमीशन", "कमिशन"),
    "Commission by salesperson": ("सेल्सपर्सन के अनुसार कमीशन", "सेल्सपर्सननुसार कमिशन"),
    "Salesperson": ("सेल्सपर्सन", "सेल्सपर्सन"),
    "Default rate %": ("सामान्य दर %", "सामान्य दर %"),
    "Save default rate": ("सामान्य दर सेव करें", "सामान्य दर सेव्ह करा"),
    "Own rate %": ("अपनी दर %", "स्वतःची दर %"),
    "Save rate for selected person": ("चुने व्यक्ति की दर सेव करें", "निवडलेल्या व्यक्तीची दर सेव्ह करा"),
    "Use the default for selected person": ("चुने व्यक्ति के लिए सामान्य दर लगाएँ", "निवडलेल्या व्यक्तीसाठी सामान्य दर लावा"),
    "Person": ("व्यक्ति", "व्यक्ती"),
    "Rate used %": ("लागू दर %", "लागू दर %"),
    "Select a person first.": ("पहले व्यक्ति चुनें।", "आधी व्यक्ती निवडा."),
    "Commission is ON: bills record the salesperson.": ("कमीशन चालू है: बिल पर सेल्सपर्सन दर्ज होता है।", "कमिशन चालू आहे: बिलावर सेल्सपर्सन नोंदवला जातो."),
    "Commission is OFF: nobody is credited on bills.": ("कमीशन बंद है: बिल पर किसी का नाम दर्ज नहीं होता।", "कमिशन बंद आहे: बिलावर कोणाचेही नाव नोंदवले जात नाही."),
    "Switch commission on": ("कमीशन चालू करें", "कमिशन चालू करा"),
    "Switch commission off": ("कमीशन बंद करें", "कमिशन बंद करा"),
    "Commission is a percentage of net sales (before tax) of each salesperson's bills; returns take it back. See Reports > Commission by salesperson.": (
        "कमीशन हर सेल्सपर्सन के बिलों की नेट बिक्री (टैक्स से पहले) का प्रतिशत है; रिटर्न पर कमीशन वापस कटता है। रिपोर्ट > सेल्सपर्सन के अनुसार कमीशन देखें।",
        "कमिशन म्हणजे प्रत्येक सेल्सपर्सनच्या बिलांच्या निव्वळ विक्रीचे (करापूर्वी) टक्के; परतावा झाल्यास कमिशन परत जाते. रिपोर्ट > सेल्सपर्सननुसार कमिशन पहा."),
    "The commission must be a number, like 1.5": ("कमीशन एक संख्या होनी चाहिए, जैसे 1.5", "कमिशन संख्या असावी, उदा. 1.5"),
    "The commission can have at most 2 decimal places.": ("कमीशन में अधिकतम 2 दशमलव अंक हो सकते हैं।", "कमिशनमध्ये जास्तीत जास्त 2 दशांश अंक असू शकतात."),
    "The salesperson was not found or is not active.": ("सेल्सपर्सन नहीं मिला या सक्रिय नहीं है।", "सेल्सपर्सन सापडला नाही किंवा सक्रिय नाही."),
    "Commission": ("कमीशन", "कमिशन"),
    # ---- anniversary on the call list
    "Anniversary": ("शादी की सालगिरह", "लग्नाचा वाढदिवस"),
    "anniversary": ("शादी की सालगिरह", "लग्नाचा वाढदिवस"),
    "Occasion": ("मौक़ा", "प्रसंग"),
    "Date": ("तारीख", "तारीख"),
    "Anniversary is not a real date.": ("सालगिरह की तारीख सही नहीं है।", "लग्नाच्या वाढदिवसाची तारीख योग्य नाही."),
    "Anniversary must look like 2001-03-25 (or 03-25 without the year).": ("सालगिरह ऐसी लिखें: 2001-03-25 (या बिना साल के 03-25)।", "लग्नाचा वाढदिवस असा लिहा: 2001-03-25 (किंवा वर्षाशिवाय 03-25)."),
    "Anniversary is not a believable date.": ("सालगिरह की तारीख विश्वसनीय नहीं है।", "लग्नाच्या वाढदिवसाची तारीख विश्वासार्ह नाही."),
}


def merge(code, index) -> int:
    path = FOLDER / f"{code}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    for english, pair in TEXTS.items():
        if english not in data:
            data[english] = pair[index]
            added += 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return added


if __name__ == "__main__":
    for index, code in enumerate(("hi", "mr")):
        print(f"{code}.json: {merge(code, index)} text(s) added")

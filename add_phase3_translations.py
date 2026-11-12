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
    # ---- alterations
    "Alterations": ("अल्टरेशन / सिलाई", "अल्टरेशन / शिलाई"),
    "Received": ("प्राप्त", "प्राप्त"),
    "In progress": ("काम चालू", "काम सुरू"),
    "Ready": ("तैयार", "तयार"),
    "Delivered": ("दे दिया गया", "दिले"),
    "Ready for pickup": ("लेने के लिए तैयार", "घेण्यासाठी तयार"),
    "Overdue": ("देर से", "उशिरा"),
    "Late": ("देर", "उशीर"),
    "Open": ("खुला", "उघडे"),
    "Start work": ("काम शुरू करें", "काम सुरू करा"),
    "Mark ready": ("तैयार करें", "तयार करा"),
    "Deliver and take the balance": ("दें और बाकी रकम लें", "द्या आणि उरलेली रक्कम घ्या"),
    "Next step": ("अगला कदम", "पुढील पायरी"),
    "Cancel this job": ("यह जॉब रद्द करें", "हे काम रद्द करा"),
    "Save job": ("जॉब सेव करें", "काम सेव्ह करा"),
    "New job": ("नया जॉब", "नवे काम"),
    "Jobs": ("जॉब", "कामे"),
    "Job no.": ("जॉब नं.", "काम क्र."),
    "Customer": ("ग्राहक", "ग्राहक"),
    "Item": ("सामान", "वस्तू"),
    "Work": ("काम", "काम"),
    "Charge": ("चार्ज", "चार्ज"),
    "Balance": ("बाकी", "उरलेले"),
    "Promised": ("वादा की तारीख", "वचन दिलेली तारीख"),
    "Customer name": ("ग्राहक का नाम", "ग्राहकाचे नाव"),
    "Customer phone": ("ग्राहक का फ़ोन", "ग्राहकाचा फोन"),
    "Item, for example Blue shirt": ("सामान, जैसे नीली शर्ट", "वस्तू, उदा. निळा शर्ट"),
    "Work to do, for example Shorten sleeves": ("करने का काम, जैसे बाजू छोटी करना", "करायचे काम, उदा. बाही लहान करणे"),
    "Measurements (optional)": ("नाप (ज़रूरी नहीं)", "माप (ऐच्छिक)"),
    "Charge (Rs)": ("चार्ज (₹)", "चार्ज (₹)"),
    "Advance taken (Rs)": ("एडवांस लिया (₹)", "आगाऊ घेतले (₹)"),
    "Promised date DD/MM/YYYY": ("वादा की तारीख DD/MM/YYYY", "वचन दिलेली तारीख DD/MM/YYYY"),
    "A job order is not a bill. The advance and balance are written on the job only.": (
        "जॉब ऑर्डर बिल नहीं है। एडवांस और बाकी रकम सिर्फ़ जॉब पर लिखी जाती है।",
        "जॉब ऑर्डर बिल नाही. आगाऊ आणि उरलेली रक्कम फक्त कामावर लिहिली जाते."),
    "Job {no} saved. Balance to take on delivery: {amount}.": (
        "जॉब {no} सेव हुआ। देते समय लेने वाली रकम: {amount}।", "काम {no} सेव्ह झाले. देताना घ्यायची रक्कम: {amount}."),
    "The promised date": ("वादा की तारीख", "वचन दिलेली तारीख"),
    "Take {amount} from the customer and mark this job delivered?": (
        "ग्राहक से {amount} लेकर यह जॉब 'दे दिया' करें?", "ग्राहकाकडून {amount} घेऊन हे काम 'दिले' करायचे?"),
    "Cancel job {no}?": ("जॉब {no} रद्द करें?", "काम {no} रद्द करायचे?"),
    "The item is required.": ("सामान लिखना ज़रूरी है।", "वस्तू लिहिणे आवश्यक आहे."),
    "The work to do is required.": ("करने का काम लिखना ज़रूरी है।", "करायचे काम लिहिणे आवश्यक आहे."),
    "The advance cannot be more than the charge.": ("एडवांस चार्ज से ज़्यादा नहीं हो सकता।", "आगाऊ चार्जपेक्षा जास्त असू शकत नाही."),
    "The promised date cannot be in the past.": ("वादे की तारीख बीती हुई नहीं हो सकती।", "वचन दिलेली तारीख मागील असू शकत नाही."),
    "The promised date is not a real date.": ("वादे की तारीख सही नहीं है।", "वचन दिलेली तारीख योग्य नाही."),
    "Alteration job not found.": ("जॉब नहीं मिला।", "काम सापडले नाही."),
    "A delivered or cancelled job cannot be changed.": ("दिया हुआ या रद्द जॉब बदला नहीं जा सकता।", "दिलेले किंवा रद्द काम बदलता येत नाही."),
    "The customer was not found.": ("ग्राहक नहीं मिला।", "ग्राहक सापडला नाही."),
    # ---- reports: titles, formulas and column headings
    "Sell-through % by product": ("प्रोडक्ट के अनुसार बिक्री प्रतिशत (सेल-थ्रू)", "प्रोडक्टनुसार विक्री टक्के (सेल-थ्रू)"),
    "Sell-through % = pieces sold / (pieces sold + stock left at the end of the period) x 100. Sorted from the best seller to the slowest.": (
        "सेल-थ्रू % = बिके पीस / (बिके पीस + अवधि के अंत में बचा स्टॉक) x 100। सबसे ज़्यादा बिकने वाले से सबसे धीमे तक।",
        "सेल-थ्रू % = विकलेले नग / (विकलेले नग + कालावधीच्या शेवटी उरलेला स्टॉक) x 100. सर्वाधिक विक्रीपासून सर्वात कमीपर्यंत."),
    "Commission = net sales x rate. Net sales = taxable value of the person's bills minus the taxable value of credit notes made against those bills, before tax.": (
        "कमीशन = नेट बिक्री x दर। नेट बिक्री = व्यक्ति के बिलों का टैक्स-पूर्व मूल्य घटा उन बिलों पर बने क्रेडिट नोट का टैक्स-पूर्व मूल्य।",
        "कमिशन = निव्वळ विक्री x दर. निव्वळ विक्री = व्यक्तीच्या बिलांचे करापूर्वीचे मूल्य वजा त्या बिलांवरील क्रेडिट नोटचे करापूर्वीचे मूल्य."),
    "Sell through percent": ("बिक्री %", "विक्री %"),
    "Available": ("उपलब्ध था", "उपलब्ध होते"),
    "Closing": ("बचा स्टॉक", "उरलेला स्टॉक"),
    "Net sales": ("नेट बिक्री", "निव्वळ विक्री"),
    "Sales taxable": ("बिक्री (टैक्स से पहले)", "विक्री (करापूर्वी)"),
    "Returns taxable": ("रिटर्न (टैक्स से पहले)", "परतावा (करापूर्वी)"),
    "Rate percent": ("दर %", "दर %"),
    "Net sales paise": ("नेट बिक्री", "निव्वळ विक्री"),
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

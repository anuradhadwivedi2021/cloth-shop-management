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
    # ---- fabric by the metre
    "Fabric": ("कपड़ा (मीटर में)", "कापड (मीटरमध्ये)"),
    "Roll": ("थान / रोल", "थान / रोल"),
    "Remnant (cut-off)": ("कतरन / बचा टुकड़ा", "तुकडा / उरलेले कापड"),
    "Length": ("लंबाई", "लांबी"),
    "Length in metres, for example 2.5": ("मीटर में लंबाई, जैसे 2.5", "मीटरमध्ये लांबी, उदा. 2.5"),
    "Roll number or note (optional)": ("रोल नंबर या नोट (ज़रूरी नहीं)", "रोल क्रमांक किंवा नोंद (ऐच्छिक)"),
    "Shortest cut in metres (blank = none)": ("सबसे छोटा कट मीटर में (खाली = कोई नहीं)", "सर्वात लहान कट मीटरमध्ये (रिकामे = नाही)"),
    "Add to stock": ("स्टॉक में जोड़ें", "स्टॉकमध्ये जोडा"),
    "Save shortest cut": ("सबसे छोटा कट सेव करें", "सर्वात लहान कट सेव्ह करा"),
    "Piece": ("टुकड़ा", "तुकडा"),
    "Colour": ("रंग", "रंग"),
    "Reference": ("संदर्भ", "संदर्भ"),
    "Left": ("बचा हुआ", "उरलेले"),
    "Rolls and remnants on the shelf": ("शेल्फ पर रोल और कतरन", "शेल्फवरील थान आणि तुकडे"),
    "Fabric is sold by the metre. To add a fabric, set the product's unit to metre in Products and price it per metre in whole rupees.": (
        "कपड़ा मीटर के हिसाब से बिकता है। कपड़ा जोड़ने के लिए Products में प्रोडक्ट की यूनिट 'metre' करें और भाव प्रति मीटर पूरे रुपये में रखें।",
        "कापड मीटरने विकले जाते. कापड जोडण्यासाठी Products मध्ये प्रोडक्टचे एकक 'metre' करा आणि भाव प्रति मीटर पूर्ण रुपयांत ठेवा."),
    "In stock: {length}. Price: {price} per metre.": ("स्टॉक में: {length}। भाव: {price} प्रति मीटर।", "स्टॉकमध्ये: {length}. भाव: {price} प्रति मीटर."),
    "No fabric yet.": ("अभी कोई कपड़ा नहीं है।", "अजून कापड नाही."),
    "Choose a fabric first.": ("पहले कपड़ा चुनें।", "आधी कापड निवडा."),
    "The length must be a number of metres, like 2.5": ("लंबाई मीटर में संख्या होनी चाहिए, जैसे 2.5", "लांबी मीटरमध्ये संख्या असावी, उदा. 2.5"),
    "The length must be more than zero.": ("लंबाई शून्य से ज़्यादा होनी चाहिए।", "लांबी शून्यापेक्षा जास्त असावी."),
    "The length can have at most 2 decimal places (whole centimetres).": ("लंबाई में अधिकतम 2 दशमलव अंक (पूरे सेंटीमीटर) हो सकते हैं।", "लांबीत जास्तीत जास्त 2 दशांश अंक (पूर्ण सेंटीमीटर) असू शकतात."),
    "Fabric is priced per metre in whole rupees. Change the price to whole rupees.": ("कपड़े का भाव प्रति मीटर पूरे रुपये में होना चाहिए। भाव पूरे रुपये में करें।", "कापडाचा भाव प्रति मीटर पूर्ण रुपयांत असावा. भाव पूर्ण रुपयांत करा."),
    "A piece is a roll or a remnant.": ("टुकड़ा थान होता है या कतरन।", "तुकडा थान किंवा उरलेले कापड असतो."),
    "This product is not sold by the metre. Set its unit to metre first.": ("यह प्रोडक्ट मीटर में नहीं बिकता। पहले इसकी यूनिट metre करें।", "हे प्रोडक्ट मीटरने विकले जात नाही. आधी त्याचे एकक metre करा."),
    # ---- second counter (LAN)
    "Second Counter": ("दूसरा काउंटर", "दुसरा काउंटर"),
    "This PC's address": ("इस PC का पता", "या PC चा पत्ता"),
    "Pairing code (type it on the second PC):": ("पेयरिंग कोड (दूसरे PC पर टाइप करें):", "पेअरिंग कोड (दुसऱ्या PC वर टाइप करा):"),
    "Make a new pairing code": ("नया पेयरिंग कोड बनाएँ", "नवीन पेअरिंग कोड बनवा"),
    "Start the second counter automatically when this app opens": ("ऐप खुलते ही दूसरा काउंटर अपने आप शुरू करें", "अॅप उघडताच दुसरा काउंटर आपोआप सुरू करा"),
    "A second PC on the same shop WiFi or LAN can bill on this shop at the same time. Nothing goes to the internet. Keep this app open on this PC while the second counter works.": (
        "दुकान के उसी WiFi या LAN पर दूसरा PC इसी दुकान पर एक साथ बिल बना सकता है। इंटरनेट पर कुछ नहीं जाता। दूसरा काउंटर चलते समय इस PC पर यह ऐप खुला रखें।",
        "दुकानाच्या त्याच WiFi किंवा LAN वरील दुसरा PC याच दुकानावर एकाच वेळी बिल करू शकतो. इंटरनेटवर काहीही जात नाही. दुसरा काउंटर चालू असताना या PC वर हे अॅप उघडे ठेवा."),
    "The second counter is OFF.": ("दूसरा काउंटर बंद है।", "दुसरा काउंटर बंद आहे."),
    "The second counter is ON: {address}": ("दूसरा काउंटर चालू है: {address}", "दुसरा काउंटर चालू आहे: {address}"),
    "Start the second counter": ("दूसरा काउंटर शुरू करें", "दुसरा काउंटर सुरू करा"),
    "Stop the second counter": ("दूसरा काउंटर बंद करें", "दुसरा काउंटर बंद करा"),
    "On the second PC: sign-in page > Connect to the main counter > address {address}, then the pairing code above.": (
        "दूसरे PC पर: साइन-इन पेज > Connect to the main counter > पता {address}, फिर ऊपर का पेयरिंग कोड।",
        "दुसऱ्या PC वर: साइन-इन पेज > Connect to the main counter > पत्ता {address}, मग वरचा पेअरिंग कोड."),
    "Type the main PC's address as numbers, like 192.168.1.20.": ("मुख्य PC का पता अंकों में लिखें, जैसे 192.168.1.20।", "मुख्य PC चा पत्ता अंकांत लिहा, उदा. 192.168.1.20."),
    "Only an address on your own shop network (like 192.168.1.20) is allowed.": ("सिर्फ़ आपके दुकान के नेटवर्क का पता (जैसे 192.168.1.20) चलेगा।", "फक्त तुमच्या दुकानाच्या नेटवर्कचा पत्ता (उदा. 192.168.1.20) चालेल."),
    "The pairing code has 10 letters and numbers, like K7M2-QX9P-4T.": ("पेयरिंग कोड में 10 अक्षर और अंक होते हैं, जैसे K7M2-QX9P-4T।", "पेअरिंग कोडमध्ये 10 अक्षरे आणि अंक असतात, उदा. K7M2-QX9P-4T."),
    "The main counter cannot be reached. Check that its PC is on, the app is open and 'Second Counter' is started.": (
        "मुख्य काउंटर से संपर्क नहीं हो पा रहा। देखें कि उसका PC चालू है, ऐप खुला है और 'दूसरा काउंटर' शुरू है।",
        "मुख्य काउंटरशी संपर्क होत नाही. त्याचा PC चालू आहे, अॅप उघडे आहे आणि 'दुसरा काउंटर' सुरू आहे ते तपासा."),
    "The main counter refused the connection. Check the pairing code.": ("मुख्य काउंटर ने कनेक्शन मना किया। पेयरिंग कोड जाँचें।", "मुख्य काउंटरने कनेक्शन नाकारले. पेअरिंग कोड तपासा."),
    "The connection to the main counter was lost. Sign in again.": ("मुख्य काउंटर से कनेक्शन टूट गया। फिर से साइन-इन करें।", "मुख्य काउंटरशी कनेक्शन तुटले. पुन्हा साइन-इन करा."),
    "Show this PC's addresses": ("इस PC के पते दिखाएँ", "या PC चे पत्ते दाखवा"),
    "Detect automatically": ("अपने आप पता करें", "आपोआप शोधा"),
    "This PC is not on a shop network (no private address found).": ("यह PC दुकान के नेटवर्क पर नहीं है (कोई निजी पता नहीं मिला)।", "हा PC दुकानाच्या नेटवर्कवर नाही (खाजगी पत्ता सापडला नाही)."),
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

"""Adds the Phase 2 texts (UPI QR, estimates, bulk edit, seasons, loyalty, vouchers, images, year lock, Tally)
to ui/translations/hi.json and mr.json.

Run once from the project folder:   python add_phase2_translations.py

Only texts that are not in the file yet are added; nothing existing is changed or removed.
Safe to run again.
"""

import json
from pathlib import Path

FOLDER = Path(__file__).resolve().parent / "ui" / "translations"

# each entry: English text -> (Hindi, Marathi)
TEXTS = {
    # ---- UPI QR
    "Show UPI QR": ("UPI QR दिखाएँ", "UPI QR दाखवा"),
    "Hide UPI QR": ("UPI QR छिपाएँ", "UPI QR लपवा"),
    "Customer scans and pays {amount} to {upi}. When the money has arrived, enter it above as a UPI payment.": (
        "ग्राहक स्कैन करके {upi} को {amount} देगा। पैसे आ जाने पर ऊपर UPI भुगतान के रूप में लिखें।",
        "ग्राहक स्कॅन करून {upi} ला {amount} देईल. पैसे आल्यावर वर UPI पेमेंट म्हणून लिहा."),
    "Print a UPI QR on bills that still have an amount to pay (needs the shop's UPI id)": (
        "जिन बिलों में रकम बाकी है उन पर UPI QR छापें (दुकान की UPI आईडी ज़रूरी)",
        "ज्या बिलांवर रक्कम बाकी आहे त्यावर UPI QR छापा (दुकानाचा UPI आयडी आवश्यक)"),
    "The shop's UPI id is missing or not valid. Set it in Settings.": (
        "दुकान की UPI आईडी नहीं है या सही नहीं है। इसे सेटिंग्स में डालें।",
        "दुकानाचा UPI आयडी नाही किंवा चुकीचा आहे. तो सेटिंग्जमध्ये टाका."),
    # ---- estimates
    "Add to estimate": ("अनुमान में जोड़ें", "अंदाजात जोडा"),
    "An estimate is a price quote. It does not take stock and is not a bill.": (
        "अनुमान एक कीमत का भाव है। यह स्टॉक नहीं घटाता और बिल नहीं है।",
        "अंदाज म्हणजे किमतीचा भाव. तो स्टॉक कमी करत नाही आणि बिल नाही."),
    "Bill {no} saved from the estimate.": ("अनुमान से बिल {no} सेव हुआ।", "अंदाजातून बिल {no} सेव्ह झाले."),
    "Cancel estimate {no}?": ("अनुमान {no} रद्द करें?", "अंदाज {no} रद्द करायचा?"),
    "Cancel this estimate": ("यह अनुमान रद्द करें", "हा अंदाज रद्द करा"),
    "Customer name (optional)": ("ग्राहक का नाम (ज़रूरी नहीं)", "ग्राहकाचे नाव (ऐच्छिक)"),
    "Estimate": ("अनुमान", "अंदाज"),
    "Estimate no.": ("अनुमान संख्या", "अंदाज क्रमांक"),
    "Estimate saved as a PDF: {path}": ("अनुमान PDF में सेव हुआ: {path}", "अंदाज PDF म्हणून सेव्ह झाला: {path}"),
    "Estimate sent to the printer ({printer}).": ("अनुमान प्रिंटर ({printer}) को भेजा गया।", "अंदाज प्रिंटरला ({printer}) पाठवला."),
    "Estimate total: {amount}": ("अनुमान कुल: {amount}", "अंदाज एकूण: {amount}"),
    "Estimate {no} saved. Total {amount}, valid until {day}.": (
        "अनुमान {no} सेव हुआ। कुल {amount}, {day} तक मान्य।", "अंदाज {no} सेव्ह झाला. एकूण {amount}, {day} पर्यंत वैध."),
    "Estimates": ("अनुमान (एस्टिमेट)", "अंदाज (एस्टिमेट)"),
    "Expired": ("समाप्त", "मुदत संपली"),
    "Made into a bill": ("बिल बन गया", "बिल केले"),
    "Make a bill...": ("बिल बनाएँ...", "बिल करा..."),
    "New estimate": ("नया अनुमान", "नवीन अंदाज"),
    "No printer was found on this computer. The estimate is saved; connect a printer and print it again.": (
        "इस कंप्यूटर पर प्रिंटर नहीं मिला। अनुमान सेव है; प्रिंटर जोड़कर दोबारा प्रिंट करें।",
        "या संगणकावर प्रिंटर सापडला नाही. अंदाज सेव्ह आहे; प्रिंटर जोडून पुन्हा प्रिंट करा."),
    "Prices have changed since this estimate: the same items now cost {amount}. A bill uses today's prices.": (
        "इस अनुमान के बाद कीमतें बदली हैं: वही सामान अब {amount} का है। बिल में आज की कीमतें लगती हैं।",
        "या अंदाजानंतर किमती बदलल्या आहेत: तेच सामान आता {amount} चे आहे. बिलात आजच्या किमती लागतात."),
    "Print": ("प्रिंट", "प्रिंट"),
    "Rate": ("दर", "दर"),
    "Remove the selected line": ("चुनी हुई पंक्ति हटाएँ", "निवडलेली ओळ काढा"),
    "Save as PDF...": ("PDF में सेव करें...", "PDF म्हणून सेव्ह करा..."),
    "Save estimate": ("अनुमान सेव करें", "अंदाज सेव्ह करा"),
    "The estimate could not be found.": ("अनुमान नहीं मिला।", "अंदाज सापडला नाही."),
    "Valid for (days)": ("मान्य (दिन)", "वैध (दिवस)"),
    "Open": ("खुला", "उघडा"),
    "All": ("सभी", "सर्व"),
    "Cancelled": ("रद्द", "रद्द"),
    "Only an open estimate can be cancelled.": ("सिर्फ़ खुला अनुमान रद्द हो सकता है।", "फक्त उघडा अंदाज रद्द करता येतो."),
    "This estimate has expired. Make a new estimate.": ("यह अनुमान समाप्त हो गया है। नया अनुमान बनाएँ।", "या अंदाजाची मुदत संपली आहे. नवीन अंदाज करा."),
    "This estimate was cancelled.": ("यह अनुमान रद्द हो चुका है।", "हा अंदाज रद्द झाला आहे."),
    "Estimate not found.": ("अनुमान नहीं मिला।", "अंदाज सापडला नाही."),
    "Add at least one item to the estimate.": ("अनुमान में कम से कम एक सामान जोड़ें।", "अंदाजात किमान एक वस्तू जोडा."),
    # ---- Bulk edit
    "Stock ageing": ("स्टॉक की उम्र", "स्टॉकचे वय"),
    "older than 60 days": ("60 दिन से पुराना", "60 दिवसांपेक्षा जुना"),
    "older than 90 days": ("90 दिन से पुराना", "90 दिवसांपेक्षा जुना"),
    "older than 180 days": ("180 दिन से पुराना", "180 दिवसांपेक्षा जुना"),
    "Only stock older than (days)": ("इतने दिन से पुराना स्टॉक (दिन)", "इतक्या दिवसांपेक्षा जुना स्टॉक (दिवस)"),
    "The number of days must be zero or more.": ("दिनों की संख्या शून्य या अधिक होनी चाहिए।", "दिवसांची संख्या शून्य किंवा जास्त असावी."),
    "0-30 days": ("0-30 दिन", "0-30 दिवस"),
    "31-60 days": ("31-60 दिन", "31-60 दिवस"),
    "61-90 days": ("61-90 दिन", "61-90 दिवस"),
    "91-180 days": ("91-180 दिन", "91-180 दिवस"),
    "Over 180 days": ("180 दिन से ज़्यादा", "180 दिवसांपेक्षा जास्त"),
    # ---- Loyalty, vouchers, store credit
    "Code": ("कोड", "कोड"),
    "Gift Vouchers": ("गिफ़्ट वाउचर", "गिफ्ट व्हाउचर"),
    "Sell voucher": ("वाउचर बेचें", "व्हाउचर विका"),
    "Sell a gift voucher": ("गिफ़्ट वाउचर बेचें", "गिफ्ट व्हाउचर विका"),
    "Voucher value (Rs)": ("वाउचर की कीमत (₹)", "व्हाउचरची किंमत (₹)"),
    "Paid by": ("भुगतान का तरीका", "पैसे देण्याचा प्रकार"),
    "Valid until (optional)": ("इस तारीख तक मान्य (वैकल्पिक)", "या तारखेपर्यंत वैध (ऐच्छिक)"),
    "Own code (optional)": ("अपना कोड (वैकल्पिक)", "स्वतःचा कोड (ऐच्छिक)"),
    "(made automatically)": ("(अपने आप बनेगा)", "(आपोआप तयार होईल)"),
    "Cancel selected voucher": ("चुना हुआ वाउचर रद्द करें", "निवडलेले व्हाउचर रद्द करा"),
    "Refresh": ("रीफ़्रेश", "रिफ्रेश"),
    "Left": ("बाकी", "शिल्लक"),
    "Value": ("कीमत", "किंमत"),
    "Valid until": ("मान्य तारीख", "वैध तारखेपर्यंत"),
    "Active": ("चालू", "चालू"),
    "Used up": ("पूरा इस्तेमाल हो गया", "पूर्ण वापरले"),
    "Expired": ("समाप्त", "मुदत संपली"),
    "UPI": ("UPI", "UPI"),
    "Card": ("कार्ड", "कार्ड"),
    "Bank transfer": ("बैंक ट्रांसफ़र", "बँक ट्रान्सफर"),
    "Cheque": ("चेक", "धनादेश"),
    "Other": ("अन्य", "इतर"),
    "Select a voucher first.": ("पहले एक वाउचर चुनें।", "आधी एक व्हाउचर निवडा."),
    "Cancel voucher {code}? It can no longer be used.": ("वाउचर {code} रद्द करें? इसे फिर इस्तेमाल नहीं किया जा सकेगा।", "व्हाउचर {code} रद्द करायचे? ते पुन्हा वापरता येणार नाही."),
    "Voucher sold. Code: {code}": ("वाउचर बिक गया। कोड: {code}", "व्हाउचर विकले. कोड: {code}"),
    "A voucher is paid for when it is sold and then pays part or all of a later bill. The money taken now goes into the cash book.": ("वाउचर बेचते समय उसका पैसा लिया जाता है, और बाद में वह किसी बिल का कुछ या पूरा हिस्सा चुकाता है। अभी लिया गया पैसा कैश बुक में जाता है।", "व्हाउचर विकताना त्याचे पैसे घेतले जातात आणि नंतर ते बिलाचा काही किंवा पूर्ण भाग भरते. आता घेतलेले पैसे कॅश बुकमध्ये जातात."),
    "Points to use (you have {n})": ("इस्तेमाल करने वाले पॉइंट (आपके पास {n})", "वापरायचे पॉइंट (तुमच्याकडे {n})"),
    "Store credit to use (Rs, you have {amount})": ("इस्तेमाल करने वाला स्टोर क्रेडिट (₹, आपके पास {amount})", "वापरायचे स्टोअर क्रेडिट (₹, तुमच्याकडे {amount})"),
    "Gift voucher code": ("गिफ़्ट वाउचर कोड", "गिफ्ट व्हाउचर कोड"),
    "Points must be a whole number.": ("पॉइंट पूरी संख्या होने चाहिए।", "पॉइंट पूर्ण संख्या असावेत."),
    "Customer not found or not active.": ("ग्राहक नहीं मिला या चालू नहीं है।", "ग्राहक सापडला नाही किंवा चालू नाही."),
    "The voucher value must be more than zero.": ("वाउचर की कीमत शून्य से ज़्यादा होनी चाहिए।", "व्हाउचरची किंमत शून्यापेक्षा जास्त असावी."),
    "Choose how the voucher was paid for.": ("चुनें कि वाउचर का पैसा कैसे दिया गया।", "व्हाउचरचे पैसे कसे दिले ते निवडा."),
    "The expiry date is already past.": ("समाप्ति तारीख निकल चुकी है।", "मुदत संपल्याची तारीख होऊन गेली आहे."),
    "The expiry date must be a valid date (YYYY-MM-DD).": ("समाप्ति तारीख सही तारीख होनी चाहिए (YYYY-MM-DD)।", "मुदतीची तारीख योग्य असावी (YYYY-MM-DD)."),
    "A voucher code can have 4 to 30 letters, digits or dashes.": ("वाउचर कोड में 4 से 30 अक्षर, अंक या डैश हो सकते हैं।", "व्हाउचर कोडमध्ये 4 ते 30 अक्षरे, अंक किंवा डॅश असू शकतात."),
    "This voucher code is already used.": ("यह वाउचर कोड पहले से इस्तेमाल हो चुका है।", "हा व्हाउचर कोड आधीच वापरला आहे."),
    "This voucher code was not found.": ("यह वाउचर कोड नहीं मिला।", "हा व्हाउचर कोड सापडला नाही."),
    "A reason is required to cancel a voucher.": ("वाउचर रद्द करने का कारण ज़रूरी है।", "व्हाउचर रद्द करण्याचे कारण आवश्यक आहे."),
    "This voucher is already cancelled.": ("यह वाउचर पहले ही रद्द हो चुका है।", "हे व्हाउचर आधीच रद्द झाले आहे."),
    "This voucher was cancelled.": ("यह वाउचर रद्द हो चुका है।", "हे व्हाउचर रद्द झाले आहे."),
    "This voucher has expired.": ("इस वाउचर की मुदत खत्म हो गई है।", "या व्हाउचरची मुदत संपली आहे."),
    "This voucher has no balance left.": ("इस वाउचर में कुछ बाकी नहीं है।", "या व्हाउचरमध्ये शिल्लक नाही."),
    "This voucher belongs to another customer.": ("यह वाउचर किसी दूसरे ग्राहक का है।", "हे व्हाउचर दुसऱ्या ग्राहकाचे आहे."),
    "The voucher amount to use must be more than zero.": ("इस्तेमाल होने वाली वाउचर रकम शून्य से ज़्यादा होनी चाहिए।", "वापरायची व्हाउचर रक्कम शून्यापेक्षा जास्त असावी."),
    "The voucher does not have that much left.": ("वाउचर में इतनी रकम बाकी नहीं है।", "व्हाउचरमध्ये एवढी रक्कम शिल्लक नाही."),
    "A voucher can be used once on a bill.": ("एक बिल पर वाउचर एक ही बार इस्तेमाल हो सकता है।", "एका बिलावर व्हाउचर एकदाच वापरता येते."),
    "Each of store credit and points can be used once on a bill.": ("एक बिल पर स्टोर क्रेडिट और पॉइंट एक-एक बार ही इस्तेमाल हो सकते हैं।", "एका बिलावर स्टोअर क्रेडिट आणि पॉइंट प्रत्येकी एकदाच वापरता येतात."),
    "The store credit to use must be more than zero.": ("इस्तेमाल होने वाला स्टोर क्रेडिट शून्य से ज़्यादा होना चाहिए।", "वापरायचे स्टोअर क्रेडिट शून्यापेक्षा जास्त असावे."),
    "The customer does not have that much store credit.": ("ग्राहक के पास इतना स्टोर क्रेडिट नहीं है।", "ग्राहकाकडे एवढे स्टोअर क्रेडिट नाही."),
    "The points to use must be more than zero.": ("इस्तेमाल होने वाले पॉइंट शून्य से ज़्यादा होने चाहिए।", "वापरायचे पॉइंट शून्यापेक्षा जास्त असावेत."),
    "Loyalty points are switched off.": ("लॉयल्टी पॉइंट बंद हैं।", "लॉयल्टी पॉइंट बंद आहेत."),
    "The customer does not have that many points.": ("ग्राहक के पास इतने पॉइंट नहीं हैं।", "ग्राहकाकडे एवढे पॉइंट नाहीत."),
    "Points, vouchers and store credit together are more than the bill.": ("पॉइंट, वाउचर और स्टोर क्रेडिट मिलकर बिल से ज़्यादा हैं।", "पॉइंट, व्हाउचर आणि स्टोअर क्रेडिट मिळून बिलापेक्षा जास्त आहेत."),
    "Unknown way of paying.": ("भुगतान का तरीका अज्ञात है।", "पैसे देण्याचा प्रकार अज्ञात आहे."),
    "The payment with points, vouchers or store credit is not valid.": ("पॉइंट, वाउचर या स्टोर क्रेडिट से भुगतान सही नहीं है।", "पॉइंट, व्हाउचर किंवा स्टोअर क्रेडिटने देय योग्य नाही."),
    "Points must be a whole number, not zero.": ("पॉइंट शून्य नहीं, पूरी संख्या होने चाहिए।", "पॉइंट शून्य नाही, पूर्ण संख्या असावेत."),
    "A note is required for every correction.": ("हर सुधार के लिए नोट ज़रूरी है।", "प्रत्येक दुरुस्तीसाठी टीप आवश्यक आहे."),
    "The amount must be a whole number of paise, not zero.": ("रकम शून्य नहीं, पैसों की पूरी संख्या होनी चाहिए।", "रक्कम शून्य नाही, पैशांची पूर्ण संख्या असावी."),
    # ---- Product pictures
    "Pictures": ("तस्वीरें", "छायाचित्रे"),
    "Add picture...": ("तस्वीर जोड़ें...", "छायाचित्र जोडा..."),
    "Remove selected picture": ("चुनी हुई तस्वीर हटाएँ", "निवडलेले छायाचित्र काढा"),
    "Remove this picture?": ("यह तस्वीर हटाएँ?", "हे छायाचित्र काढायचे?"),
    "Select a picture first.": ("पहले एक तस्वीर चुनें।", "आधी एक छायाचित्र निवडा."),
    "PNG or JPEG, up to 2 MB each, up to 4 pictures.": ("PNG या JPEG, हर एक 2 MB तक, ज़्यादा से ज़्यादा 4 तस्वीरें।", "PNG किंवा JPEG, प्रत्येकी 2 MB पर्यंत, जास्तीत जास्त 4 छायाचित्रे."),
    "The picture file could not be read.": ("तस्वीर की फ़ाइल पढ़ी नहीं जा सकी।", "छायाचित्राची फाइल वाचता आली नाही."),
    "A picture can be at most 2 MB.": ("एक तस्वीर अधिकतम 2 MB की हो सकती है।", "एक छायाचित्र जास्तीत जास्त 2 MB असू शकते."),
    "The picture file is empty.": ("तस्वीर की फ़ाइल खाली है।", "छायाचित्राची फाइल रिकामी आहे."),
    "Only PNG or JPEG pictures can be added.": ("सिर्फ़ PNG या JPEG तस्वीरें जोड़ी जा सकती हैं।", "फक्त PNG किंवा JPEG छायाचित्रे जोडता येतात."),
    "A product can have at most 4 pictures. Remove one first.": ("एक सामान की ज़्यादा से ज़्यादा 4 तस्वीरें हो सकती हैं। पहले एक हटाएँ।", "एका वस्तूची जास्तीत जास्त 4 छायाचित्रे असू शकतात. आधी एक काढा."),
    "Picture not found.": ("तस्वीर नहीं मिली।", "छायाचित्र सापडले नाही."),
    # ---- Year close and Tally
    "Year Close and Tally": ("वर्ष बंद और टैली", "वर्ष बंद आणि टॅली"),
    "Financial year lock": ("वित्तीय वर्ष लॉक", "आर्थिक वर्ष लॉक"),
    "Tally export": ("टैली एक्सपोर्ट", "टॅली एक्सपोर्ट"),
    "Lock this year": ("यह वर्ष लॉक करें", "हे वर्ष लॉक करा"),
    "Unlock selected year": ("चुना हुआ वर्ष अनलॉक करें", "निवडलेले वर्ष अनलॉक करा"),
    "Locked year": ("लॉक किया वर्ष", "लॉक केलेले वर्ष"),
    "Locked on": ("लॉक की तारीख", "लॉक केल्याची तारीख"),
    "Save Tally file...": ("टैली फ़ाइल सेव करें...", "टॅली फाइल सेव्ह करा..."),
    "e.g. {year}": ("जैसे {year}", "उदा. {year}"),
    "A locked year accepts no new bills, credit notes, purchases or expenses dated in it. Work done today still goes in today's year.": ("लॉक किए वर्ष में उस तारीख के नए बिल, क्रेडिट नोट, खरीद या खर्च नहीं जुड़ते। आज का काम आज के वर्ष में ही जाता है।", "लॉक केलेल्या वर्षात त्या तारखेची नवी बिले, क्रेडिट नोट, खरेदी किंवा खर्च जोडता येत नाहीत. आजचे काम आजच्या वर्षातच जाते."),
    "Sales, credit notes and purchases of these dates as a Tally XML file. The ledgers (Sales, Cash, Output CGST ...) must already exist in Tally.": ("इन तारीखों की बिक्री, क्रेडिट नोट और खरीद एक टैली XML फ़ाइल में। लेजर (Sales, Cash, Output CGST ...) टैली में पहले से बने होने चाहिए।", "या तारखांची विक्री, क्रेडिट नोट आणि खरेदी एका टॅली XML फाइलमध्ये. लेजर (Sales, Cash, Output CGST ...) टॅलीमध्ये आधीपासून असले पाहिजेत."),
    "Lock the financial year {year}? Nothing new can be dated in it.": ("वित्तीय वर्ष {year} लॉक करें? इसमें कुछ नया नहीं जुड़ सकेगा।", "आर्थिक वर्ष {year} लॉक करायचे? त्यात काही नवीन जोडता येणार नाही."),
    "Unlock the financial year {year}?": ("वित्तीय वर्ष {year} अनलॉक करें?", "आर्थिक वर्ष {year} अनलॉक करायचे?"),
    "Financial year {year} is locked.": ("वित्तीय वर्ष {year} लॉक हो गया।", "आर्थिक वर्ष {year} लॉक झाले."),
    "Financial year {year} is unlocked.": ("वित्तीय वर्ष {year} अनलॉक हो गया।", "आर्थिक वर्ष {year} अनलॉक झाले."),
    "Select a year first.": ("पहले एक वर्ष चुनें।", "आधी एक वर्ष निवडा."),
    "Saved {sales} sale(s), {notes} credit note(s) and {purchases} purchase(s): {path}": ("{sales} बिक्री, {notes} क्रेडिट नोट और {purchases} खरीद सेव हुई: {path}", "{sales} विक्री, {notes} क्रेडिट नोट आणि {purchases} खरेदी सेव्ह झाली: {path}"),
    "A financial year looks like 25-26.": ("वित्तीय वर्ष ऐसा दिखता है: 25-26।", "आर्थिक वर्ष असे दिसते: 25-26."),
    "Only a financial year that has ended can be locked.": ("सिर्फ़ खत्म हो चुका वित्तीय वर्ष लॉक हो सकता है।", "फक्त संपलेले आर्थिक वर्ष लॉक करता येते."),
    "A reason is required to unlock a financial year.": ("वित्तीय वर्ष अनलॉक करने का कारण ज़रूरी है।", "आर्थिक वर्ष अनलॉक करण्याचे कारण आवश्यक आहे."),
    "The file could not be saved. Choose another folder.": ("फ़ाइल सेव नहीं हो सकी। दूसरा फ़ोल्डर चुनें।", "फाइल सेव्ह होऊ शकली नाही. दुसरा फोल्डर निवडा."),
    "From date": ("शुरू की तारीख", "सुरुवातीची तारीख"),
    "To date": ("आखिरी तारीख", "शेवटची तारीख"),
    "Search": ("खोजें", "शोधा"),
    "Bulk Edit": ("थोक बदलाव", "एकत्र बदल"),
    "Search by name, brand, category or code": ("नाम, ब्रांड, श्रेणी या कोड से खोजें", "नाव, ब्रँड, श्रेणी किंवा कोडने शोधा"),
    "Select all shown": ("दिखाए गए सभी चुनें", "दाखवलेले सर्व निवडा"),
    "Season (blank = leave)": ("मौसम (खाली = जैसा है)", "हंगाम (रिकामे = तसेच)"),
    "GST rate % (blank = leave)": ("GST दर % (खाली = जैसा है)", "GST दर % (रिकामे = तसेच)"),
    "Reorder level (blank = leave)": ("रीऑर्डर स्तर (खाली = जैसा है)", "रीऑर्डर पातळी (रिकामे = तसेच)"),
    "Leave the price": ("कीमत न बदलें", "किंमत बदलू नका"),
    "Set one new price (Rs)": ("सबकी एक नई कीमत (₹)", "सर्वांची एक नवी किंमत (₹)"),
    "Change by percent (+/-)": ("प्रतिशत से बदलें (+/-)", "टक्क्यांनी बदला (+/-)"),
    "Change to": ("बदलाव", "बदल"),
    "Preview the changes": ("बदलाव देखें", "बदल पहा"),
    "Apply": ("लागू करें", "लागू करा"),
    "Field": ("फ़ील्ड", "फील्ड"),
    "Old": ("पुराना", "जुने"),
    "New": ("नया", "नवे"),
    "Season": ("मौसम", "हंगाम"),
    "GST rate": ("GST दर", "GST दर"),
    "Reorder level": ("रीऑर्डर स्तर", "रीऑर्डर पातळी"),
    "Selling price": ("बिक्री कीमत", "विक्री किंमत"),
    "GST %": ("GST %", "GST %"),
    "Select products, choose what to change, and check the preview. Nothing is saved until you press Apply. Bills already made keep their old prices.": ("सामान चुनें, क्या बदलना है चुनें और बदलाव देखें। Apply दबाने तक कुछ सेव नहीं होता। बन चुके बिल पुरानी कीमत ही रखते हैं।", "वस्तू निवडा, काय बदलायचे ते निवडा आणि बदल पहा. Apply दाबेपर्यंत काही सेव्ह होत नाही. झालेली बिले जुन्याच किमती ठेवतात."),
    "{n} change(s) to apply.": ("{n} बदलाव लागू होंगे।", "{n} बदल लागू होतील."),
    "Nothing would change for the selected products.": ("चुने गए सामान में कुछ नहीं बदलेगा।", "निवडलेल्या वस्तूंमध्ये काही बदलणार नाही."),
    "Apply these changes to {n} product(s)?": ("ये बदलाव {n} सामान पर लागू करें?", "हे बदल {n} वस्तूंवर लागू करायचे?"),
    "Changed {n} product(s).": ("{n} सामान बदले गए।", "{n} वस्तू बदलल्या."),
    "Nothing to change.": ("बदलने के लिए कुछ नहीं है।", "बदलण्यासाठी काही नाही."),
    "GST rate must be a number.": ("GST दर एक संख्या होनी चाहिए।", "GST दर संख्या असावी."),
    "Reorder level must be a whole number.": ("रीऑर्डर स्तर पूरी संख्या होनी चाहिए।", "रीऑर्डर पातळी पूर्ण संख्या असावी."),
    "Type the new price or the percent.": ("नई कीमत या प्रतिशत लिखें।", "नवी किंमत किंवा टक्केवारी लिहा."),
    "The price or percent is not a valid number.": ("कीमत या प्रतिशत सही संख्या नहीं है।", "किंमत किंवा टक्केवारी योग्य संख्या नाही."),
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

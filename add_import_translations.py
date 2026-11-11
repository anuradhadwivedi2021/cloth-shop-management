"""Adds the Import Data texts to ui/translations/hi.json and mr.json.

Run once from the project folder:   python add_import_translations.py

Only texts that are not in the file yet are added; nothing existing is changed or removed.
Safe to run again.
"""

import json
from pathlib import Path

FOLDER = Path(__file__).resolve().parent / "ui" / "translations"

HI = {
    "Bring products in from a CSV or Excel (.xlsx) file. Each row is one size and colour of a product. Nothing is saved until the check shows no problems.":
        "CSV या Excel (.xlsx) फ़ाइल से प्रोडक्ट लाएँ। हर पंक्ति प्रोडक्ट का एक साइज़ और रंग है। जाँच में कोई समस्या न दिखे तब तक कुछ सेव नहीं होता।",
    "Check the file": "फ़ाइल की जाँच",
    "Choose file": "फ़ाइल चुनें",
    "Choose file and check it...": "फ़ाइल चुनें और जाँचें...",
    "Column": "कॉलम",
    "Example file": "नमूना फ़ाइल",
    "Import Data": "डेटा इम्पोर्ट",
    "Import now": "अभी इम्पोर्ट करें",
    "Import {n} variant(s) from this file?": "इस फ़ाइल से {n} वेरिएंट इम्पोर्ट करें?",
    "Imported {variants} variant(s) in {products} new product(s), with {pieces} piece(s) of opening stock.":
        "{products} नए प्रोडक्ट में {variants} वेरिएंट इम्पोर्ट हुए, शुरुआती स्टॉक {pieces} पीस।",
    "No file chosen.": "कोई फ़ाइल नहीं चुनी।",
    "Problem": "समस्या",
    "Problems file": "समस्याओं की फ़ाइल",
    "Problems found": "मिली समस्याएँ",
    "Row": "पंक्ति",
    "Save an example file": "नमूना फ़ाइल सेव करें",
    "Save the problems as a file...": "समस्याएँ फ़ाइल में सेव करें...",
    "Saved: {path}": "सेव हुआ: {path}",
    "The file is fine: {products} new product(s), {existing} existing product(s) getting new sizes, {variants} variant(s), {pieces} piece(s) of opening stock. Press Import now.":
        "फ़ाइल सही है: {products} नए प्रोडक्ट, {existing} मौजूदा प्रोडक्ट में नए साइज़, {variants} वेरिएंट, {pieces} पीस शुरुआती स्टॉक। \"अभी इम्पोर्ट करें\" दबाएँ।",
    "{bad} of {total} row(s) have problems. Nothing will be imported until they are fixed. Save the problems as a file, fix them in the file, and choose it again.":
        "{total} में से {bad} पंक्तियों में समस्या है। ठीक होने तक कुछ इम्पोर्ट नहीं होगा। समस्याएँ फ़ाइल में सेव करें, फ़ाइल में ठीक करें और दोबारा चुनें।",
    "This is required.": "यह ज़रूरी है।",
    "Enter a whole number.": "पूरी संख्या लिखें।",
    "GST must be between 0 and 100.": "GST 0 से 100 के बीच होना चाहिए।",
    "A code cannot contain spaces.": "कोड में खाली जगह नहीं हो सकती।",
    "You are not allowed to set cost prices.": "आपको खरीद कीमत डालने की अनुमति नहीं है।",
    "You are not allowed to set opening stock.": "आपको शुरुआती स्टॉक डालने की अनुमति नहीं है।",
    "This size and colour already exists for this product. Nothing is overwritten.":
        "इस प्रोडक्ट का यह साइज़ और रंग पहले से है। कुछ बदला नहीं जाएगा।",
}

MR = {
    "Bring products in from a CSV or Excel (.xlsx) file. Each row is one size and colour of a product. Nothing is saved until the check shows no problems.":
        "CSV किंवा Excel (.xlsx) फाइलमधून प्रोडक्ट आणा. प्रत्येक ओळ म्हणजे प्रोडक्टचा एक साईज आणि रंग. तपासणीत समस्या नसेपर्यंत काहीही सेव्ह होत नाही.",
    "Check the file": "फाइल तपासा",
    "Choose file": "फाइल निवडा",
    "Choose file and check it...": "फाइल निवडा आणि तपासा...",
    "Column": "कॉलम",
    "Example file": "नमुना फाइल",
    "Import Data": "डेटा इम्पोर्ट",
    "Import now": "आता इम्पोर्ट करा",
    "Import {n} variant(s) from this file?": "या फाइलमधून {n} व्हेरिएंट इम्पोर्ट करायचे?",
    "Imported {variants} variant(s) in {products} new product(s), with {pieces} piece(s) of opening stock.":
        "{products} नवीन प्रोडक्टमध्ये {variants} व्हेरिएंट इम्पोर्ट झाले, सुरुवातीचा स्टॉक {pieces} नग.",
    "No file chosen.": "कोणतीही फाइल निवडली नाही.",
    "Problem": "समस्या",
    "Problems file": "समस्यांची फाइल",
    "Problems found": "सापडलेल्या समस्या",
    "Row": "ओळ",
    "Save an example file": "नमुना फाइल सेव्ह करा",
    "Save the problems as a file...": "समस्या फाइलमध्ये सेव्ह करा...",
    "Saved: {path}": "सेव्ह झाले: {path}",
    "The file is fine: {products} new product(s), {existing} existing product(s) getting new sizes, {variants} variant(s), {pieces} piece(s) of opening stock. Press Import now.":
        "फाइल बरोबर आहे: {products} नवीन प्रोडक्ट, {existing} विद्यमान प्रोडक्टमध्ये नवीन साईज, {variants} व्हेरिएंट, {pieces} नग सुरुवातीचा स्टॉक. \"आता इम्पोर्ट करा\" दाबा.",
    "{bad} of {total} row(s) have problems. Nothing will be imported until they are fixed. Save the problems as a file, fix them in the file, and choose it again.":
        "{total} पैकी {bad} ओळींमध्ये समस्या आहे. त्या दुरुस्त होईपर्यंत काहीही इम्पोर्ट होणार नाही. समस्या फाइलमध्ये सेव्ह करा, फाइलमध्ये दुरुस्त करा आणि पुन्हा निवडा.",
    "This is required.": "हे आवश्यक आहे.",
    "Enter a whole number.": "पूर्ण संख्या लिहा.",
    "GST must be between 0 and 100.": "GST 0 ते 100 दरम्यान असावा.",
    "A code cannot contain spaces.": "कोडमध्ये मोकळी जागा असू शकत नाही.",
    "You are not allowed to set cost prices.": "तुम्हाला खरेदी किंमत टाकण्याची परवानगी नाही.",
    "You are not allowed to set opening stock.": "तुम्हाला सुरुवातीचा स्टॉक टाकण्याची परवानगी नाही.",
    "This size and colour already exists for this product. Nothing is overwritten.":
        "या प्रोडक्टचा हा साईज आणि रंग आधीपासून आहे. काहीही बदलले जाणार नाही.",
}


def merge(code, table) -> int:
    path = FOLDER / f"{code}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    for english, text in table.items():
        if english not in data:
            data[english] = text
            added += 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return added


if __name__ == "__main__":
    for code, table in (("hi", HI), ("mr", MR)):
        print(f"{code}.json: {merge(code, table)} text(s) added")

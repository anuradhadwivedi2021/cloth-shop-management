# core/localization.py

TRANSLATIONS = {
    "hi": {
        "billing": "बिलिंग काउंटर",
        "products": "उत्पाद और स्टॉक",
        "checkout": "चेकआउट और प्रिंट",
        "gst_report": "GST सारांश रिपोर्ट"
    },
    "mr": {
        "billing": "बिलिंग काउंटर",
        "products": "उत्पाद आणि स्टॉक",
        "checkout": "चेकआउट आणि प्रिन्ट",
        "gst_report": "GST सारांश अहवाल"
    }
}

def get_translation(key, lang="hi"):
    return TRANSLATIONS.get(lang, TRANSLATIONS["hi"]).get(key, key)
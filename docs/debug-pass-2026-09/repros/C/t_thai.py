import _harness
try:
    from core import textsim as ts
    import unicodedata
    samples = {
        "Thai(real,combining)": "การเริ่มต้นเซสชัน",
        "Thai(smoke-like)": "สวัสดีครับ",
        "Lao": "ສະບາຍດີ",
        "Devanagari": "नमस्ते",
        "Cyrillic": "привет",
        "Greek": "καλημέρα",
        "Arabic": "مرحبا",
        "Hebrew": "שלום",
    }
    for name, s in samples.items():
        ws = ts.word_set(s)
        print(f"{name:24} len={len(s):2} word_set_size={len(ws)}  {'EMPTY!!' if not ws else ''}")
finally:
    _harness.cleanup()

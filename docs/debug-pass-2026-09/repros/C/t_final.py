import _harness
try:
    from core import privacy as pv
    print("=== privacy case-sensitivity ===")
    for t in ["<private>SEK-ret</private>", "<PRIVATE>SEK-ret</PRIVATE>", "<Private>SEK-ret</Private>"]:
        cleaned = pv.clean_for_storage("safe " + t + " safe")
        print(f"{t!r:34} -> clean_for_storage: {cleaned!r}  LEAK={'SEK-ret' in cleaned}")
        print(f"{'':34}    has_private={pv.has_private('x '+t)}")
    # confirm neutralize doesn't catch <PRIVATE> either
finally:
    _harness.cleanup()

import _harness
try:
    from pathlib import Path
    import os, json
    from core import extractor as ex

    def rec(i, role="user", content=None):
        if content is None: content = f"msg {i}"
        return {"type":"message","message":{"role":role,"content":content}}

    d = Path(_harness.SB)

    print("=== BOM at start of transcript ===")
    p = d/"bom.jsonl"
    lines = [json.dumps(rec(i)) for i in range(3)]
    p.write_bytes(b"\xef\xbb\xbf" + ("\n".join(lines)).encode("utf-8"))
    w = ex.load_transcript_window(str(p))
    print("records read:", len(w.messages), " total_records:", w.total_records, " truncated:", w.truncated)
    print("first message present?", w.messages[0].get("message",{}).get("content") if w.messages else "NONE")

    print("\n=== CRLF transcript, small branch ===")
    p2 = d/"crlf.jsonl"
    p2.write_bytes(("\r\n".join([json.dumps(rec(i)) for i in range(4)])+"\r\n").encode())
    w2 = ex.load_transcript_window(str(p2))
    print("small branch: msgs", len(w2.messages), "total", w2.total_records, "trunc", w2.truncated)

    print("\n=== content as list of blocks ===")
    p3 = d/"blocks.jsonl"
    r = {"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"hello world this is content"}]}}
    p3.write_text(json.dumps(r)+"\n", encoding="utf-8")
    w3 = ex.load_transcript_window(str(p3))
    print("blocks msgs:", len(w3.messages))
    print("summarize:", ex.summarize_transcript(w3.messages)[:60])

    print("\n=== 0-byte transcript ===")
    p4 = d/"empty.jsonl"; p4.write_bytes(b"")
    w4 = ex.load_transcript_window(str(p4))
    print("empty:", w4.messages, w4.total_records, w4.truncated)

    print("\n=== directory as transcript ===")
    p5 = d/"adir"; p5.mkdir()
    w5 = ex.load_transcript_window(str(p5))
    print("dir:", w5.messages, w5.total_records)

    print("\n=== msg_count consistency: small vs truncated branch on SAME file ===")
    p6 = d/"big.jsonl"
    payload = [json.dumps(rec(i, content="x"*500)) for i in range(50)]
    blob = ("\n".join(payload)).encode()   # no trailing newline
    p6.write_bytes(blob)
    w_small = ex.load_transcript_window(str(p6))  # default tail 32MiB -> small branch
    w_trunc = ex.load_transcript_window(str(p6), head_records=5, tail_bytes=2000)  # force truncated
    print("small branch total_records:", w_small.total_records, "(file has 50 records, no trailing NL)")
    print("trunc branch total_records:", w_trunc.total_records)
    print("MATCH:", w_small.total_records == w_trunc.total_records == 50)

    print("\n=== truncated branch with BOM: does _count_records see the BOM'd first line? ===")
    p7 = d/"bigbom.jsonl"
    p7.write_bytes(b"\xef\xbb\xbf" + ("\n".join([json.dumps(rec(i,content="y"*400)) for i in range(40)])).encode())
    w7 = ex.load_transcript_window(str(p7), head_records=5, tail_bytes=1500)
    print("trunc+bom total_records:", w7.total_records, "(should be 40)")
finally:
    _harness.cleanup()

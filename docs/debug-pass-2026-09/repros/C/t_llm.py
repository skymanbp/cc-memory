import _harness
try:
    import json, time, threading, http.server, socketserver
    from pathlib import Path
    import llm.ccl_backend as b

    # stub server
    STATE = {"mode":"ok", "hits":[]}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            n = int(self.headers.get("Content-Length",0)); body=self.rfile.read(n)
            key = self.headers.get("x-api-key") or self.headers.get("Authorization","")
            STATE["hits"].append(key)
            if STATE["mode"]=="ok":
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"content":[{"type":"text","text":"RESPONSE-OK"}]}).encode())
            elif STATE["mode"]=="first400":
                # first caller key -> 400, others -> 200
                if "FAILKEY" in key:
                    self.send_response(400); self.end_headers(); self.wfile.write(b'{"error":"low credit"}')
                else:
                    self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                    self.wfile.write(json.dumps({"content":[{"type":"text","text":"OK-2ND"}]}).encode())
            elif STATE["mode"]=="drip":
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length","100000"); self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x"*10); self.wfile.flush(); time.sleep(0.5)
                except Exception: pass
    srv = socketserver.ThreadingTCPServer(("127.0.0.1",0), H); srv.daemon_threads=True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    b._ANTHROPIC_URL = f"http://127.0.0.1:{port}/v1/messages"

    print("=== (a) fall-through: caller FAILKEY 400 -> next candidate OK ===")
    STATE["mode"]="first400"; STATE["hits"]=[]
    # provide an env candidate via credentials file
    creds = Path(_harness.SB)/".claude"/".credentials.json"; creds.parent.mkdir(parents=True,exist_ok=True)
    creds.write_text(json.dumps({"claudeAiOauth":{"accessToken":"sk-ant-oat-GOODOAUTH","expiresAt": int(time.time()*1000)+10**9}}))
    out = b.call_llm("sys","user", api_key="sk-ant-api-FAILKEY", max_tokens=10, timeout=5)
    print("result:", out, "| hits:", [h[:20] for h in STATE["hits"]])

    print("\n=== (b) deadline bounds a dripping server (true wall clock) ===")
    STATE["mode"]="drip"; STATE["hits"]=[]
    t0=time.monotonic()
    try:
        b.call_llm("sys","user", api_key="sk-ant-api-ONLY", max_tokens=10, timeout=30, deadline=time.monotonic()+3.0)
        print("returned normally (unexpected)")
    except Exception as e:
        print("raised", type(e).__name__)
    dt=time.monotonic()-t0
    print(f"elapsed {dt:.2f}s (deadline 3s; must be small, not 30s)")

    print("\n=== (c) _MIN_LEG_S skip: deadline already passed ===")
    STATE["mode"]="ok"
    try:
        b.call_llm("s","u", api_key="sk-ant-api-X", timeout=30, deadline=time.monotonic()-1)
        print("returned (unexpected)")
    except RuntimeError as e:
        print("raised RuntimeError (all skipped):", str(e)[:70])
    srv.shutdown()
finally:
    _harness.cleanup()

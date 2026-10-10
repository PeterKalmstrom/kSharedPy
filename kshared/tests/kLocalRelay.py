"""A stand-in for the error relay on 127.0.0.1, for the tests in this folder. Never the real relay.

The path picks the answer:  /ok -> 202 {"ok":true}   /dup -> 202 {"duplicate":true}
                            /bad -> 400 {"error":"ProductName is missing"}   /busy -> 429 {"retryAfterSeconds":125}
Every request body is kept in Received (parsed JSON) in arrival order.
"""
import http.server
import json
import threading

ANSWERS = {
    "/ok": (202, {"ok": True}),
    "/dup": (202, {"duplicate": True}),
    "/bad": (400, {"error": "ProductName is missing"}),
    "/busy": (429, {"retryAfterSeconds": 125}),
}


class kRelayHandler(http.server.BaseHTTPRequestHandler):
    """Answers a POST from ANSWERS and records its body on the server."""

    def do_POST(self):
        Length = int(self.headers.get("Content-Length", "0") or 0)
        Body = self.rfile.read(Length).decode("utf-8")
        self.server.Received.append({"path": self.path, "body": json.loads(Body)})
        Status, Answer = ANSWERS.get(self.path, (500, {"error": "unknown path"}))
        Data = json.dumps(Answer).encode("utf-8")
        self.send_response(Status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(Data)))
        self.end_headers()
        self.wfile.write(Data)

    def log_message(self, Format, *Args):
        return  # keep test output clean


class kLocalRelay:
    """Start() / Stop() a relay on a free 127.0.0.1 port; Url(path) is its address."""

    def __init__(self):
        self.Server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), kRelayHandler)
        self.Server.Received = []
        self.Thread = threading.Thread(target=self.Server.serve_forever, daemon=True)

    def Start(self):
        self.Thread.start()
        return self

    def Stop(self):
        self.Server.shutdown()
        self.Server.server_close()

    def Url(self, Path):
        return f"http://127.0.0.1:{self.Server.server_address[1]}{Path}"

    @property
    def Received(self):
        return self.Server.Received

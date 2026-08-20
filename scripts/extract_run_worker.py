#!/usr/bin/env python3
"""One-off helper: extract /run comparison body from builtin_server.py."""
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "builtin_server.py"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
start, end = 4405, 7285
body = lines[start - 1 : end]
out = []
out.append("    def _execute_run_comparison(self, sink, qs, session_id, session_progress):\n")
out.append('        """Run comparison job (invoked from background thread)."""\n')
for line in body:
    if line.startswith("            "):
        out.append("        " + line[12:])
    else:
        out.append(line)
text = "".join(out)
text = text.replace("self.send_response", "sink.send_response")
text = text.replace("self.send_header", "sink.send_header")
text = text.replace("self.end_headers", "sink.end_headers")
text = text.replace("self.wfile.write", "sink.write")
Path(__file__).resolve().parent.parent.joinpath("_extracted_worker.py").write_text(
    text, encoding="utf-8"
)
print(f"extracted {len(body)} lines")

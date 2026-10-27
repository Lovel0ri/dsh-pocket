# dsh-pocket · static (personal mobile UI)
- Mock: `cd 04_tools/dsh-pocket && ./start.sh --mock` (default port 8766), open `http://<电脑IP>:8766/` — no login.
- Real: `cd 04_tools/dsh-pocket && ./start.sh` (or direct `python3 -m dsh_pocket --host 0.0.0.0 --port 8766 --dsh-home ~/.dsh`); only phone-created sessions in `~/.dsh-pocket/pocket.db` are listed.
- No login / pairing code / token / WS ticket: REST and WS are unauthenticated; trusted LAN only, never expose or port-forward the port.
- Known limits: safe Markdown subset (headings/lists/bold/inline code/fenced code/links), no model-config UI, one active workspace (switching restarts ACP), browse-created cwd depends on backend `POST /sessions {"cwd":…}`.

"""
gui.py  —  Community Directory AI Agent  (Desktop GUI)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dark dashboard GUI. Connects to server.py via MCP.

Run:
    python gui.py

Requires:
    pip install mcp requests dnspython python-dotenv ollama
"""

import asyncio
import json
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "granite4:350m ")

# ─────────────────────────────────────────────────────────
# COLOURS + FONTS
# ─────────────────────────────────────────────────────────

BG       = "#0a0e17"
SURFACE  = "#111827"
SURFACE2 = "#1a2235"
BORDER   = "#1e2d45"
ACCENT   = "#00d4ff"
ACCENT2  = "#7c3aed"
WARN     = "#f59e0b"
DANGER   = "#ef4444"
SUCCESS  = "#10b981"
TEXT     = "#e2e8f0"
MUTED    = "#64748b"

F_TITLE = ("Courier New", 15, "bold")
F_HEAD  = ("Courier New", 12, "bold")
F_MONO  = ("Courier New", 10)
F_SM    = ("Courier New", 9)


# ─────────────────────────────────────────────────────────
# JSON HELPERS
# ─────────────────────────────────────────────────────────

def _extract_json(text: str):
    """Parse JSON that may have WP debug prefix or be newline-delimited."""
    if not text:
        return text
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip WP debug prefix before first [ or {
    for ch in ('[', '{'):
        idx = text.find(ch)
        if idx > 0:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                pass
    # Newline-delimited JSON objects
    objects = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if objects:
        return objects if len(objects) > 1 else objects[0]
    return text


def _safe_text(text: str) -> str:
    parsed = _extract_json(text)
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed)


# ─────────────────────────────────────────────────────────
# ARG SANITISER  — fix Ollama passing schema objects as values
# and auto-fill fields from the listings cache
# ─────────────────────────────────────────────────────────

_BLANK = {"", "N/A", "Missing", None}

def sanitise_args(tool_name: str, raw_args: dict, cache: list) -> dict:
    # Step 1: strip schema objects {"type": "string"} passed as values
    clean = {}
    for k, v in raw_args.items():
        if isinstance(v, dict) and "type" in v and len(v) <= 3:
            continue          # schema object — drop it
        if v in _BLANK:
            continue          # empty / placeholder — drop it
        clean[k] = v

    # Step 2: for listing tools, resolve real listing data from cache
    # Priority: title match > ID match (Ollama often sends wrong IDs like 1)
    lid = clean.get("listing_id")
    lookup_name = (
        clean.get("listing_name") or clean.get("name") or
        clean.get("title") or clean.get("organisation") or ""
    )

    match = None
    # Always try title first — more reliable than the ID Ollama provides
    if lookup_name and cache:
        name_l = lookup_name.lower().strip()
        # Exact match
        match = next((r for r in cache if r.get("title", "").lower().strip() == name_l), None)
        if not match:
            # Fuzzy: search term contained in title
            match = next((r for r in cache if name_l in r.get("title", "").lower()), None)
        if not match:
            # Fuzzy: title contained in search term
            match = next((r for r in cache
                          if r.get("title", "").lower() in name_l and len(r.get("title","")) > 2),
                         None)
    # Fallback: ID match (only if no title match)
    if not match and lid and cache:
        match = next((r for r in cache if r.get("id") == lid), None)

    if match:
        if tool_name == "validate_listing":
            # Always overwrite — never trust Ollama's listing_id or contact values
            clean["listing_id"]   = match["id"]
            clean["listing_name"] = match["title"]
            clean["phone"]   = match.get("phone",   "") or ""
            clean["email"]   = match.get("email",   "") or ""
            clean["website"] = match.get("website", "") or ""
            for f in ("phone", "email", "website"):
                if clean[f] in _BLANK:
                    clean[f] = ""

        elif tool_name == "verify_listing_details":
            # Always overwrite contact fields from cache
            clean["listing_id"]      = match["id"]
            clean["name"]            = match["title"]
            clean["current_phone"]   = match.get("phone",   "N/A") or "N/A"
            clean["current_email"]   = match.get("email",   "N/A") or "N/A"
            clean["current_website"] = match.get("website", "N/A") or "N/A"
            for f in ("current_phone", "current_email", "current_website"):
                if clean[f] in _BLANK:
                    clean[f] = "N/A"

        elif tool_name in ("update_listing_meta", "notify_admin"):
            clean["listing_id"]   = match["id"]
            clean["listing_name"] = match["title"]

    # Step 3: required-field fallbacks
    if tool_name == "validate_listing":
        clean.setdefault("listing_name", f"Listing {clean.get('listing_id', '?')}")
        for f in ("phone", "email", "website"):
            clean.setdefault(f, "")

    if tool_name == "verify_listing_details":
        if "name" not in clean:
            clean["name"] = clean.pop("listing_name", clean.pop("title",
                           f"Listing {clean.get('listing_id', '?')}"))
        clean.setdefault("current_phone",   "N/A")
        clean.setdefault("current_email",   "N/A")
        clean.setdefault("current_website", "N/A")

    return clean


def flatten_schema(schema: dict) -> dict:
    """Strip anyOf / $defs from FastMCP schemas before sending to Ollama."""
    if not schema or "properties" not in schema:
        return schema
    flat = {}
    for name, prop in schema["properties"].items():
        entry = {"type": prop.get("type", "string")}
        if "description" in prop:
            entry["description"] = prop["description"]
        if "default" in prop:
            entry["default"] = prop["default"]
        flat[name] = entry
    return {"type": "object", "properties": flat, "required": schema.get("required", [])}


# ─────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an AI agent for a Hawke's Bay, New Zealand community service directory.

TOOLS AVAILABLE:
- get_listings: fetch all listings from WordPress
- validate_listing: check phone, email, website validity for one listing
- verify_listing_details: web search to check if contact details are current
- update_listing_meta: update a field in WordPress (only after user approval)
- notify_admin: alert the admin of a detected change
- audit_outdated: find listings not updated in >1 year (only for full audits)
- generate_report: produce an audit summary (only when user asks for full audit/report)

WORKFLOW A — user asks to "validate" contact details:
Step 1: Call get_listings with query=<name>. Use the EXACT values returned.
Step 2: Call validate_listing with listing_id, listing_name, phone, email, website from results.
Step 3: Report the result — status, score, and each issue clearly.
Step 4: Ask if the user wants to also verify online.

WORKFLOW B — user asks to "verify", "search online", "check if up to date", or confirms after Workflow A:
Step 1: Call get_listings with query=<name> ONLY if you don't already have the listing data in context.
        If you already fetched the listing this conversation, skip straight to Step 2.
Step 2: Call verify_listing_details with name, current_phone, current_email, current_website.
        Use values from get_listings — never values the user typed.
Step 3: Report discrepancies found. If any: call notify_admin, then ask user permission to update.
Step 4: Only call update_listing_meta after explicit user approval.

DECIDING WHICH WORKFLOW:
- "validate" / "check format" / "check details" → Workflow A
- "verify online" / "search online" / "check if current" / "yes" after being asked → Workflow B (skip straight to verify_listing_details)
- "validate AND verify" / "validate and search online" → Workflow A then B in sequence

NZ PHONE FORMAT RULES:
- Landline: 06 835 2154  (9 digits: 2-digit area code + 7 digits)
- Mobile:   021 123 4567 (10 digits: 3-digit prefix + 7 digits)
- Freephone: 0800 123 456 (10 digits: 0800 + 6 digits)
- MORE than 11 digits = ALWAYS invalid, flag it immediately regardless of spacing
- "inconsistent format" = valid digit count, wrong spacing/brackets → offer phone_normalised
- "invalid format" = wrong digit count → state stored count vs expected count
STRICT RULES:
- NEVER use listing_id=1 or any guessed id — the id MUST come from get_listings results
- NEVER use phone/email/website values from the user's message — use values from get_listings
- If user says "search online", "verify", or "yes" after a validation → call verify_listing_details IMMEDIATELY, do NOT call validate_listing again
- NEVER call generate_report or audit_outdated unless user explicitly asks for a full audit
- NEVER call notify_admin unless a real discrepancy was found by verify_listing_details
- NEVER update without explicit user permission
- Keep responses short — no markdown bold or bullet formatting
"""

AUDIT_PROMPT = (
    "Run the full yearly audit: call audit_outdated, then validate_listing and "
    "verify_listing_details for each listing found, then generate_report with results."
)


# ─────────────────────────────────────────────────────────
# MCP BRIDGE
# ─────────────────────────────────────────────────────────

class MCPBridge:

    def __init__(self, on_log, on_listings, on_stats, on_reply, on_tool, on_ready):
        self.on_log      = on_log
        self.on_listings = on_listings
        self.on_stats    = on_stats
        self.on_reply    = on_reply
        self.on_tool     = on_tool
        self.on_ready    = on_ready

        self._loop    = None
        self._session = None
        self._tools   = []
        self._msgs    = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._ready   = False
        self._cache   = []

    def start(self):
        t = threading.Thread(target=self._thread, daemon=True)
        t.start()

    def _thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        self.on_log("Connecting to MCP server…", "INFO")
        params = StdioServerParameters(
            command=sys.executable, args=["server.py"], env=os.environ.copy()
        )
        try:
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    self._session = session
                    await session.initialize()

                    mt = await session.list_tools()
                    self._tools = [
                        {"type": "function", "function": {
                            "name":        t.name,
                            "description": t.description,
                            "parameters":  flatten_schema(t.inputSchema),
                        }}
                        for t in mt.tools
                    ]
                    self.on_log(f"MCP ready — {len(self._tools)} tools loaded.", "OK")
                    self._ready = True
                    self.on_ready()
                    await self._fetch()
                    # Heartbeat — keeps the MCP session alive
                    while True:
                        await asyncio.sleep(30)
        except Exception as e:
            import traceback
            self.on_log(f"MCP error: {e}", "ERR")
            self.on_log(traceback.format_exc()[:300], "ERR")

    async def _fetch(self):
        self.on_log("Fetching listings from WordPress…", "INFO")
        try:
            res  = await self._session.call_tool("get_listings", {})
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            data = _extract_json(text)

            rows = None
            if isinstance(data, dict) and "listings" in data:
                rows = data["listings"]
            elif isinstance(data, list):
                rows = data

            if rows is not None:
                self._cache = rows
                self.on_listings(rows)
                self.on_stats({"total": len(rows), "valid": 0, "review": 0, "invalid": 0})
                self.on_log(f"Loaded {len(rows)} listings.", "OK")
            else:
                self.on_log(f"Could not parse listings: {str(data)[:120]}", "ERR")
        except Exception as e:
            self.on_log(f"Fetch error: {e}", "ERR")

    def fetch(self):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._fetch(), self._loop)

    async def _validate_row(self, row):
        """Directly validate a single row (from right-click or audit)."""
        phone   = row.get("phone",   "") or ""
        email   = row.get("email",   "") or ""
        website = row.get("website", "") or ""
        # Normalise placeholders
        if phone   in _BLANK: phone   = ""
        if email   in _BLANK: email   = ""
        if website in _BLANK: website = ""

        self.on_log(f"Validating: {row['title']}…", "INFO")
        try:
            res = await self._session.call_tool("validate_listing", {
                "listing_id":   row["id"],
                "listing_name": row["title"],
                "phone":        phone,
                "email":        email,
                "website":      website,
            })
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            d    = _extract_json(text)
            if isinstance(d, dict):
                s = d.get("status", "unknown")
                tag = "OK" if s == "valid" else ("WARN" if s == "review" else "ERR")
                issues = d.get("issues", [])
                msg = f"  {row['title']} — {s.upper()} (score {d.get('score',0)})"
                if issues:
                    msg += ": " + "; ".join(issues)
                self.on_log(msg, tag)
            return d
        except Exception as e:
            self.on_log(f"Validation error: {e}", "ERR")
            return None

    def validate_row(self, row):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._validate_row(row), self._loop)

    async def _run_audit(self):
        self.on_log("━━━ Full audit starting ━━━", "INFO")
        try:
            res  = await self._session.call_tool("audit_outdated", {"days_threshold": 365})
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            data = _extract_json(text)
            outdated = data.get("outdated", []) if isinstance(data, dict) else []
            self.on_log(f"Found {len(outdated)} outdated listings.", "INFO")

            results = []
            valid = review = invalid = 0
            for row in outdated:
                d = await self._validate_row(row)
                if d:
                    results.append(d)
                    s = d.get("status")
                    if s == "valid":    valid   += 1
                    elif s == "review": review  += 1
                    else:               invalid += 1

            total = data.get("total_listings", len(self._cache)) if isinstance(data, dict) else len(self._cache)
            self.on_stats({"total": total, "valid": valid, "review": review, "invalid": invalid})

            if results:
                rep  = await self._session.call_tool("generate_report", {"results": results})
                rtext = "\n".join(c.text for c in rep.content if hasattr(c, "text"))
                rd   = _extract_json(rtext)
                if isinstance(rd, dict):
                    self.on_log(f"Report saved: {rd.get('saved_to','?')}", "OK")

            self.on_log("━━━ Audit complete ━━━", "OK")
        except Exception as e:
            self.on_log(f"Audit error: {e}", "ERR")

    def run_audit(self):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._run_audit(), self._loop)

    async def _chat(self, user_input: str):
        import ollama
        self._msgs.append({"role": "user", "content": user_input})

        # Track tool results so we can override Ollama's reply with real data
        _validate_result = None   # last validate_listing result
        _verify_result   = None   # last verify_listing_details result
        _update_result   = None   # last update_listing_meta result

        try:
            resp = ollama.chat(model=OLLAMA_MODEL, messages=self._msgs, tools=self._tools)

            while resp.get("message", {}).get("tool_calls"):
                self._msgs.append(resp["message"])
                for tc in resp["message"]["tool_calls"]:
                    name     = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    args     = sanitise_args(name, raw_args, self._cache)
                    self.on_tool(name)
                    self.on_log(f"→ {name}({json.dumps(args)[:90]})", "AI")
                    try:
                        res  = await self._session.call_tool(name, args)
                        rtxt = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                        clean = _safe_text(rtxt)
                    except Exception as e:
                        clean = json.dumps({"error": str(e)})

                    # ── Intercept validate_listing ────────────────────────
                    if name == "validate_listing":
                        try:
                            _validate_result = json.loads(clean)
                        except Exception:
                            _validate_result = {"status": "unknown", "raw": clean}

                    # ── Intercept verify_listing_details ───────────────────
                    if name == "verify_listing_details":
                        try:
                            _verify_result = json.loads(clean)
                        except Exception:
                            _verify_result = {"has_discrepancy": False, "raw": clean}

                    # ── Intercept update_listing_meta ──────────────────────
                    if name == "update_listing_meta":
                        try:
                            _update_result = json.loads(clean)
                        except Exception:
                            _update_result = {"success": False, "raw": clean}
                        if isinstance(_update_result, dict) and _update_result.get("success") and _update_result.get("confirmed_in_db"):
                            field = _update_result.get("field", "field")
                            val   = _update_result.get("new_value", "")
                            self.on_log(f"WordPress UPDATED: {field} → {val}", "OK")
                        else:
                            self.on_log("WordPress NOT updated.", "WARN")

                    self._msgs.append({
                        "role": "tool", "content": f"TOOL RESULT: {clean}", "name": name
                    })
                    self.on_log(f"← {name}: {clean[:120]}", "OK")
                resp = ollama.chat(model=OLLAMA_MODEL, messages=self._msgs, tools=self._tools)

            final = resp["message"]["content"]
            self._msgs.append(resp["message"])

            # ── Override Ollama reply with real tool results ─────────
            # Priority: update > verify > validate
            # (verify must beat validate so "search online" after a prior validate works)
            if _update_result is not None:
                if _update_result.get("success") and _update_result.get("confirmed_in_db"):
                    field = _update_result.get("field", "field")
                    val   = _update_result.get("new_value", "")
                    lst   = _update_result.get("listing", "the listing")
                    self.on_reply(f"Done. {lst}: {field} updated to '{val}' in WordPress.")
                else:
                    lst = _update_result.get("listing", "the listing")
                    fld = _update_result.get("field", "the field")
                    self.on_reply(f"The update failed — WordPress was NOT changed for {lst} ({fld}).")
            elif _verify_result is not None:
                org          = _verify_result.get("organisation", "the listing")
                disc         = _verify_result.get("discrepancies", {})
                found        = _verify_result.get("web_found", {})
                method       = _verify_result.get("method", "")
                contact_page = _verify_result.get("contact_page", "")
                scraped_from = _verify_result.get("scraped_from", [])
                srcs         = _verify_result.get("sources_used", scraped_from)
                note         = _verify_result.get("note", "")
                fallback     = _verify_result.get("fallback_reason", "")
                err          = _verify_result.get("error", "")
                lines        = []

                METHOD_LABELS = {
                    "direct":       "scraped from website",
                    "google_cache": "scraped from Google Cache",
                    "archive_org":  "scraped from archive.org",
                    "social_media": "found on social media",
                }
                method_label = next(
                    (v for k, v in METHOD_LABELS.items() if k in method),
                    f"verified via {method}" if method else "verified"
                )

                best_phone = found.get("best_phone")
                all_phones = found.get("phones", [])

                if err:
                    lines.append(f"{org} — could not verify: {err}")
                elif note:
                    lines.append(f"{org} — {note}")
                elif disc:
                    lines.append(f"{org} — discrepancies found ({method_label}):")
                    for fld, diff in disc.items():
                        note_str = f" ({diff['note']})" if diff.get("note") else ""
                        lines.append(f"  • {fld}: stored '{diff['stored']}' → found '{diff['found']}'{note_str}")
                    src = contact_page or (srcs[0] if srcs else "")
                    if src:
                        lines.append(f"  Source: {src}")
                    if len(all_phones) > 1:
                        lines.append(f"  All phones on page: {', '.join(str(p) for p in all_phones if isinstance(p, str))}")
                    lines.append("Would you like me to update these in WordPress?")
                else:
                    src = contact_page or (scraped_from[0] if scraped_from else "")
                    lines.append(f"{org} — details confirmed ({method_label})" +
                                 (f": {src}" if src else "."))
                    if best_phone:
                        lines.append(f"  Phone:  {best_phone}")
                    elif found.get("phones"):
                        lines.append(f"  Phone:  {found['phones'][0]}")
                    if found.get("emails"):
                        lines.append(f"  Email:  {found['emails'][0]}")
                    if len(all_phones) > 1:
                        lines.append(f"  All phones found: {', '.join(str(p) for p in all_phones if isinstance(p, str))}")
                    if fallback:
                        lines.append(f"  Note: {fallback}")
                self.on_reply("\n".join(lines))
            elif _validate_result is not None:
                lst    = _validate_result.get("listing_name", "the listing")
                status = _validate_result.get("status", "unknown")
                score  = _validate_result.get("score",  "?")
                issues = _validate_result.get("issues", [])
                phone  = _validate_result.get("phone",  "N/A")
                pnorm  = _validate_result.get("phone_normalised")
                email  = _validate_result.get("email",  "N/A")
                site   = _validate_result.get("website", "N/A")
                label  = {"valid": "✓ Valid", "review": "⚠ Needs Review",
                          "invalid": "✗ Invalid"}.get(status, status)
                lines  = [f"{lst} — {label} (score {score}/100)"]
                phone_line = f"  Phone:   {phone}"
                if pnorm and pnorm != phone:
                    phone_line += f" → correct format: {pnorm}"
                lines.append(phone_line)
                lines.append(f"  Email:   {email}")
                lines.append(f"  Website: {site}")
                if issues:
                    lines.append("Issues:")
                    for iss in issues:
                        lines.append(f"  • {iss}")
                if status in ("review", "invalid"):
                    lines.append("Would you like me to search online to verify the correct details?")
                elif status == "valid":
                    lines.append("All contact details look correct.")
                self.on_reply("\n".join(lines))
            else:
                self.on_reply(final)

        except Exception as e:
            self.on_reply(f"Error: {e}")
            self.on_log(f"Chat error: {e}", "ERR")
    def chat(self, text: str):
        if self._loop and self._ready:
            asyncio.run_coroutine_threadsafe(self._chat(text), self._loop)
        else:
            self.on_reply("Agent not ready yet — please wait.")


# ─────────────────────────────────────────────────────────
# GUI  — uses pack throughout (no grid/pack mixing)
# ─────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Directory AI Agent — Hawke's Bay")
        self.geometry("1300x800")
        self.minsize(1100, 680)
        self.configure(bg=BG)

        self._data  = []
        self._stats = {"total": 0, "valid": 0, "review": 0, "invalid": 0}

        self._build_ui()
        self._start_bridge()

    # ── TOP-LEVEL LAYOUT ──────────────────────────────────
    # header (fixed height) → body (fill+expand)
    # body = sidebar (fixed 250px) | main (expand)

    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_sidebar(body)
        self._build_main(body)

    # ── HEADER ────────────────────────────────────────────

    def _build_header(self):
        hdr = tk.Frame(self, bg=SURFACE, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        row = tk.Frame(hdr, bg=SURFACE)
        row.pack(fill="both", expand=True, padx=18)

        ico = tk.Frame(row, bg=ACCENT2, width=32, height=32)
        ico.pack(side="left", pady=10)
        ico.pack_propagate(False)
        tk.Label(ico, text="⚙", bg=ACCENT2, fg="white", font=("Courier New", 15)).pack(expand=True)

        tk.Label(row, text="  Directory", bg=SURFACE, fg=TEXT, font=F_TITLE).pack(side="left")
        tk.Label(row, text="AI",          bg=SURFACE, fg=ACCENT, font=F_TITLE).pack(side="left")
        tk.Label(row, text=" Agent",      bg=SURFACE, fg=TEXT, font=F_TITLE).pack(side="left")

        sub = tk.Frame(row, bg=SURFACE2, padx=10, pady=4)
        sub.pack(side="right", pady=12)
        self._dot = tk.Label(sub, text="●", bg=SURFACE2, fg=MUTED, font=F_SM)
        self._dot.pack(side="left", padx=(0, 4))
        self._status = tk.Label(sub, text="Connecting…", bg=SURFACE2, fg=MUTED, font=F_SM)
        self._status.pack(side="left")

        tk.Label(row, text="Listdom / WordPress · Hawke's Bay NZ",
                 bg=SURFACE, fg=MUTED, font=F_SM).pack(side="right", padx=14)

    # ── SIDEBAR ───────────────────────────────────────────
    # packed LEFT with fixed width — no grid at all

    def _build_sidebar(self, body):
        sb = tk.Frame(body, bg=BG, width=250)
        sb.pack(side="left", fill="y", padx=(0, 10), pady=10)
        sb.pack_propagate(False)   # locks the 250px width

        # Summary card — _card_frame returns the inner content frame directly
        sc = self._card_frame(sb, "📊  Summary")
        for label_text, color, attr in [
            ("Total Listings", ACCENT,   "_s_total"),
            ("✓  Valid",        SUCCESS, "_s_valid"),
            ("⚠  Needs Review", WARN,    "_s_review"),
            ("✗  Invalid",      DANGER,  "_s_invalid"),
            ("Last Scan",       MUTED,   "_s_scan"),
        ]:
            row = tk.Frame(sc, bg=SURFACE)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label_text, bg=SURFACE, fg=MUTED, font=F_SM).pack(side="left")
            lbl = tk.Label(row, text="—", bg=SURFACE, fg=color, font=F_MONO)
            lbl.pack(side="right")
            setattr(self, attr, lbl)

        # Health bar — appended directly to sc (no winfo_children lookup)
        tk.Frame(sc, bg=BORDER, height=1).pack(fill="x", pady=(6, 4))
        bar_row = tk.Frame(sc, bg=SURFACE)
        bar_row.pack(fill="x")
        tk.Label(bar_row, text="Health", bg=SURFACE, fg=MUTED, font=F_SM).pack(side="left")
        self._hpct = tk.Label(bar_row, text="—", bg=SURFACE, fg=ACCENT, font=F_SM)
        self._hpct.pack(side="right")
        bg_bar = tk.Frame(sc, bg=BORDER, height=4)
        bg_bar.pack(fill="x", pady=(2, 0))
        self._hbar = tk.Frame(bg_bar, bg=ACCENT, height=4)
        self._hbar.place(relx=0, rely=0, relheight=1, relwidth=0)

        # Actions card
        ac = self._card_frame(sb, "⚡  Actions")
        self._btn_fetch = self._btn(ac, "↻  Refresh Listings", self._do_fetch,  ACCENT)
        self._btn_audit = self._btn(ac, "▶  Run Full Audit",    self._do_audit,  SUCCESS)

        # Yearly scheduler card
        yc = self._card_frame(sb, "🗓  Yearly Audit")
        tk.Label(yc, text="Scheduled: Jan 1st  09:00",
                 bg=SURFACE, fg=ACCENT, font=F_SM).pack(anchor="w", pady=(0, 6))
        self._btn_sched = self._btn(yc, "⏱  Scheduler Info", self._do_sched, WARN)

        for b in (self._btn_fetch, self._btn_audit, self._btn_sched):
            b.config(state="disabled")

    def _card_frame(self, parent, title):
        outer = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="x", pady=(0, 8))
        tk.Label(outer, text=title, bg=SURFACE, fg=MUTED, font=F_SM
                 ).pack(anchor="w", padx=12, pady=(8, 4))
        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", padx=12)
        inner = tk.Frame(outer, bg=SURFACE)
        inner.pack(fill="both", padx=12, pady=8)
        return inner

    def _btn(self, parent, text, cmd, color):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=SURFACE2, fg=color,
                      activebackground=BORDER, activeforeground=color,
                      font=F_SM, relief="flat", cursor="hand2",
                      highlightbackground=BORDER, highlightthickness=1,
                      padx=8, pady=5)
        b.pack(fill="x", pady=2)
        return b

    # ── MAIN AREA ─────────────────────────────────────────
    # packed RIGHT, fills remaining space
    # top: listings table (weight 2) | bottom: console + chat (weight 1)

    def _build_main(self, body):
        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True, pady=10)

        self._build_table(main)
        self._build_bottom(main)

    # ── LISTINGS TABLE ────────────────────────────────────

    def _build_table(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="x", pady=(0, 8))
        wrap.configure(height=300)
        wrap.pack_propagate(False)

        # Header bar
        hbar = tk.Frame(wrap, bg=SURFACE2)
        hbar.pack(fill="x")
        tk.Label(hbar, text="Community Listings", bg=SURFACE2, fg=TEXT,
                 font=F_HEAD).pack(side="left", padx=12, pady=8)

        self._search = tk.StringVar()
        self._search.trace_add("write", self._filter)
        e = tk.Entry(hbar, textvariable=self._search,
                     bg=SURFACE, fg=TEXT, insertbackground=ACCENT,
                     relief="flat", font=F_MONO, width=20,
                     highlightbackground=BORDER, highlightthickness=1)
        e.pack(side="right", padx=12, pady=8, ipady=3)
        tk.Label(hbar, text="🔍", bg=SURFACE2, fg=MUTED, font=F_SM).pack(side="right")

        # Treeview style
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("D.Treeview",
                     background=SURFACE, foreground=TEXT,
                     fieldbackground=SURFACE, rowheight=28,
                     font=F_MONO, borderwidth=0)
        s.configure("D.Treeview.Heading",
                     background=SURFACE2, foreground=MUTED,
                     font=F_SM, relief="flat")
        s.map("D.Treeview",
              background=[("selected", SURFACE2)],
              foreground=[("selected", ACCENT)])

        cols = ("title", "phone", "email", "website", "updated", "status")
        self._tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                   style="D.Treeview", selectmode="browse")
        self._tree.pack(fill="both", expand=True, side="left")

        cfg = [("Listing", 210), ("Phone", 130), ("Email", 185),
               ("Website", 155), ("Last Updated", 115), ("Status", 80)]
        for col, (hd, w) in zip(cols, cfg):
            self._tree.heading(col, text=hd)
            self._tree.column(col, width=w, anchor="w", minwidth=60)

        self._tree.tag_configure("valid",   foreground=SUCCESS)
        self._tree.tag_configure("review",  foreground=WARN)
        self._tree.tag_configure("invalid", foreground=DANGER)
        self._tree.tag_configure("normal",  foreground=TEXT)

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        vsb.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=vsb.set)

        # Right-click menu
        self._ctx = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=TEXT,
                             activebackground=BORDER, activeforeground=ACCENT,
                             font=F_SM)
        self._ctx.add_command(label="✓  Validate this listing",  command=self._ctx_validate)
        self._ctx.add_command(label="🔍  Verify online",          command=self._ctx_verify)
        self._ctx.add_command(label="💬  Ask agent about this",   command=self._ctx_ask)
        self._tree.bind("<Button-3>", self._show_ctx)
        self._tree.bind("<Button-2>", self._show_ctx)

    # ── BOTTOM: CONSOLE + CHAT ────────────────────────────

    def _build_bottom(self, parent):
        bot = tk.Frame(parent, bg=BG)
        bot.pack(fill="both", expand=True)   # expands to fill remaining space

        self._build_console(bot)
        self._build_chat(bot)

    def _build_console(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(side="left", fill="both", expand=True, padx=(0, 6))

        hbar = tk.Frame(wrap, bg=SURFACE2)
        hbar.pack(fill="x")
        tk.Label(hbar, text="🖥  Console", bg=SURFACE2, fg=TEXT, font=F_HEAD
                 ).pack(side="left", padx=12, pady=6)
        tk.Button(hbar, text="Clear", command=self._clear_console,
                  bg=SURFACE2, fg=MUTED, relief="flat", font=F_SM, cursor="hand2"
                  ).pack(side="right", padx=8)

        self._console = scrolledtext.ScrolledText(
            wrap, bg=BG, fg=TEXT, font=F_MONO,
            relief="flat", state="disabled", wrap="word")
        self._console.pack(fill="both", expand=True, padx=2, pady=2)
        for tag, color in [("INFO", ACCENT), ("OK", SUCCESS), ("WARN", WARN),
                            ("ERR", DANGER), ("AI", "#a78bfa"),
                            ("TIME", MUTED),  ("MSG", TEXT)]:
            self._console.tag_config(tag, foreground=color)

    def _build_chat(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(side="left", fill="both", expand=True)

        hbar = tk.Frame(wrap, bg=SURFACE2)
        hbar.pack(fill="x")
        tk.Label(hbar, text="💬  Ask the Agent", bg=SURFACE2, fg=TEXT, font=F_HEAD
                 ).pack(side="left", padx=12, pady=6)
        tk.Label(hbar, text="Ollama", bg=SURFACE2, fg=MUTED, font=F_SM
                 ).pack(side="right", padx=10)

        self._chat_log = scrolledtext.ScrolledText(
            wrap, bg=BG, fg=TEXT, font=F_MONO,
            relief="flat", state="disabled", wrap="word")
        self._chat_log.pack(fill="both", expand=True, padx=2, pady=2)
        self._chat_log.tag_config("you",   foreground=ACCENT)
        self._chat_log.tag_config("agent", foreground=SUCCESS)
        self._chat_log.tag_config("lbl",   foreground=MUTED)

        inp = tk.Frame(wrap, bg=SURFACE2)
        inp.pack(fill="x", padx=2, pady=(0, 2))
        self._input = tk.Entry(inp, bg=SURFACE, fg=TEXT, insertbackground=ACCENT,
                               relief="flat", font=F_MONO,
                               highlightbackground=BORDER, highlightthickness=1)
        self._input.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=6, ipady=5)
        self._input.bind("<Return>", self._send)
        tk.Button(inp, text="Send ➤", command=self._send,
                  bg=ACCENT2, fg="white", font=F_SM,
                  relief="flat", cursor="hand2", padx=10, pady=5
                  ).pack(side="right", padx=(0, 6), pady=6)

        self._append_chat("Agent",
            "Hi! I manage the Hawke's Bay community directory. "
            "I can validate contact details, check if they're up to date, "
            "and update WordPress with your approval. What would you like to do?",
            "agent")

    # ── BRIDGE CALLBACKS ──────────────────────────────────

    def _start_bridge(self):
        self._bridge = MCPBridge(
            on_log      = lambda m, t: self.after(0, self._log, m, t),
            on_listings = lambda rows: self.after(0, self._set_listings, rows),
            on_stats    = lambda s:    self.after(0, self._set_stats, s),
            on_reply    = lambda t:    self.after(0, self._agent_reply, t),
            on_tool     = lambda n:    self.after(0, self._log, f"Calling tool: {n}", "AI"),
            on_ready    = lambda:      self.after(0, self._set_ready),
        )
        self._bridge.start()

    def _log(self, msg: str, tag: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._console.config(state="normal")
        self._console.insert("end", ts + "  ", "TIME")
        self._console.insert("end", f"[{tag:<4}]  ", tag)
        self._console.insert("end", msg + "\n", "MSG")
        self._console.see("end")
        self._console.config(state="disabled")

    def _set_listings(self, rows):
        self._data = rows
        self._render(rows)

    def _render(self, rows):
        self._tree.delete(*self._tree.get_children())
        for r in rows:
            last = r.get("last_updated", "")
            try:
                last = datetime.fromisoformat(last[:19]).strftime("%Y-%m-%d")
            except Exception:
                pass
            status = r.get("_status", "")
            tag    = status if status in ("valid", "review", "invalid") else "normal"
            self._tree.insert("", "end", iid=str(r["id"]), values=(
                r.get("title",   ""),
                r.get("phone",   "N/A"),
                r.get("email",   "N/A"),
                r.get("website", "N/A"),
                last or "N/A",
                status or "—",
            ), tags=(tag,))

    def _filter(self, *_):
        q = self._search.get().lower()
        self._render(self._data if not q else [
            r for r in self._data
            if q in r.get("title",   "").lower()
            or q in r.get("email",   "").lower()
            or q in r.get("phone",   "").lower()
        ])

    def _set_stats(self, s):
        self._stats = s
        total = s.get("total", 0)
        self._s_total.config(  text=str(total))
        self._s_valid.config(  text=str(s.get("valid",   0)))
        self._s_review.config( text=str(s.get("review",  0)))
        self._s_invalid.config(text=str(s.get("invalid", 0)))
        self._s_scan.config(   text=datetime.now().strftime("%H:%M"))
        pct = int(s.get("valid", 0) / total * 100) if total else 0
        self._hpct.config(text=f"{pct}%")
        self._hbar.place(relwidth=pct / 100)

    def _set_ready(self):
        self._dot.config(fg=SUCCESS)
        self._status.config(fg=SUCCESS, text="Agent Active")
        for b in (self._btn_fetch, self._btn_audit, self._btn_sched):
            b.config(state="normal")

    def _agent_reply(self, text: str):
        # Remove "Thinking…" line
        self._chat_log.config(state="normal")
        content = self._chat_log.get("1.0", "end-1c")
        if "Thinking…" in content:
            lines = [ln for ln in content.splitlines() if "Thinking…" not in ln]
            self._chat_log.delete("1.0", "end")
            for ln in lines:
                self._chat_log.insert("end", ln + "\n")
        self._chat_log.insert("end", "Agent:  ", "lbl")
        self._chat_log.insert("end", text + "\n\n", "agent")
        self._chat_log.see("end")
        self._chat_log.config(state="disabled")

    # ── ACTIONS ───────────────────────────────────────────

    def _do_fetch(self):
        self._bridge.fetch()

    def _do_audit(self):
        if messagebox.askyesno("Run Full Audit",
            "This scans all listings, checks validity and searches the web "
            "for updated contact details.\n\nThis may take several minutes. Continue?"):
            self._bridge.run_audit()

    def _do_sched(self):
        messagebox.showinfo("Yearly Scheduler",
            "To run the yearly audit automatically on Jan 1st at 09:00, "
            "start the scheduler in a terminal:\n\n    python main.py --schedule")

    def _show_ctx(self, e):
        item = self._tree.identify_row(e.y)
        if item:
            self._tree.selection_set(item)
            self._ctx.post(e.x_root, e.y_root)

    def _selected_row(self):
        sel = self._tree.selection()
        if not sel:
            return None
        iid = sel[0]
        return next((r for r in self._data if str(r["id"]) == iid), None)

    def _ctx_validate(self):
        row = self._selected_row()
        if row:
            self._bridge.validate_row(row)

    def _ctx_verify(self):
        row = self._selected_row()
        if row:
            # Ask the agent to validate format first, then check online.
            # Do NOT pass the raw phone — let the agent fetch it via get_listings
            # so validate_listing can catch format errors before the web search.
            self._send_msg(
                f"Validate and verify the contact details for '{row['title']}': "
                f"first check if the phone, email and website format is valid, "
                f"then search online to check if the details are current."
            )

    def _ctx_ask(self):
        row = self._selected_row()
        if row:
            self._input.delete(0, "end")
            self._input.insert(0, f"Validate the contact details of {row['title']}")
            self._input.focus()

    # ── CHAT ──────────────────────────────────────────────

    def _send(self, *_):
        txt = self._input.get().strip()
        if txt:
            self._input.delete(0, "end")
            self._send_msg(txt)

    def _send_msg(self, txt: str):
        self._append_chat("You", txt, "you")
        self._append_chat("Agent", "Thinking…", "lbl")
        self._bridge.chat(txt)

    def _append_chat(self, sender, text, style):
        self._chat_log.config(state="normal")
        self._chat_log.insert("end", f"{sender}:  ", "lbl")
        self._chat_log.insert("end", text + "\n\n", style)
        self._chat_log.see("end")
        self._chat_log.config(state="disabled")

    def _clear_console(self):
        self._console.config(state="normal")
        self._console.delete("1.0", "end")
        self._console.config(state="disabled")


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()

"""
main.py  —  Community Directory AI Agent  (MCP Client)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
    python main.py              # interactive chat
    python main.py --scan       # run a full audit now
    python main.py --schedule   # start yearly scheduler (runs in background)

Install:
    pip install mcp ollama python-dotenv schedule
"""

import asyncio
import json
import os
import sys
import schedule
import time
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:0.8b")

SYSTEM_PROMPT = """You are an AI agent managing a Hawke's Bay, New Zealand community service directory built on WordPress with the Listdom plugin.

Your 5 responsibilities are:

1. CHECK VALIDITY — use validate_listing to check each listing's phone, email, and website.

2. CHECK IF UP-TO-DATE — use verify_listing_details to search the web for the organisation's current contact details and compare with what is stored.

3. PRESENT & ASK PERMISSION — if you find any incorrect or outdated details:
   - Clearly tell the user: "I found that [field] for [listing] is stored as [old] but the website shows [new]. Would you like me to update this?"
   - Only call update_listing_meta AFTER the user explicitly says yes.

4. ALERT ADMIN — whenever you detect a discrepancy (regardless of user decision), call notify_admin immediately.

5. YEARLY AUDIT — when asked to run a full audit, use audit_outdated to find all stale listings, then validate and verify each one, and finish with generate_report.

Always be specific about which listing has an issue and what the issue is.
Never update a listing without first asking the user for permission.
"""

FULL_AUDIT_PROMPT = """Please run the full yearly audit:
1. Call audit_outdated to find listings not updated in the past year
2. For every listing returned, call validate_listing to check phone, email, and website validity
3. For every listing with issues, call verify_listing_details to check if updated info exists online
4. For any discrepancy found, call notify_admin to alert the administrator
5. Present each discrepancy to the user and ask for update permission
6. After reviewing all listings, call generate_report with the full validation results"""


# ─────────────────────────────────────────────────────────
# AGENT LOOP
# ─────────────────────────────────────────────────────────

async def run_agent(session: ClientSession, initial_prompt: str = None):
    import ollama

    # Fetch tools from MCP server and convert to Ollama format
    mcp_tools    = await session.list_tools()
    ollama_tools = [
        {
            "type": "function",
            "function": {
                "name":        t.name,
                "description": t.description,
                "parameters":  t.inputSchema,
            },
        }
        for t in mcp_tools.tools
    ]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if not initial_prompt:
        print("\n Directory AI Agent Ready!")
        print(" Type your question or command. Type 'exit' to quit.\n")

    async def run_turn(user_input: str):
        """Send one user message, handle all tool calls, print final response."""
        messages.append({"role": "user", "content": user_input})

        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=ollama_tools,
        )

        # Keep calling tools until the model gives a plain text response
        while response.get("message", {}).get("tool_calls"):
            messages.append(response["message"])

            for tool_call in response["message"]["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_args = tool_call["function"]["arguments"]

                print(f"  → {tool_name}({json.dumps(tool_args)[:100]})")

                result      = await session.call_tool(tool_name, tool_args)
                result_text = "\n".join(
                    c.text for c in result.content if hasattr(c, "text")
                )

                messages.append({
                    "role":    "tool",
                    "content": f"TOOL RESULT: {result_text}",
                    "name":    tool_name,
                })

            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=ollama_tools,
            )

        final = response["message"]["content"]
        print(f"\n Agent: {final}\n")
        messages.append(response["message"])

    # Non-interactive (scan) mode
    if initial_prompt:
        await run_turn(initial_prompt)
        return

    # Interactive chat loop
    while True:
        try:
            user_input = input(" You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue
        await run_turn(user_input)


# ─────────────────────────────────────────────────────────
# SESSION WRAPPER
# ─────────────────────────────────────────────────────────

async def start_session(prompt: str = None):
    """Launch the MCP server subprocess and start the agent."""
    server_params = StdioServerParameters(
        command="python3",
        args=["server.py"],
        env=os.environ.copy(),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await run_agent(session, initial_prompt=prompt)


# ─────────────────────────────────────────────────────────
# YEARLY SCHEDULER  [Function 5]
# ─────────────────────────────────────────────────────────

def run_yearly_audit():
    """Triggered by the scheduler once a year. Runs the full audit non-interactively."""
    print(f"\n[{datetime.now()}] Running scheduled yearly audit...")
    asyncio.run(start_session(prompt=FULL_AUDIT_PROMPT))


def start_scheduler(month: int = 1, day: int = 1, run_time: str = "09:00"):
    from datetime import datetime

    # Format: MM-DD HH:MM  e.g. "01-01 09:00"
    schedule_str = f"{month:02d}-{day:02d} {run_time}"

    try:
        schedule.every().year.at(schedule_str).do(run_yearly_audit)
    except Exception as e:
        print(f"Schedule error: {e}")
        print(f"Falling back to Jan 1st 09:00")
        schedule.every().year.at("01-01 09:00").do(run_yearly_audit)

    next_run = schedule.next_run()
    print(f"\n Yearly audit scheduler started.")
    print(f" Scheduled for: {month:02d}/{day:02d} at {run_time} each year")
    print(f" Next run: {next_run}")
    print(f" Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(60)


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "chat"

    if mode == "--scan":
        asyncio.run(start_session(prompt=FULL_AUDIT_PROMPT))

    elif mode == "--schedule":
        # Parse optional --month MM --day DD --time HH:MM
        args = sys.argv[2:]
        def _arg(flag, default):
            try:
                return args[args.index(flag) + 1]
            except (ValueError, IndexError):
                return default

        month    = int(_arg("--month", 1))
        day      = int(_arg("--day",   1))
        run_time =     _arg("--time",  "09:00")
        start_scheduler(month=month, day=day, run_time=run_time)

    else:
        # Default: interactive chat
        asyncio.run(start_session())

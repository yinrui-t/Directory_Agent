import asyncio
import os
import ollama
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

async def run_local_agent():
    # Connect to MCP Server
    server_params = StdioServerParameters(

        command="python3",
        args=["server.py"],
        env=os.environ.copy()
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Get tools from Server
            mcp_tools = await session.list_tools()

            # Format for Ollama's API
            ollama_tools = []
            for tool in mcp_tools.tools:
                ollama_tools.append({
                    'type': 'function',
                    'function': {
                        'name': tool.name,
                        'description': tool.description,
                        'parameters': tool.inputSchema,
                    },
                })
            
            messages = [{'role': 'user', 'content': 'Fetch my community services and find any with missing phone numbers.'}]

            response = ollama.chat(
                model='granite4:350m-h',
                messages=messages,
                tools=ollama_tools,
            )
                
            if response.get('message', {}).get('tool_calls'):
                for tool_call in response['message']['tool_calls']:
                    print(f"LLM requesting tool: {tool_call['function']['name']}")

                    result = await session.call_tool(
                        tool_call['function']['name'],
                        tool_call['function']['arguments']
                    )

                    messages.append(response['message'])
                    messages.append({'role': 'tool', 'content':str(result.content)})

                    final_response = ollama.chat(model='granite4:350m-h', messages=messages)
                    print(f"Final Result: {final_response['message']['content']}")

if __name__ == "__main__":
    asyncio.run(run_local_agent())                

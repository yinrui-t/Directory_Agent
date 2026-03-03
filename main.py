import asyncio
import os
import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def chat_with_directory():
    server_params = StdioServerParameters(
        command="python3",
        args=["server.py"], 
        env=os.environ.copy()
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            mcp_tools = await session.list_tools()
            ollama_tools = [{
                'type': 'function',
                'function': {
                    'name': t.name,
                    'description': t.description,
                    'parameters': t.inputSchema,
                }
            } for t in mcp_tools.tools]

            # 1. MOVE SYSTEM PROMPT HERE AND REMOVE 'messages = []' BELOW
            messages = [
                {
                    "role": "system", 
                    "content": "You are a WordPress Manager. Use 'get_listings' to find data. Once a tool returns data, summarize it for the user accurately."
                }
            ]

            print("\n Directory AI Agent Ready!")

            while True:
                user_input = input("\n You: ").strip()
                if user_input.lower() in ['exit', 'quit']: break
                
                messages.append({'role': 'user', 'content': user_input})

                response = ollama.chat(
                    model='llama3.2',
                    messages=messages,
                    tools=ollama_tools,
                )

                if response.get('message', {}).get('tool_calls'):
                    # Add the model's intent to call the tool to history
                    messages.append(response['message'])

                    for tool_call in response['message']['tool_calls']:
                        print(f"  AI is calling: {tool_call['function']['name']}...")
                        
                        result = await session.call_tool(
                            tool_call['function']['name'], 
                            tool_call['function']['arguments']
                        )
                        
                        # 2. FEED THE RESULT BACK PROPERLY
                        messages.append({
                            'role': 'tool', 
                            'content': f"TOOL RESULT: {str(result.content)}",
                            'name': tool_call['function']['name']
                        })
                    
                    # 3. ASK THE MODEL TO FINALIZE WITH THE NEW DATA
                    final_response = ollama.chat(model='llama3.2', messages=messages)
                    print(f" AI: {final_response['message']['content']}")
                    messages.append(final_response['message'])
                else:
                    print(f" AI: {response['message']['content']}")
                    messages.append(response['message'])

if __name__ == "__main__":
    asyncio.run(chat_with_directory())
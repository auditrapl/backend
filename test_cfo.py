import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.schemas import CfoRequest
from app.services.cfo_agent import run_cfo_simulation_stream

req = CfoRequest(prompt="Inwestujemy 50k PLN w reklamy by zdobyc udzialy w rynku")
async def main():
    print("Testing CFO MoE Agent...")
    try:
        async for chunk in run_cfo_simulation_stream(req, "ce7f9e85-4eb5-43a9-a78b-d72b53e8e2fa"): # random uuid or hardcoded
            print(">>>", chunk)
    except Exception as e:
        print("CRITICAL ERROR:", e)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(main())

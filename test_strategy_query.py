import asyncio
import json
import os
from app.services.strategy_agent import generate_sql, DB_SCHEMA, execute_sql

os.environ["GEMINI_API_KEY"] = "MOCK" # It uses the one from env if not set

async def main():
    import os
    if "GEMINI_API_KEY" not in os.environ:
        print("Set GEMINI_API_KEY to test")
        # return
        
    prompt = "co jezeli dodam budzetu do administracji i zakupy 50000 miesiecznie"
    company_id = '8b92a1d3-fc83-492b-9831-803a20438998'
    
    print("Generating SQL...")
    sql = await generate_sql(prompt, DB_SCHEMA, company_id)
    print(f"Generated SQL:\n{sql}\n")
    
    print("Executing SQL...")
    try:
        res = await execute_sql(sql)
        print("Result:")
        print(json.dumps(res, default=str, indent=2))
    except Exception as e:
        print("Error executing SQL:", str(e))

if __name__ == "__main__":
    asyncio.run(main())

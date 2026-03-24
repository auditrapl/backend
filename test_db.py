import asyncio
import json
from sqlalchemy import text
from app.database import engine

async def main():
    async with engine.connect() as conn:
        print("Connected!")
        res1 = await conn.execute(text("SELECT id, name, monthly_limit FROM departments WHERE company_id = '8b92a1d3-fc83-492b-9831-803a20438998'"))
        deps = [dict(r) for r in res1.mappings()]
        print("DEPARTMENTS:")
        print(json.dumps(deps, default=str, indent=2))
        
        res2 = await conn.execute(text("SELECT id, name, nip, industry FROM companies WHERE id = '8b92a1d3-fc83-492b-9831-803a20438998'"))
        comps = [dict(r) for r in res2.mappings()]
        print("\nCOMPANIES:")
        print(json.dumps(comps, default=str, indent=2))
        
        res3 = await conn.execute(text("SELECT id, department_id, invoice_type, net_amount, issue_date FROM invoices WHERE company_id = '8b92a1d3-fc83-492b-9831-803a20438998' LIMIT 5"))
        invs = [dict(r) for r in res3.mappings()]
        
        res_count = await conn.execute(text("SELECT COUNT(id) as count FROM invoices WHERE company_id = '8b92a1d3-fc83-492b-9831-803a20438998'"))
        count = res_count.mappings().first()['count']
        
        print(f"\nINVOICES COUNT: {count}")
        print(json.dumps(invs, default=str, indent=2))
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
from webapp.app import rag_search
from fastapi import Request

class MockClient:
    host = "127.0.0.1"

class MockRequest:
    def __init__(self):
        self.client = MockClient()
        self.headers = {}

async def main():
    try:
        req = MockRequest()
        res = rag_search(req, "whats today")
        print("Success", res)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())

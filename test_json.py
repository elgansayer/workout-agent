import asyncio
from webapp.app import _dashboard_context, app
from fastapi.encoders import jsonable_encoder

def test():
    ctx = _dashboard_context(user_id=None)
    json_data = jsonable_encoder(ctx)
    print("Success encoding")

test()

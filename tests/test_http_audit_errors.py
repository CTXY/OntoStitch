import importlib
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import OpenAI, AsyncOpenAI


def test_html_502_is_recorded_without_becoming_json_contract_error(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    audit=importlib.import_module('run_graph_guided_test').audit_http
    class OfflineClient(httpx.Client):
        def __init__(self,**kwargs):
            super().__init__(transport=httpx.MockTransport(lambda _:httpx.Response(502,text='<html>Bad Gateway</html>')),**kwargs)
    with OfflineClient() as client:
        original_async=AsyncOpenAI(api_key='offline',http_client=httpx.AsyncClient())
        original_sync=OpenAI(api_key='offline',http_client=client)
        model=SimpleNamespace(root_client=original_sync,root_async_client=original_async)
        audit(model,tmp_path)
        response=model.root_client._client.post('https://offline.invalid',json={'messages':[]})
        assert response.status_code==502
        model.root_client.close()
        asyncio.run(model.root_async_client.close())
        asyncio.run(original_async.close())
    saved=json.loads(next(tmp_path.glob('*_response.json')).read_text())
    assert saved['status']==502 and 'Bad Gateway' in saved['body']['non_json_body']

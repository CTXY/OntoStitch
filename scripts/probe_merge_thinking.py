"""Bounded live compatibility probe; never reads or edits merge checkpoints."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage

from cortex.config import load_config
from cortex.config.models import LLMConfig
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.llm.providers import create_chat_model
from run_resilient_training import observe, transport_error_details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('native', 'explicit_none', 'extra_false'), required=True)
    parser.add_argument('--slot', type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env')
    cfg = load_config(root / 'configs/cortex.yaml').llm.resolve('graph_merge')
    keys = read_key_slots(root / 'configs/sql_training.keys')
    key = keys[args.slot].api_key
    settings = cfg.model_copy(update={'model': 'deepseek-v4-pro', 'api_key': key,
        'enable_thinking': False, 'reasoning_effort': None if args.mode == 'native' else 'none',
        'timeout': 90, 'max_retries': 0})
    host = urlparse(settings.base_url).hostname
    for var in ('NO_PROXY', 'no_proxy'):
        os.environ[var] = ','.join(dict.fromkeys([x for x in os.environ.get(var, '').split(',') if x] + [host]))
    model = create_chat_model(LLMConfig.model_validate(settings.model_dump()))
    if args.mode == 'extra_false':
        model.extra_body = {**(model.extra_body or {}), 'enable_thinking': False}
    model.streaming = model.stream_usage = True
    model.metadata = {'credential_slot': args.slot}
    folder = root / 'output/merge_thinking_disabled_probe_20261122' / (args.mode + '_' + datetime.now().strftime('%H%M%S'))
    folder.mkdir(parents=True)
    observe(model, folder)
    result = {'mode': args.mode, 'endpoint_host': host, 'model': settings.model, 'anonymous_key_slot': args.slot,
              'requested_thinking': 'disabled', 'requested_effort': settings.reasoning_effort, 'checkpoint_modified': False}
    start = time.monotonic()
    try:
        reply = model.bind(max_tokens=1024).invoke([SystemMessage('Return a short exact JSON answer; no explanation.'),
            HumanMessage('Compute 347 * 219. Return {"answer": integer}.')])
        reasoning = reply.additional_kwargs.get('reasoning_content') or ''
        result.update(status='returned', reasoning_characters=len(reasoning), content=reply.content,
            usage=reply.usage_metadata, finish_reason=reply.response_metadata.get('finish_reason'),
            response_model=reply.response_metadata.get('model_name'), thinking_actually_disabled=not bool(reasoning))
    except Exception as error:
        result.update(status='failed', errors=transport_error_details(error, [k.api_key.get_secret_value() for k in keys]))
    result['elapsed_seconds'] = round(time.monotonic() - start, 3)
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'folder': str(folder), **result}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

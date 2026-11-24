"""Per-run generation settings, independent of accepted ontology checkpoints."""
import json
from pathlib import Path


def configure_merge_model(model, folder):
    """Apply an explicitly verified disabled-thinking profile on every key rotation."""
    folder = Path(folder)
    path = folder.parent.parent / 'runtime_model.json'
    if not path.exists():
        return model
    profile = json.loads(path.read_text())
    if profile.get('thinking_mode') != 'disabled' or profile.get('reasoning_effort') != 'none':
        raise ValueError('runtime_model.json must request thinking_mode=disabled and reasoning_effort=none')
    model.reasoning_effort = 'none'
    model.extra_body = {**(model.extra_body or {}), 'thinking': {'type': 'disabled'}}
    model.metadata = {**(getattr(model, 'metadata', None) or {}), 'thinking_mode': 'disabled', 'reasoning_effort': 'none'}

    def verify_wire(request):
        payload = json.loads(request.content)
        if payload.get('thinking', {}).get('type') != 'disabled' or payload.get('reasoning_effort') != 'none':
            raise ValueError('Disabled-thinking profile was not preserved on the actual wire request')

    model.root_client._client.event_hooks['request'].append(verify_wire)
    configuration = folder / 'model_configuration.json'
    data = json.loads(configuration.read_text()) if configuration.exists() else {}
    data.update(requested_thinking='disabled', requested_reasoning_effort='none',
                disabled_thinking_profile=str(path))
    configuration.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return model

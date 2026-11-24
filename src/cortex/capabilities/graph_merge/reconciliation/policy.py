"""Role-specific reconciliation policy, with a stable shared prefix."""
import re
from importlib.resources import files
from cortex.capabilities.graph_merge.reconciliation.contract import SIGNALS

ROLE_FILES = {
    'writer': ('common.en.md', 'writer.en.md', 'writer-contract.en.md'),
    'assessment': ('common.en.md', 'assessment.en.md', 'assessment-contract.en.md'),
}
SEPARATOR = '\n\n---\n\n'


def prompt_assets():
    root = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('reconciliation')
    names = {f'prompts/{name}' for role in ROLE_FILES.values() for name in role}
    names.update(f'signals/{kind}.md' for kind in SIGNALS)
    from cortex.capabilities.graph_merge.reconciliation.presentation import USER_FILES
    names.update(USER_FILES)
    return {name: root.joinpath(name).read_text() for name in sorted(names)}


def signal_guidance(text, role):
    if role not in ROLE_FILES:
        raise ValueError('Unknown reconciliation role')
    parts = re.split(r'(?m)^(## (?:Writer|Assessment)(?:：|[ \t])[^\n]+)\n', text.rstrip())
    if (len(parts) != 5 or not re.match(r'## Writer(?:：|[ \t])', parts[1])
            or not re.match(r'## Assessment(?:：|[ \t])', parts[3])):
        raise ValueError('Signal policy needs exactly one Writer and Assessment section, in order')
    index = 1 if role == 'writer' else 3
    title, body = parts[0].rstrip().split('\n', 1)
    title += " — Handling specification" if role == 'writer' else " — Checking specification"
    return title + '\n' + body + '\n\n' + parts[index] + '\n' + parts[index + 1].strip()


def reconciliation_system(role, kinds):
    kinds = tuple(sorted(set(kinds)))
    if set(kinds) - set(SIGNALS):
        raise ValueError('Reconciliation requires explicit supported signals or required obligations')
    assets = prompt_assets()
    parts = [assets[f'prompts/{name}'].rstrip() for name in ROLE_FILES[role]]
    parts.extend(signal_guidance(assets[f'signals/{kind}.md'], role) for kind in kinds)
    return SEPARATOR.join(parts) + '\n'

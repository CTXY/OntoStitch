"""English, read-only guidance for identity, using the active merge policies."""
from importlib.resources import files
from langchain_core.tools import tool
from cortex.capabilities.graph_merge.llm.prompt_language import english_contract


def build_identity_guidance(context):
    @tool
    def read_guidance(topic: str) -> dict:
        """Read English concept, metric, generation_rule, event, relations, conflicts or operations semantics. Guidance informs identity; it does not authorize graph edits."""
        root=files('cortex.capabilities.graph_merge.llm.prompts')
        if topic in {'concept','metric','generation_rule','event'}:
            result={'guidance':root.joinpath('node_local',f'types/{topic}.en.md').read_text(),
                    'fields':english_contract({k:v.model_dump(mode='json') for k,v in context.schema.effective_node_fields(topic).items()})}
        elif topic in {'conflicts','operations'}:
            result={'guidance':root.joinpath('node_local','common-fields.en.md' if topic=='conflicts' else 'types/generation_rule.en.md').read_text()}
        elif topic=='relations':
            result={'guidance':root.joinpath('reconciliation','prompts/common.en.md').read_text()}
        else:
            return {'error':'Unknown guidance topic'}
        return context.log('read_guidance',{'topic':topic},result)
    return read_guidance

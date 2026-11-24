"""Controller failures shared by merge stages and local transactions."""

from cortex.capabilities.graph_merge.runtime.feedback import validation_feedback
from cortex.capabilities.graph_merge.graph.scope import scope_error


class IntegrationIncomplete(RuntimeError):
    """Unaccepted work is retained without publishing an incomplete graph."""


def failure_feedback(error):
    result = {'status': 'rejected_unapplied', 'error': validation_feedback(error), **scope_error(error)}
    for attribute in ('before', 'after', 'missing_sources', 'counterexamples'):
        if hasattr(error, attribute):
            result[attribute] = [v.model_dump(mode='json') if hasattr(v, 'model_dump') else v
                                 for v in getattr(error, attribute)]
    if getattr(error, 'details', None) is not None:
        result['details'] = error.details
    return result

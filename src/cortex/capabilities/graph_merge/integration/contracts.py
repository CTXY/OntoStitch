"""Isolate source-local predicate spellings before any semantic comparison."""


def isolate_contracts(existing, incoming, roots, *, prefix="input__"):
    contracts = {c.id: c for c in existing}
    mappings = {}
    for contract in incoming:
        key = contract.id
        if (key in roots and roots[key] != contract) or (
            key in contracts and contracts[key] != contract
        ):
            key = prefix + key
            while key in contracts or key in roots:
                key = prefix + key
        mappings[contract.id] = key
        contracts[key] = contract.model_copy(update={"id": key})
    return tuple(contracts.values()), mappings

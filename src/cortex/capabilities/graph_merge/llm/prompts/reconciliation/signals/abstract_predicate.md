# `abstract_predicate`: abstract predicates

- **Trigger, cause, and normal state**: Actual surviving edges use abstract base predicates such as `has_attribute`, possibly without stating the specific business role; specific predicates inheriting an abstract type are themselves valid.
- **General example (illustrative, not this round's graph records or evidence)**:
  - **Can change**: `Patient → Sex` can be changed to `has_sex` if the role is clear.
  - **Cannot infer**: A diagnosis stored in the `Examination` table does not by itself prove the subject should be changed to the examination record.

## Necessary local material
The flagged edge, its endpoint meanings and branches, relevant role assertions on that endpoint pair, and applicable predicate contracts. Read evidence if the business role is unclear; unrelated endpoint neighbors are outside this check.

## Writer judgment and modification
Verify endpoint meanings, the selected role relationships, and the given predicate contracts; when evidenced, fix the specific predicate, direction, or subject, and you may add real omitted role nodes. Without an accurate basis for the role, do not rename arbitrarily or mask it with `related_to`; when an existing node `conflict` is involved, retain the complete competing versions, clearly state any decisive dependency, and do not choose a branch in this round.

## Assessment check points
Check the basis for roles and subjects and the retention of old conditions, sources, scope, and metadata; a specific name by itself does not prove the fix is correct. Base judgments on this round's actual graph and real candidates, and do not treat general examples as source evidence.

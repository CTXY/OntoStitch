from enum import StrEnum


class NodeType(StrEnum):
    TABLE = "table"
    COLUMN = "column"
    DIMENSION_VALUE = "dimension_value"
    METRIC = "metric"
    CONCEPT = "concept"
    # Workflow/owner naming alias. Persisted assets always serialize as ``concept``.
    SEMANTIC_MAPPING = "concept"
    GENERATION_RULE = "generation_rule"
    EXAMPLE = "example"


class SourceType(StrEnum):
    DATABASE = "database"
    QA = "qa"
    DOCUMENT = "document"
    MANUAL = "manual"
    INFERRED = "inferred"


class UnitType(StrEnum):
    NONE = "NONE"
    NUMBER = "NUMBER"
    CURRENCY = "CURRENCY"
    PERCENT = "PERCENT"
    TIME = "TIME"
    RATIO = "RATIO"


class SemanticRole(StrEnum):
    MEASURE = "MEASURE"
    DIMENSION = "DIMENSION"
    TIME = "TIME"
    FLAG = "FLAG"
    ID = "ID"
    TEXT = "TEXT"
    UNKNOWN = "UNKNOWN"


class KeyRole(StrEnum):
    PRIMARY_KEY = "PRIMARY_KEY"
    FOREIGN_KEY = "FOREIGN_KEY"
    NONE = "NONE"


class JoinCardinality(StrEnum):
    MANY_TO_ONE = "N:1"
    ONE_TO_MANY = "1:N"
    ONE_TO_ONE = "1:1"


class JoinType(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    INNER = "INNER"
    FULL = "FULL"
    FULL_OUTER = "FULL_OUTER"

"""Tool addresses and human-readable relation endpoints are separate contracts."""

from urllib.parse import quote


def object_id(kind, *names):
    return kind + "." + ".".join(quote(n, safe="").replace(".", "%2E") for n in names)


def column_reference(table, column):
    def component(name):
        return (
            '"' + name.replace('"', '""') + '"' if "." in name or '"' in name else name
        )

    return component(table) + "." + component(column)

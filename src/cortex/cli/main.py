import click

from cortex.cli.online_context import online_context_cli
from cortex.cli.retrieve import retrieve_cli
from cortex.cli.sql_training import (
    graph_merge_cli,
    sql_extraction_cli,
    sql_training_cli,
)


@click.group()
def main():
    """AutoOnto NL2SQL experiment commands."""


main.add_command(sql_training_cli)
main.add_command(sql_extraction_cli)
main.add_command(graph_merge_cli)
main.add_command(online_context_cli)
main.add_command(retrieve_cli)

if __name__ == "__main__":
    main()

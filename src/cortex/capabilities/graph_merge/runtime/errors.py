"""Controller failures shared by merge stages and local transactions."""


class IntegrationIncomplete(RuntimeError):
    """Unaccepted work is retained without publishing an incomplete graph."""

class ToolError(Exception):
    """Expected tool failure carrying a public, credential-free error code."""

    def __init__(self, code: str, *, result: dict | None = None):
        super().__init__(code)
        self.result = result if result is not None else {}

class ModelError(Exception):
    def __init__(self, message: str, *, incomplete=False):
        super().__init__(message)
        self.incomplete = incomplete

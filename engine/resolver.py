class DependencyResolver:
    """Resolver placeholder used until the registry resolver is implemented."""

    def __init__(self, registry_url: str):
        self.registry_url = registry_url

    def resolve(self, pipeline_config: dict) -> dict:
        return {
            "pipeline": pipeline_config.get("name"),
            "dependencies": {},
        }

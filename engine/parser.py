class PipelineParser:
    """Small parser placeholder so the engine can import and run."""

    allowed_top_level = {"name", "version", "dependencies", "jobs", "artifacts"}

    def parse_and_validate(self, pipeline_config: dict) -> dict:
        if not isinstance(pipeline_config, dict):
            raise ValueError("Pipeline YAML must be a mapping")

        for field in ["name", "version", "jobs"]:
            if field not in pipeline_config:
                raise ValueError(f"Missing required field: {field}")

        unknown = set(pipeline_config) - self.allowed_top_level
        if unknown:
            raise ValueError(f"Unknown field: {sorted(unknown)[0]}")

        if not isinstance(pipeline_config["jobs"], dict) or not pipeline_config["jobs"]:
            raise ValueError("jobs must be a non-empty mapping")

        return pipeline_config

# Legacy installation fixtures

These are published installation-root templates, not snapshots regenerated from the current template. Only the asset-bucket origin and release version are replaced with the existing `<BUCKET_PLACEHOLDER>` and `<VERSION_PLACEHOLDER>` tokens.

- [v4.21.1 source](https://datadog-cloudformation-template-quickstart.s3.amazonaws.com/aws/v4.21.1/main_agent_installation.yaml): raw SHA256 `2db5c977976379036f74cb02d9e0629f4282279bf159424cc7ff157af42b67db`.
- [v4.22.0 source](https://datadog-cloudformation-template-quickstart.s3.amazonaws.com/aws/v4.22.0/main_agent_installation.yaml): raw SHA256 `ccc887c14e864ea191b170a1b60b1146f3e2476eddebcfb51209021a1e81f6ff`.

The launcher records separate normalized YAML/JSON fingerprints for each release. It normalizes the matching release under one approved origin: either the configured candidate origin or the official published origin. Arbitrary origins, mixed-origin dependencies, pre-normalized placeholder URLs and version ranges are not accepted. Recognition determines the frozen permissions child's original version and URL, both of which remain pinned on adoption and subsequent requests.

# Feedback task starting environment

Upstream: https://github.com/fastapi/fastapi
Upstream commit: `84d5f8ea759355e26d5a36f9c5c5652df924328e`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests/test_security_oauth2.py tests/test_dependency_security_overrides.py tests/test_security_http_basic_optional.py tests/test_response_model_invalid.py tests/test_dependency_yield_scope.py
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `33 passed in 0.46s`
Whole-command wall time: 0.884 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.

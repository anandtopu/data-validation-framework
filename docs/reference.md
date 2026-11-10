# Python API

The supported import surface is `dvf.api`, `dvf.plugins`, `dvf.models`, and the notebook helper `dvf.debug`. `dvf.run` and `dvf.validate_dataframe` are also available at the top level. Everything else is internal and may change in any release; the guarantees are in [Plugins](plugins.md#compatibility).

```python
import dvf
from dvf import api

output = api.run("dvf.yaml", only=["table:customers"])
print(output.result.status, api.exit_code(output.result))

valid, quarantine, result = dvf.validate_dataframe(df, [{"type": "not_null", "columns": ["id"]}])
```

| Module | Use it to |
|---|---|
| [`dvf.api`](#dvf.api) | Load and validate configurations, run them, compare datasets ad hoc, render reports, explain and trace, load earlier runs, discover and generate configurations, validate DataFrames |
| [`dvf.plugins`](#dvf.plugins) | Write checks, connectors, normalizers, reporters, notifiers, secret providers, sinks, and hooks ([Plugins](plugins.md)) |
| [`dvf.models`](#dvf.models) | Read results (`RunResult` and the models inside it), canonical types, and shared enums ([Results schema](reference/results.md)) |
| [`dvf.debug`](#dvf.debug) | Load a run in a notebook and look at its mismatches ([Debugging](debugging.md#in-a-notebook)) |

::: dvf.api

::: dvf.plugins

::: dvf.models

::: dvf.debug

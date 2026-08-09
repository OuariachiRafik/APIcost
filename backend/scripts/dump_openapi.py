"""Write the dashboard API's OpenAPI schema to a file.

BUILD_SPEC §8: "the OpenAPI schema is the source of truth for the generated
TypeScript client". This is the first half of that — it imports the app and
serialises `app.openapi()` without starting a server, so `make api-types` needs
no running stack and works in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    from apicost.main_api import app

    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    schema = app.openapi()

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")

    print(
        f"wrote {destination} — {len(schema['paths'])} paths, "
        f"{len(schema.get('components', {}).get('schemas', {}))} schemas"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

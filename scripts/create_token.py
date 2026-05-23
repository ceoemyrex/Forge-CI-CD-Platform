#!/usr/bin/env python3
"""Create the first Forge auth token."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.auth import create_token

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "admin"
    token = create_token(name)
    print(f"FORGE_TOKEN={token}")
    print(f"Token identity: {name}")

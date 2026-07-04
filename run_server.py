"""Launcher that pins cwd/sys.path to the project root so the `app` package
resolves correctly regardless of where the process is started from."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8020"))
    # host="localhost" binds both 127.0.0.1 and ::1 (Windows browsers may use IPv6)
    uvicorn.run("app.main:app", host="localhost", port=port)

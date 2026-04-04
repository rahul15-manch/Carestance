import sys
import os
import traceback

# Add the project root to the sys.path so Vercel can find the 'app' module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app.main import app
except Exception as e:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    app = FastAPI()
    error_info = traceback.format_exc()
    @app.get("/{path:path}")
    async def catch_all(path: str):
        return HTMLResponse(content=f"<html><body><h1>Startup Error</h1><pre>{error_info}</pre></body></html>", status_code=500)

if __name__ == "__main__":
    print("Vercel wrapper initialized.")

# This file acts as the entry point for Vercel
# Vercel looks for an 'app' object in a file in the api/ directory

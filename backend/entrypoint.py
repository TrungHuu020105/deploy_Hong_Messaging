"""VPS entry point: the deployment image always uses web mode."""
from backend.api import create_app

app = create_app(mode="web")

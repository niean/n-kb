from app.composition import build_services
from app.config import Settings
from app.interfaces.mcp.server import create_mcp_server


def create_stdio_server(settings: Settings | None = None):
    resolved_settings = settings or Settings()
    services = build_services(resolved_settings)
    return create_mcp_server(services.retrieval, services.health, resolved_settings)


def main() -> None:
    server = create_stdio_server()
    server.run(transport="stdio")

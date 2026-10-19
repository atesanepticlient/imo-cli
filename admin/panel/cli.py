import os
import typer

def main(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    init_db: bool = typer.Option(False, "--init-db", help="Create tables then exit"),
):
    """IMO control server and admin panel."""
    if init_db:
        from .server import initialize_database
        initialize_database()
        print("Authentication database tables are ready.")
        return
    if not os.getenv("IMO_ADMIN_PASSWORD"):
        raise typer.BadParameter("Set IMO_ADMIN_PASSWORD before starting the control panel.")
    import uvicorn
    print(f"Admin panel: http://{host}:{port}/admin")
    uvicorn.run("imo_admin.server:app", host=host, port=port, reload=False)

def run():
    typer.run(main)
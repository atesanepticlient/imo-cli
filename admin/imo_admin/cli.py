import typer

def main(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address"),
    port: int = typer.Option(8765, "--port", envvar="PORT", min=1, max=65535),
    init_db: bool = typer.Option(False, "--init-db", help="Create tables/admin account then exit"),
):
    """IMO control server and admin panel."""
    if init_db:
        from .server import initialize_database
        initialize_database()
        print("Authentication database is ready.")
        return
    import uvicorn
    uvicorn.run("imo_admin.server:app", host=host, port=port, reload=False)

def run():
    typer.run(main)
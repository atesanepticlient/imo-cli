"""imo — IMO automation CLI (uiautomator2, concurrent, no Appium server).

Usage:
  imo devices                 List connected Android devices.
  imo open <app> [--all|--serial S]   Launch an app on one or all devices.
  imo run [--json FILE]       Run the IMO workflow on all devices concurrently.
  imo run --device S          Run the workflow on one device.
  imo disconnect S            Gracefully stop a running device workflow.
  imo mirror [--port P]       Serve live MJPEG screens for all devices.
  imo report [--json FILE]    Print a final report for a past run.
  imo init                    Install the uiautomator2 agent on all devices.
  imo help                    Show this help text.
  imo doctor                  Diagnose adb/devices/agent setup.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .auth import AuthenticationError, login as auth_login, logout as auth_logout, require_auth
from .adb import (
    ADB_PATH,
    KNOWN_APPS,
    connected_device_ids,
    get_device,
    is_package_installed,
    list_devices,
    open_app,
)
from .constants import CL_MESSAGE_ROOT, DICE_EMOJI, IMO_PACKAGE, MULTISPACE_PACKAGE, THUMB_IDS
from .control import start_disconnect_server, start_mirror_server
from .dashboard import Dashboard
from .logs import NdJsonWriter, make_log
from .workflow import (
    request_disconnect,
    run_all_devices,
    run_imo_workflow_on_device,
    workflow_checkpoint_emit,
)

# Windows consoles default to the ANSI codepage (e.g. cp1252), which mangles
# the em-dashes/accents in our output. Force UTF-8 so the help text and logs
# render correctly regardless of the terminal's code page.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

app = typer.Typer(
    name="imo",
    help="Fast, concurrent IMO automation CLI on uiautomator2.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


@app.callback()
def authentication_gate(ctx: typer.Context):
    """Require a valid token/IP pair for each operational command."""
    if ctx.invoked_subcommand in {"login", "logout", "admin", "version", "help"}:
        return
    if ctx.invoked_subcommand:
        require_auth()


@app.command()
def version():
    """Show version."""
    console.print(f"imo-cli {__version__}")


@app.command()
def login():
    """Sign in with an administrator-issued key (only needed once)."""
    auth_login(console)


@app.command()
def logout():
    """Remove this computer's securely stored login token."""
    auth_logout(console)


@app.command()
def admin(
    host: str = typer.Option("127.0.0.1", "--host", help="Control panel bind address"),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Control panel port"),
    init_db: bool = typer.Option(False, "--init-db", help="Create the authentication tables then exit"),
):
    """Start the administrator panel, or create its database tables."""
    from .server import initialize_database

    if init_db:
        initialize_database()
        console.print("[green]Authentication database tables are ready.[/green]")
        return
    if not __import__("os").getenv("IMO_ADMIN_PASSWORD"):
        raise typer.BadParameter("Set IMO_ADMIN_PASSWORD before starting the control panel.")
    import uvicorn

    console.print(f"[cyan]Admin panel: http://{host}:{port}/admin[/cyan]")
    uvicorn.run("imocli.server:app", host=host, port=port, reload=False)


HELP_TEXT = """\
[b]imo[/b] — concurrent IMO Multi Space automation on uiautomator2.

[b]Commands[/b]
  [cyan]imo devices[/cyan]                  List connected Android devices.
  [cyan]imo open <app> --all|--serial S[/cyan]  Launch an app (facebook, facebook_lite,
                          messenger, instagram, whatsapp) on all or one device.
  [cyan]imo run[/cyan]                       Run the IMO workflow on every connected
                          device [b]concurrently[/b] (one thread per device).
  [cyan]imo run --device S[/cyan]            Run the workflow on just one device.
  [cyan]imo run --json file.ndjson[/cyan]   Run and stream NDJSON events to a file.
  [cyan]imo run --mirror[/cyan]              Also serve live MJPEG screen streams.
  [cyan]imo disconnect S[/cyan]              Gracefully stop one device's workflow.
  [cyan]imo mirror --port P[/cyan]           Serve live screens at
                          http://127.0.0.1:P/<serial>
  [cyan]imo report file.ndjson[/cyan]        Print a report from a past --json run.
  [cyan]imo init[/cyan]                      Install the uiautomator2 agent on devices.
  [cyan]imo doctor[/cyan]                    Diagnose adb, devices, agent and Multi Space setup.
  [cyan]imo login[/cyan]                     Sign in with an administrator-issued key.
  [cyan]imo logout[/cyan]                    Remove this computer's saved login token.
  [cyan]imo admin --init-db[/cyan]           Create authentication tables.
  [cyan]imo admin[/cyan]                     Start the local control panel.

[b]What it does[/b]
  For each device, the workflow:
    1. Launches Multi Space.
    2. Opens every cloned IMO tile (imo, imo(1), imo(2), imo(3), ...).
    3. Inside each instance: dismisses popups, opens every pinned chat and
       likes recent videos/photos.
    4. Switches accounts and repeats until all accounts are processed.
    5. Returns cleanly to Multi Space and moves to the next clone tile.
"""


@app.command()
def help():
    """Show help text."""
    console.print(HELP_TEXT)


@app.command()
def doctor():
    """Check adb, devices, uiautomator2 agent and Multi Space setup."""
    ok = True

    # 1. adb binary
    console.print("[bold]Checking adb...[/bold]")
    try:
        version_out = subprocess.run([ADB_PATH, "version"], capture_output=True, text=True, timeout=10).stdout.strip()
        console.print(f"  [green]OK[/green] adb found: {version_out.splitlines()[0] if version_out else ADB_PATH}")
    except Exception as e:
        ok = False
        console.print(f"  [red]FAIL[/red] adb not usable: {e}")

    # 2. devices
    console.print("[bold]Checking devices...[/bold]")
    try:
        rows = list_devices()
    except Exception as e:
        ok = False
        console.print(f"  [red]FAIL[/red] could not list devices: {e}")
        rows = []
    if not rows:
        console.print("  [yellow]WARN[/yellow] no devices connected (check USB cable + debugging)")
    for d in rows:
        state_mark = "[green]OK[/green]" if d.state == "device" else "[red]FAIL[/red]"
        if d.state != "device":
            ok = False
        console.print(f"  {state_mark} {d.serial} ({d.model}, {d.manufacturer}, Android {d.androidVersion or '?'}, battery {d.battery}%) — state: {d.state}")

    # 3. Multi Space package
    if rows:
        console.print("[bold]Checking Multi Space...[/bold]")
        for d in rows:
            if d.state != "device":
                continue
            ms_ok = is_package_installed(d.serial, MULTISPACE_PACKAGE)
            if ms_ok:
                console.print(f"  [green]OK[/green] {d.serial}: Multi Space package accessible")
            else:
                console.print(f"  [yellow]WARN[/yellow] {d.serial}: Multi Space package not visible to adb shell; run uses the current foreground user")

    # 4. uiautomator2 agent
    if rows:
        console.print("[bold]Checking uiautomator2 agent...[/bold]")
        for d in rows:
            if d.state != "device":
                continue
            try:
                import uiautomator2 as u2

                dev = u2.connect_usb(d.serial)
                # Forces the uiautomator2 server to start (pushes u2.jar if
                # needed); app_current() confirms the server is reachable.
                info = dev.info
                pkg = dev.app_current()["package"]
                console.print(f"  [green]OK[/green] {d.serial}: agent reachable (sdk {info.get('sdk', '?')}, foreground: {pkg})")
            except Exception as e:
                ok = False
                console.print(
                    f"  [red]FAIL[/red] {d.serial}: agent not reachable — run [cyan]imo init[/cyan] ({e})"
                )

    if ok:
        console.print("\n[green]All checks passed.[/green]")
    else:
        console.print("\n[yellow]Some checks need attention — see messages above.[/yellow]")
        raise typer.Exit(1)


@app.command()
def devices():
    """List connected Android devices with model/battery info."""
    try:
        rows = list_devices()
    except RuntimeError as e:
        console.print(f"[red]adb error:[/red] {e}")
        raise typer.Exit(1)
    if not rows:
        console.print("No devices connected.")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Serial")
    table.add_column("Model")
    table.add_column("Manufacturer")
    table.add_column("Android")
    table.add_column("Battery")
    table.add_column("State")
    for d in rows:
        table.add_row(
            d.serial,
            d.model,
            d.manufacturer or "—",
            d.androidVersion or "—",
            f"{d.battery}%" if d.battery is not None else "—",
            d.state,
        )
    console.print(table)


@app.command("open")
def open_app_cmd(
    app_key: str = typer.Argument(..., help="App key: facebook, facebook_lite, messenger, instagram, whatsapp"),
    all: bool = typer.Option(False, "--all", help="Open on every connected device"),
    serial: str = typer.Option(None, "--serial", "-s", help="Open on this specific device"),
):
    """Launch an app on one or all connected devices."""
    if app_key not in KNOWN_APPS:
        console.print(f"[red]Unknown app '{app_key}'. Choose from: {', '.join(KNOWN_APPS)}[/red]")
        raise typer.Exit(1)
    label, pkg = KNOWN_APPS[app_key]
    targets = []
    if all:
        targets = connected_device_ids()
    elif serial:
        targets = [serial]
    else:
        console.print("Specify --all or --serial.")
        raise typer.Exit(1)
    if not targets:
        console.print("No devices connected.")
        return
    for target in targets:
        try:
            open_app(target, pkg)
            console.print(f"[green]Opened {label} on {target}[/green]")
        except Exception as e:
            console.print(f"[red]Failed on {target}: {e}[/red]")


@app.command()
def dump(
    serial: str = typer.Option(None, "--serial", "-s", help="Device serial (defaults to the first connected device)"),
    output: str = typer.Option(None, "--output", "-o", help="Write the full hierarchy XML to this file"),
):
    """Dump the current screen hierarchy for debugging (e.g. to see how the 🎲
    messages actually appear in the UI tree). Writes the full uncompressed XML
    to imo_dump_<serial>_<time>.xml and prints a compact summary of the chat
    message rows.
    """
    import xml.etree.ElementTree as ET

    targets = [serial] if serial else connected_device_ids()[:1]
    if not targets:
        console.print("[red]No devices connected.[/red]")
        raise typer.Exit(1)
    dev = get_device(targets[0])
    try:
        xml = dev.dump_hierarchy(compressed=False)
    except Exception as e:
        console.print(f"[red]Could not dump hierarchy: {e}[/red]")
        raise typer.Exit(1)
    out = output or f"imo_dump_{targets[0]}_{time.strftime('%Y%m%d_%H%M%S')}.xml"
    Path(out).write_text(xml, encoding="utf-8")
    console.print(f"[green]Saved full hierarchy to {out}[/green]")

    nodes = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        console.print(f"[red]Could not parse hierarchy: {e}[/red]")
        return
    for elem in root.iter():
        attr = elem.attrib
        b = attr.get("bounds")
        if not b:
            continue
        try:
            x1, y1, x2, y2 = map(int, b.replace("[", "").replace("]", ",").split(",")[:4])
        except (ValueError, IndexError):
            continue
        nodes.append(
            {
                "resourceId": attr.get("resource-id", ""),
                "text": (attr.get("text") or "").strip(),
                "contentDesc": (attr.get("content-desc") or "").strip(),
                "className": attr.get("class", "").rsplit(".", 1)[-1],
                "bounds": (x1, y1, x2, y2),
            }
        )

    dice = [n for n in nodes if DICE_EMOJI in n["text"] or DICE_EMOJI in n["contentDesc"]]
    console.print(f"Nodes containing {DICE_EMOJI}: [bold]{len(dice)}[/bold]")
    for n in dice[:10]:
        console.print(f"  {n['className']} id={n['resourceId'] or '-'} text={n['text'][:40]!r} desc={n['contentDesc'][:40]!r} bounds={n['bounds']}")

    rows = sorted([n for n in nodes if n["resourceId"] == CL_MESSAGE_ROOT], key=lambda n: n["bounds"][1])
    console.print(f"Message rows (cl_message_root): [bold]{len(rows)}[/bold]")
    for r in rows[:12]:
        rb = r["bounds"]
        inner = [
            n
            for n in nodes
            if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]
            and (n["text"] or n["contentDesc"])
        ]
        texts = [f"{(n['text'] or n['contentDesc'])[:24]}" for n in inner[:3]]
        is_thumb = any(
            n["resourceId"] in THUMB_IDS
            for n in nodes
            if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]
        )
        row_text = (r["text"] or "").strip()
        row_desc = (r["contentDesc"] or "").strip()
        extra = f" row.text={row_text[:24]!r} row.desc={row_desc[:24]!r}" if (row_text or row_desc) else ""
        console.print(f"  y={rb[1]:>4} thumb={is_thumb} texts={texts}{extra}")
    console.print("\nFull XML is in the saved file — paste the row lines above (or the file) back to wire the detection.")


def _start_servers_safely(console: Console, disconnect: bool = True, mirror: bool = False, port: int = 8771) -> None:
    """Start the optional background servers without crashing the run if a
    port is already taken (e.g. a second `imo run` in another terminal)."""
    if disconnect:
        try:
            start_disconnect_server()
        except OSError as e:
            console.print(f"[yellow]Disconnect server on :8770 unavailable ({e}) — remote `imo disconnect` won't work in this run[/yellow]")
    if mirror:
        try:
            start_mirror_server(port)
        except OSError as e:
            console.print(f"[yellow]Mirror server on :{port} unavailable ({e}) — screens won't stream[/yellow]")


@app.command()
def run(
    device: str = typer.Option(None, "--device", "-d", help="Only run on this device"),
    json_file: Path = typer.Option(None, "--json", "-j", help="Also append NDJSON events to this file"),
    mirror: bool = typer.Option(False, "--mirror", help="Start the MJPEG screen-mirror server alongside"),
    port: int = typer.Option(8771, "--port", help="Mirror server port"),
):
    """Run the IMO workflow on all connected devices concurrently."""
    ids = [device] if device else connected_device_ids()
    if not ids:
        console.print("[yellow]No connected devices found.[/yellow]")
        raise typer.Exit(1)

    results: list[dict] = []
    dashboard: Dashboard | None = None
    # NDJSON sink: console stdout if requested via --json, else the Rich dashboard.
    if json_file:
        stream = open(json_file, "w", encoding="utf-8") if json_file else sys.stdout
        writer = NdJsonWriter(stream=stream)
        log = make_log(writer.emit)
        workflow_checkpoint_emit(writer.emit)
        console.print(f"[cyan]Running on {len(ids)} device(s)...[/cyan]")
        _start_servers_safely(console, disconnect=True, mirror=mirror, port=port)
        results = run_all_devices(log) if not device else [run_imo_workflow_on_device(device, log)]
        writer.close()
        if stream is not sys.stdout:
            stream.close()
    else:
        dashboard = Dashboard([{"serial": s} for s in ids])
        log = dashboard.make_log()
        workflow_checkpoint_emit(dashboard._write_checkpoint_log)
        _start_servers_safely(console, disconnect=True, mirror=mirror, port=port)

        def worker():
            try:
                results.extend(run_all_devices(log) if not device else [run_imo_workflow_on_device(device, log)])
            finally:
                dashboard.mark_done()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        dashboard.run()  # renders until all device threads finish

    # Summary
    console.print("\n[bold]Summary:[/bold]")
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Device")
    table.add_column("Mode")
    table.add_column("Result")
    table.add_column("Liked")
    table.add_column("Details")
    if results:
        for r in results:
            serial = r.get("deviceId") or "?"
            mode = r.get("mode") or "—"
            liked = "—"
            if dashboard and serial != "?":
                liked = str(dashboard.device_totals(serial)["liked"])
            if r.get("success"):
                table.add_row(
                    serial,
                    mode,
                    "[green]OK[/green]",
                    liked,
                    r.get("package", ""),
                )
            else:
                table.add_row(
                    serial,
                    mode,
                    "[red]FAIL[/red]",
                    liked,
                    r.get("error", ""),
                )
        console.print(table)
    console.print("Done.")


@app.command()
def disconnect(
    device_id: str = typer.Argument(..., help="Device serial to stop"),
    port: int = typer.Option(8770, "--port", help="Control server port"),
):
    """Gracefully stop a running workflow on a device."""
    try:
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/disconnect",
            data=json.dumps({"deviceId": device_id}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        console.print(f"[green]{data}[/green]")
    except Exception as e:
        console.print(f"[yellow]Control server unreachable ({e}); flagging locally...[/yellow]")
        request_disconnect(device_id)
        console.print(f"[green]Disconnect requested for {device_id}[/green]")


@app.command()
def mirror(port: int = typer.Option(8771, "--port", help="Port to serve MJPEG streams")):
    """Serve live MJPEG screens for all connected devices (http://127.0.0.1:port/<serial>)."""
    ids = connected_device_ids()
    if not ids:
        console.print("[yellow]No devices connected.[/yellow]")
        raise typer.Exit(1)
    start_mirror_server(port)
    console.print(f"[cyan]Mirror server on http://127.0.0.1:{port}[/cyan]")
    for s in ids:
        console.print(f"  {s} -> http://127.0.0.1:{port}/{s}")
    console.print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        console.print("\nStopped.")


@app.command()
def report(json_file: Path = typer.Argument(..., help="Path to a --json NDJSON file from a past run")):
    """Print a final report for a past run's NDJSON log."""
    from .dashboard import reduce_device

    devices: dict[str, dict] = {}
    if not json_file.exists():
        console.print(f"[red]{json_file} not found.[/red]")
        raise typer.Exit(1)
    for line in json_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "log":
            continue
        device_id = ev.get("deviceId")
        if not device_id:
            continue
        prev = devices.setdefault(device_id, {"status": "idle", "imoInstances": {}})
        devices[device_id] = reduce_device(prev, ev.get("message", ""), ev.get("level", "info"), ev.get("meta"))

    if not devices:
        console.print("No events found.")
        return
    for serial, state in devices.items():
        console.print(f"\n[bold]{serial}[/bold] — {state.get('status', 'idle')}")
        for label, inst in state.get("imoInstances", {}).items():
            console.print(f"  [yellow]{label}[/yellow]{' [done]' if inst.get('done') else ''}")
            for acc_name, acc in inst.get("accounts", {}).items():
                console.print(f"    [cyan]account: {acc_name}[/cyan]")
                for chat_name, chat in acc.get("chats", {}).items():
                    console.print(
                        f"      [white]{chat_name}[/white] liked:{chat.get('videosLiked', 0)} failed:{chat.get('videosFailed', 0)}"
                    )


@app.command()
def init():
    """Install/verify the uiautomator2 server (u2.jar) on all connected devices.

    uiautomator2 3.x pushes /data/local/tmp/u2.jar and launches the UIAutomator
    server automatically when you connect — this command just forces that to
    happen now (and resets it) so the first `imo run` isn't slow.
    """
    import uiautomator2 as u2

    ids = connected_device_ids()
    if not ids:
        console.print("[yellow]No devices connected.[/yellow]")
        raise typer.Exit(1)
    for s in ids:
        console.print(f"[cyan]Initializing {s}...[/cyan]")
        try:
            d = u2.connect_usb(s)
            # Force the uiautomator server to actually start (pushes u2.jar
            # if needed), so we know the device is ready before running.
            d.reset_uiautomator()
            console.print(f"[green]{s}: uiautomator2 server ready.[/green]")
        except Exception as e:
            console.print(f"[red]{s}: {e}[/red]")
    console.print("Done.")


def main():
    try:
        app()
    except AuthenticationError as exc:
        console.print(f"[red]Authentication failed:[/red] {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

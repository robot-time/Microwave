"""Unified CLI entry point: `microwave <command>`

Commands:
    microwave run        Start an expert node (connects to gateway)
    microwave gateway    Start the gateway server
    microwave status     Show gateway health and expert list
    microwave setup      Re-run first-time setup
    microwave version    Print version
"""

import argparse
import sys
import os

from . import __version__


def cmd_run(args: list[str]) -> None:
    """Start an expert node. Passes all args through to microwave-node."""
    from .node import main
    sys.argv = ["microwave run"] + args
    main()


def cmd_gateway(args: list[str]) -> None:
    """Start the gateway server. Passes all args through to microwave-gateway."""
    from .gateway import main
    sys.argv = ["microwave gateway"] + args
    main()


def cmd_status(args: list[str]) -> None:
    """Query the gateway for health and expert info."""
    import json
    try:
        import httpx
    except ImportError:
        print("httpx not installed. Run: pip install httpx")
        sys.exit(1)

    parser = argparse.ArgumentParser(prog="microwave status")
    parser.add_argument(
        "--gateway-url",
        default=os.getenv(
            "MICROWAVE_GATEWAY_URL",
            "http://localhost:8000",
        ),
        help="Gateway URL to query",
    )
    opts = parser.parse_args(args)
    url = opts.gateway_url.rstrip("/")

    try:
        with httpx.Client(timeout=5.0) as client:
            health = client.get(f"{url}/health").json()
            experts = client.get(f"{url}/experts").json()
    except Exception as e:
        print(f"Cannot reach gateway at {url}: {e}")
        sys.exit(1)

    print(f"Gateway:  {url}")
    print(f"Version:  {health.get('version', '?')}")
    print(f"Nodes:    {health.get('nodes', 0)}")
    print(f"Experts:  {health.get('experts', 0)}")

    moe = health.get("moe_stats", {})
    if moe.get("total_requests", 0) > 0:
        print(f"Requests: {moe['total_requests']}  "
              f"Avg experts/req: {moe['avg_experts_per_request']:.1f}  "
              f"Avg latency: {moe['avg_response_ms']:.0f}ms")
    print()

    if experts:
        print(f"{'NODE':<24} {'DOMAINS':<24} {'MODELS':<20} {'LATENCY':>10}")
        print("-" * 80)
        for e in experts:
            domains = ", ".join(e.get("domains", []))
            models = ", ".join(e.get("models", []))
            lat = e.get("latency_ms", -1)
            lat_str = f"{lat:.1f}ms" if lat >= 0 else "--"
            print(f"{e['node_id']:<24} {domains:<24} {models:<20} {lat_str:>10}")
    else:
        print("No experts online.")


def cmd_setup(args: list[str]) -> None:
    """Re-run the setup script."""
    script = os.path.join(os.path.dirname(__file__), "..", "setup.sh")
    script = os.path.abspath(script)
    if not os.path.exists(script):
        script = os.path.join(os.path.expanduser("~"), "Microwave", "setup.sh")
    if not os.path.exists(script):
        print("setup.sh not found. Run from the Microwave repo directory.")
        sys.exit(1)
    os.execvp("bash", ["bash", script] + args)


def cmd_version(_args: list[str]) -> None:
    print(f"Microwave AI v{__version__}")


COMMANDS = {
    "run": (cmd_run, "Start an expert node"),
    "gateway": (cmd_gateway, "Start the gateway server"),
    "status": (cmd_status, "Show gateway health and experts"),
    "setup": (cmd_setup, "Re-run first-time setup"),
    "version": (cmd_version, "Print version"),
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(f"Microwave AI v{__version__}")
        print()
        print("Usage: microwave <command> [options]")
        print()
        print("Commands:")
        for name, (_, desc) in COMMANDS.items():
            print(f"  {name:<12} {desc}")
        print()
        print("Examples:")
        print("  microwave run                         Start a node (default settings)")
        print("  microwave run --expert-domains code   Start a code expert")
        print("  microwave gateway                     Start the gateway")
        print("  microwave status                      Check network status")
        sys.exit(0)

    cmd_name = sys.argv[1]
    rest = sys.argv[2:]

    if cmd_name not in COMMANDS:
        print(f"Unknown command: {cmd_name}")
        print(f"Run 'microwave --help' for usage.")
        sys.exit(1)

    fn, _ = COMMANDS[cmd_name]
    fn(rest)


if __name__ == "__main__":
    main()

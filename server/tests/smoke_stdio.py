"""Smoke test over real stdio: start the server command given on the command line, list its tools,
call lr_status (Lightroom need not be running) and print the results.

Set SMOKE_FULL=1 with Lightroom open and the plug-in loaded to also call every tool and check the picture.

Usage: python smoke_stdio.py <command> [args...]      e.g.  python smoke_stdio.py build\\dist\\lrmcp\\lrmcp.exe
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from lightroom_mcp.__main__ import out


def first_text(result) -> str:
    return next((c.text for c in result.content if getattr(c, "type", "") == "text"), "")


async def main() -> int:
    params = StdioServerParameters(command=sys.argv[1], args=sys.argv[2:])
    full = os.environ.get("SMOKE_FULL") == "1"
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            for t in tools.tools:
                ro = t.annotations.read_only_hint if t.annotations else None
                print(f"  {t.name}: read_only_hint={ro}")
            result = await session.call_tool("lr_status", {})
            print("lr_status ->", first_text(result)[:300].replace("\n", " "))
            result = await session.call_tool("lr_get_develop", {"photo": "bad id with spaces"})
            print("lr_get_develop(bad) is_error=", result.is_error, "|", first_text(result)[:150].replace("\n", " "))
            if not full:
                return 0

            listing = json.loads(first_text(await session.call_tool("lr_list_selected", {})))
            print("lr_list_selected -> count", listing["count"], [p["name"] for p in listing["photos"]])
            develop = json.loads(first_text(await session.call_tool("lr_get_develop", {"photo": "selected"})))
            print("lr_get_develop -> Exposure2012", develop["settings"].get("Exposure2012"), "WhiteBalance", develop["settings"].get("WhiteBalance"))
            preview = await session.call_tool("lr_get_preview", {"photo": "selected", "long_edge": 1024})
            kinds = [(c.type, len(getattr(c, "data", "") or "")) for c in preview.content]
            print("lr_get_preview content types/sizes:", kinds, "is_error=", preview.is_error)
            info = json.loads(first_text(preview))
            print("   stats notes:", info["stats"]["notes_heuristic"], "| mean", info["stats"]["mean_brightness_0_255"])
            doctor = await session.call_tool("lr_doctor", {})
            print("lr_doctor ->")
            out(first_text(doctor))
            if os.environ.get("SMOKE_WRITE") == "1":
                await write_round_trip(session, first_text)
    return 0


async def write_round_trip(session, first_text) -> None:
    """Exercise the write tools through MCP. Only on the test catalog; undoes everything and removes its own
    edit records."""
    from pathlib import Path

    edits_dir = Path.home() / "LrMcp" / "edits"
    existing = {p.name for p in edits_dir.glob("*.json")} if edits_dir.is_dir() else set()
    status = json.loads(first_text(await session.call_tool("lr_status", {})))
    if "ceshi" not in status.get("catalog_path", ""):
        print("WRITE TEST REFUSED: not the ceshi test catalog")
        return
    base = None
    try:
        base = json.loads(first_text(await session.call_tool("lr_get_develop", {"photo": "selected"})))["settings"]["Exposure2012"]
        res = await session.call_tool("lr_set_develop", {"changes": {"Exposure2012": base + 0.2}, "reason": "mcp smoke test", "preview": True})
        kinds = [c.type for c in res.content]
        info = json.loads(first_text(res))
        print("lr_set_develop(preview=True) ->", "is_error=", res.is_error, "| content:", kinds, "| verified:", info["verified"], "| has preview stats:", "preview" in info)
        bad = await session.call_tool("lr_set_develop", {"changes": {"LensManualDistortionAmount": 5}})
        print("lr_set_develop(LensManualDistortionAmount) is_error=", bad.is_error, "|", " ".join(first_text(bad)[:90].split()))
        jump = await session.call_tool("lr_set_develop", {"changes": {"Exposure2012": base + 3}})
        print("lr_set_develop(+3 EV) is_error=", jump.is_error, "|", " ".join(first_text(jump)[:90].split()))
        listed = json.loads(first_text(await session.call_tool("lr_list_edits", {"photo": "selected"})))
        print("lr_list_edits -> count", listed["count"])
        dry = json.loads(first_text(await session.call_tool("lr_restore_original", {"dry_run": True})))
        print("lr_restore_original(dry_run) will_write:", dry.get("will_write"))
    finally:
        done = json.loads(first_text(await session.call_tool("lr_restore_original", {})))
        print("lr_restore_original -> verified:", done.get("verified"))
        after = json.loads(first_text(await session.call_tool("lr_get_develop", {"photo": "selected"})))["settings"]["Exposure2012"]
        print("Exposure2012 back to the start:", "unknown" if base is None else abs(after - base) < 1e-6)
        for p in edits_dir.glob("*.json"):
            if p.name not in existing:
                p.unlink()  # remove the records this test created


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

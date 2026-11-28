"""Run small-signal AC analysis of the saved stereo KiCad schematic in QSPICE."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


from spice_common import (ROOT, load_config, load_plot, netlist, passive_value, plot_response,
                          riaa_db, write_csv, write_report)


def executable(explicit, name, candidates):
    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise FileNotFoundError(explicit)
    found = shutil.which(name)
    for path in ([Path(found)] if found else []) + candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"{name}: specify its path with --{name.split('.')[0].lower()}")


def read_ac(path, channel):
    header, body = path.read_text(encoding="ascii").split("Values:\n", 1)
    metadata, variables = header.split("Variables:\n", 1)
    names = [line.split()[1].lower() for line in variables.splitlines()]
    count = int(re.search(r"(?m)^No\. Points:\s*(\d+)", metadata)[1])
    values = iter(body.splitlines())
    rows = list(zip(*([values] * len(names)), strict=True))
    if len(rows) != count or count < 2 or "Flags: complex" not in metadata:
        raise ValueError(f"Incomplete or unsupported AC data in {path}")
    trace = names.index(f"v(line_{channel})")
    return [(float(row[0].split()[1]), complex(*map(float, row[trace].split(","))))
            for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "scripts/config.toml")
    parser.add_argument("--qspice64", type=Path)
    parser.add_argument("--kicad-cli", type=Path)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config, "qspice")
        plot = load_plot(cfg["qspice"])
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    options = cfg["qspice"]
    analysis = cfg["analysis"]
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    qspice_path = args.qspice64 or (ROOT / options["qspice64"] if "qspice64" in options else None)
    kicad_path = args.kicad_cli or (ROOT / options["kicad_cli"] if "kicad_cli" in options else None)
    simulator = executable(qspice_path, "QSPICE64.exe", [
        Path.home() / "QSPICE/QSPICE64.exe", program_files / "QSPICE/QSPICE64.exe"])
    kicad = executable(kicad_path, "kicad-cli.exe", [
        candidate
        for location in (local / "Programs/KiCad", program_files / "KiCad")
        for candidate in sorted(location.glob("*/bin/kicad-cli.exe"), reverse=True)
    ])
    output = ROOT / options["output_directory"]
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    print(run, flush=True)
    xml = run / "PreAmp.xml"
    subprocess.run([kicad, "sch", "export", "netlist", "--format", "kicadxml",
                    "-o", xml, ROOT / "PreAmp.kicad_sch"], check=True, timeout=90)
    results = {}
    for channel in ("l", "r"):
        deck = run / f"preamp_{channel}.cir"
        raw = deck.with_suffix(".qraw")
        deck.write_text(netlist(xml, cfg, channel), encoding="ascii")
        subprocess.run([simulator, "-ascii", deck.name, "-r", raw.name],
                       cwd=run, check=True, timeout=90)
        results[channel] = write_csv(deck.with_suffix(".csv"), read_ac(raw, channel), cfg)
    if plot is not None:
        plot_response(plot, run, analysis["reference_frequency_hz"], results)
    if options["write_report"]:
        write_report(run, cfg, results)


if __name__ == "__main__":
    main()

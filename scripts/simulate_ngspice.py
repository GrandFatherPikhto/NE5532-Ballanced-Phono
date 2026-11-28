"""Run standalone small-signal AC analysis of the saved KiCad schematic in ngspice."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from ngspice_common import find_kicad, make_runner
from spice_common import ROOT, load_config, load_plot, netlist, plot_response, write_csv, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "scripts/config.toml")
    parser.add_argument("--ngspice", help="Native executable, or Linux command with --backend wsl")
    parser.add_argument("--kicad-cli", help="KiCad CLI executable")
    parser.add_argument("--backend", choices=("auto", "native", "wsl"))
    parser.add_argument("--distro", help="WSL distribution (only used by the WSL backend)")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config, "ngspice")
        options = cfg["ngspice"]
        runner = make_runner(options, backend=args.backend, ngspice=args.ngspice, distro=args.distro)
        kicad = find_kicad(args.kicad_cli or options.get("kicad_cli"))
        plot = load_plot(options)
        output = ROOT / options["output_directory"]
        output.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
        print(run, flush=True)
        shutil.copyfile(args.config, run / "config.toml")
        xml = run / "PreAmp.xml"
        exported = subprocess.run(
            [kicad, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml),
             str(ROOT / "PreAmp.kicad_sch")], capture_output=True, timeout=runner.timeout_s)
        (run / "kicad.log").write_bytes(exported.stdout + b"\n" + exported.stderr)
        if exported.returncode:
            raise RuntimeError(f"KiCad export failed; see {run / 'kicad.log'}")
        info = {"simulator": "ngspice", "backend": runner.backend,
                "command": list(runner.command), "rshunt_ohm": runner.rshunt_ohm,
                "analysis": cfg["analysis"], "opamp": cfg["opamp"], "riaa": cfg["riaa"],
                "xml_sha256": hashlib.sha256(xml.read_bytes()).hexdigest(),
                "model": "Linear one-pole op amp; ideal output and supplies; no cartridge/cable, "
                         "noise, distortion, clipping, or real-device stability validation."}
        (run / "run_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        results = {}
        for channel in ("l", "r"):
            samples = runner.run(netlist(xml, cfg, channel), channel, run)
            results[channel] = write_csv(run / f"preamp_{channel}.csv", samples, cfg)
        if plot is not None:
            plot_response(plot, run, cfg["analysis"]["reference_frequency_hz"], results,
                          simulator="ngspice")
        if options["write_report"]:
            write_report(run, cfg, results, simulator="ngspice", section="ngspice")
            summary = {}
            for channel, (frequencies, _, _, errors, gain) in results.items():
                low, high = min(zip(errors, frequencies)), max(zip(errors, frequencies))
                summary[channel] = {"points": len(frequencies), "gain_at_reference_v_per_v": gain,
                                    "riaa_min_db": low[0], "riaa_min_frequency_hz": low[1],
                                    "riaa_max_db": high[0], "riaa_max_frequency_hz": high[1]}
            (run / "report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()

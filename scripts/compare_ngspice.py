"""Compare a saved QSPICE AC run with native ngspice or ngspice in WSL."""

import argparse
from bisect import bisect_left
import csv
import math
from pathlib import Path
import subprocess

from ngspice_common import make_runner, ngspice_deck, read_ngspice_table
from spice_common import load_config


ROOT = Path(__file__).resolve().parents[1]


def interpolate(points, frequencies, frequency):
    index = bisect_left(frequencies, frequency)
    for nearby in (index, index - 1):
        if 0 <= nearby < len(points) and math.isclose(
            frequency, frequencies[nearby], rel_tol=1e-10
        ):
            return points[nearby][1:]
    if not 0 < index < len(points):
        raise ValueError(f"{frequency:g} Hz falls outside the ngspice sweep")
    low, gain, phase = points[index - 1]
    high, next_gain, next_phase = points[index]
    weight = math.log(frequency / low) / math.log(high / low)
    return gain + weight * (next_gain - gain), phase + weight * math.remainder(next_phase - phase, 360)


def compare(qspice_csv, points):
    frequencies = [sample[0] for sample in points]
    rows = []
    with qspice_csv.open(newline="", encoding="ascii") as stream:
        for sample in csv.DictReader(stream):
            f = float(sample["frequency_hz"])
            q_gain, q_phase = float(sample["gain_db"]), float(sample["phase_deg"])
            gain, phase = interpolate(points, frequencies, f)
            rows.append((f, q_gain, gain, gain - q_gain, q_phase, phase,
                         math.remainder(phase - q_phase, 360)))
    if not rows:
        raise ValueError(f"No QSPICE samples in {qspice_csv}")
    return rows


def latest_run(directory=None):
    runs = []
    for path in (directory or ROOT / "build/qspice").glob("run-*"):
        try:
            if all((path / f"preamp_{ch}.{ext}").is_file()
                   for ch in ("l", "r") for ext in ("cir", "csv")):
                runs.append((path.stat().st_mtime, path))
        except PermissionError:
            continue
    return max(runs)[1] if runs else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", type=Path, help="QSPICE run directory (default: latest)")
    parser.add_argument("--config", type=Path, default=ROOT / "scripts/config.toml")
    parser.add_argument("--backend", choices=("auto", "native", "wsl"))
    parser.add_argument("--ngspice", help="Native executable, or Linux command with --backend wsl")
    parser.add_argument("--distro", help="WSL distribution")
    args = parser.parse_args()
    comparisons = {}
    try:
        cfg = load_config(args.config, "ngspice")
        runner = make_runner(cfg["ngspice"], backend=args.backend,
                             ngspice=args.ngspice, distro=args.distro)
        directory = ROOT / cfg.get("qspice", {}).get("output_directory", "build/qspice")
        run = args.run or latest_run(directory)
        if run is None:
            parser.error("No QSPICE run found. Run scripts/simulate_qspice.py first.")
        for channel in ("l", "r"):
            samples = runner.run(run / f"preamp_{channel}.cir", channel, run,
                                 stem=f"ngspice_compare_{channel}")
            points = [(f, 20 * math.log10(abs(v)), math.degrees(math.atan2(v.imag, v.real)))
                      for f, v in samples]
            comparisons[channel] = compare(run / f"preamp_{channel}.csv", points)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")

    for channel, rows in comparisons.items():
        path = run / f"ngspice_compare_{channel}.csv"
        with path.open("w", newline="", encoding="ascii") as stream:
            writer = csv.writer(stream)
            writer.writerow(["frequency_hz", "qspice_gain_db", "ngspice_gain_db", "delta_gain_db",
                             "qspice_phase_deg", "ngspice_phase_deg", "delta_phase_deg"])
            writer.writerows(rows)
        print(f"{channel.upper()}: {len(rows)} points, "
              f"max |delta gain| = {max(abs(row[3]) for row in rows):.6g} dB, "
              f"max |delta phase| = {max(abs(row[6]) for row in rows):.6g} deg")
        print(path.resolve())


if __name__ == "__main__":
    main()

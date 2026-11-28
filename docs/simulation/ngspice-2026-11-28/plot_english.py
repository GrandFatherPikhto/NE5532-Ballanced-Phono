"""Draw English publication plots from this frozen run's CSV, without simulating."""

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    source = Path(__file__).resolve().parent
    images = source.parents[1] / "images"
    images.mkdir(exist_ok=True)
    figure, axes = plt.subplots(3, 1, sharex=True, figsize=(9, 8), layout="constrained")
    figure.suptitle("MM phono preamp · ngspice · nominal AC response")
    for channel in ("l", "r"):
        with (source / f"preamp_{channel}.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        frequencies = [float(row["frequency_hz"]) for row in rows]
        style = {"linestyle": "--", "color": "#be6225"} if channel == "r" else {"color": "#176b95"}
        for axis, field in zip(axes, ("gain_db", "phase_deg", "riaa_error_db"), strict=True):
            axis.semilogx(frequencies, [float(row[field]) for row in rows],
                          label=channel.upper(), **style)
    for axis, label in zip(axes, ("Gain, dB", "Phase, °", "RIAA deviation, dB"), strict=True):
        axis.set_ylabel(label)
        axis.grid(True, which="both", alpha=0.22)
        axis.axvline(1000, color="0.5", linewidth=0.8)
    axes[0].legend()
    axes[-1].axhline(0, color="0.5", linewidth=0.8)
    axes[-1].set_xlabel("Frequency, Hz")
    stem = images / "ngspice-2026-11-28-response-en"
    figure.savefig(stem.with_suffix(".png"), dpi=160)
    figure.savefig(stem.with_suffix(".svg"))
    plt.close(figure)


if __name__ == "__main__":
    main()

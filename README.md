# NE5532 Balanced Phono Preamplifier

[Русская версия](README_ru.md)

A stereo moving-magnet phono preamplifier for the Audio-Technica AT-3600L,
using NE5532 operational amplifiers and RIAA playback equalization.
The main design has balanced inputs from floating cartridge coils and
conventional, ground-referenced line outputs. An earlier unbalanced design
is retained for comparison.

## Design

Each channel uses two input amplifiers with a shared gain-setting bridge,
followed by a differential amplifier with the 75 µs RIAA pole. The stereo
design uses three dual NE5532 packages and a ±12 V supply.

| Parameter | Nominal design |
| --- | --- |
| Cartridge | Audio-Technica AT-3600L, moving magnet |
| Differential input load | 47 kΩ in the midband: 23.5 kΩ from each input to GND |
| Input coupling | 4.7 µF per leg |
| Input amplifier feedback | 90.9 kΩ in parallel with a series 10 kΩ / 31.5 nF branch |
| Gain bridge | 182 Ω in series with two back-to-back 1000 µF capacitors |
| Differential amplifier | Four 10 kΩ resistors and two matched 7.5 nF capacitors per channel |
| Line output | 100 Ω, series 4.7 µF, 100 kΩ to GND |
| Power | ±12 V, supplied through J2 |

The bridge capacitors have their negative terminals joined at a floating
midpoint. Their linear series equivalent is 500 µF. The L− and R− cartridge
connections remain separate from GND; cable shields and the tonearm ground
are separate connections.

With the nominal values in `scripts/config.toml` and an external 47 kΩ load,
the ideal model gives 100.723 V/V (40.063 dB) at 1 kHz. A 4 mV RMS
differential input gives approximately 0.403 V RMS at that frequency.
The ideal RIAA deviation, normalized at 1 kHz, is −0.0683 to +0.0018 dB
over 20 Hz–20 kHz.

## Project status and model limits

The schematic is under development, with component selection and footprints
still being finalized before PCB layout. `PreAmp.kicad_pcb` is an empty
board placeholder. The separate LM317/LM337 power supply is documented in
LaTeX; the KiCad preamplifier schematic receives regulated ±12 V and does
not include that regulator circuit.

The Python tools compare an analytical transfer function with a nodal model
and estimate the effect of a single-pole op-amp model. These calculations
do not establish measured noise, THD, stability, overload margin or CMRR.
They omit cartridge impedance, cable capacitance, component tolerances,
capacitor leakage and nonlinearity, and PCB parasitics.

## Files

| Path | Contents |
| --- | --- |
| `PreAmp.kicad_pro`, `PreAmp.kicad_sch` | Main KiCad project and schematic |
| `channel.kicad_sch`, `ballanced_in.kicad_sch`, `diffamp.kicad_sch`, `power.kicad_sch` | Hierarchical schematic sheets |
| `PreAmp.net` | Exported netlist snapshot; may lag behind the schematic |
| `out/` | Condensed netlists, BOM and audit snapshots for quick inspection |
| `scripts/` | Python models, plotting tools, dependencies and `config.toml` |
| `docs/latex/` | Circuit documents, shared LaTeX support files and PDF snapshots |
| [docs/PHONO_GUIDE.pdf](docs/PHONO_GUIDE.pdf) | Detailed design guide in Russian; source: [PHONO_GUIDE.tex](docs/PHONO_GUIDE.tex) |
| [docs/POWER.md](docs/POWER.md) | Preliminary transformer notes; actual voltages and phasing require verification |
| `docs/scheme/` | Reference scans for the Melodiya-103 player |
| `build/` | Generated calculations, plots, check reports and document builds; ignored by Git |

Use the current KiCad schematic to establish connectivity and
`scripts/config.toml` for the calculation parameters. KiCad, TOML and LaTeX
are maintained independently; changing one does not update the others.
Historical filenames containing `ballanced` and `ad3600l` are preserved.

## Run the calculations

Use Python 3.11 or newer. Run these commands from the repository root:

```sh
python3 -m venv scripts/.venv
scripts/.venv/bin/python -m pip install -e scripts
scripts/.venv/bin/python scripts/calculate_balanced_riaa.py
scripts/.venv/bin/python scripts/calculate_unbalanced_riaa.py
scripts/.venv/bin/python scripts/render_phono_guide.py
```

Dependencies are declared in `scripts/pyproject.toml`. Both calculation
scripts load `scripts/config.toml` by default and write CSV, JSON, PNG and
PDF results to `build/balanced_riaa/`, including the unbalanced results.
The guide renderer writes to `build/phono_guide/`.

To choose a configuration and output directory explicitly:

```sh
scripts/.venv/bin/python scripts/calculate_balanced_riaa.py \
  --config scripts/config.toml --output-dir build/check
```

## Run the QSPICE simulation on Windows

Install Python 3.11 or later, KiCad with `kicad-cli.exe`, and QSPICE with
`QSPICE64.exe`. Open PowerShell in the project root and run:

```powershell
python -m pip install -e scripts
python scripts/simulate_qspice.py
```

Install the Python dependencies once per environment. Save your changes in
KiCad before each run. The script exports the saved hierarchical schematic
`PreAmp.kicad_sch` to fresh XML and builds the SPICE netlists from that export.
Component values and connections come from this XML, so there is no need to
regenerate `PreAmp.net`.

The [`[qspice]` section of the TOML file](scripts/config.toml) controls the
output directory, report and plot generation, and optional overrides for
frequency range, point count, RIAA reference frequency, and external load.
Set `kicad_cli` and `qspice64` there if automatic detection misses your
installation, or pass `--kicad-cli` and `--qspice64` on the command line.
Use `--config path/to/config.toml` for a separate set of settings. Setting
`write_plots = false` allows running without Matplotlib.

Each run prints the path to its results directory (by default,
`build/qspice/run-*/`). Open `report.md` for gain at the reference frequency
and the RIAA error extrema and their frequencies. Open `response.png` or
`response.svg` for both channels' gain, phase, and RIAA error. The directory
also contains per-channel CSV, CIR and QRAW files and the KiCad XML export.

The script generates a linear, single-pole NE5532 approximation from the
existing `[opamp]` settings (default open-loop gain 100,000 and GBW 12 MHz).
It estimates gain and phase with an ideal differential source. Cartridge
impedance, cable capacitance, noise, and output clipping are outside this
model.

## Build the documents

Install XeLaTeX, latexmk and the TeX packages used by the documents, including
TikZ/PGFPlots, fontspec and polyglossia. The shared preamble requires
Times New Roman, Arial and Courier New. A local circuitikz style is included.
Generate the calculations and guide figures above before building.

From the repository root:

```sh
TEXINPUTS=.:./docs/latex//: latexmk -xelatex -interaction=nonstopmode \
  -halt-on-error -outdir=build/latex docs/latex/ne5532_ad3600l_ballanced.tex
TEXINPUTS=.:./docs/latex//: latexmk -xelatex -interaction=nonstopmode \
  -halt-on-error -outdir=build/latex docs/latex/ne5532_at3600l.tex
TEXINPUTS=.:./docs/latex//: latexmk -xelatex -interaction=nonstopmode \
  -halt-on-error -outdir=build/latex docs/latex/analog_power_lm317_lm337.tex
TEXINPUTS=.:./docs/latex//: latexmk -xelatex -interaction=nonstopmode \
  -halt-on-error -outdir=build/phono_guide docs/PHONO_GUIDE.tex
```

The trailing colon in `TEXINPUTS` preserves the system TeX search path.
New PDFs remain in `build/`; checked-in PDF snapshots under `docs/` are
updated manually. Some existing documents contain commands from an earlier
directory layout; use the commands in this README for the current layout.

## Check the KiCad schematic

The project was last checked with KiCad 10.0.8. Save changes in the editor
before exporting or running ERC:

```sh
mkdir -p build/review
kicad-cli sch erc --format json --severity-all \
  --output build/review/erc.json PreAmp.kicad_sch
kicad-cli sch export netlist --format kicadsexpr \
  --output build/review/PreAmp.net PreAmp.kicad_sch
```

ERC checks enabled electrical rules. It does not validate component ratings,
footprint dimensions, equalization accuracy, stability or PCB layout.

"""ngspice execution and AC-table parsing for native and Windows/WSL runs."""

from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import subprocess

from spice_common import ROOT, positive


def find_program(explicit, names, candidates=()):
    if explicit is not None:
        text = os.fspath(explicit)
        found = shutil.which(text)
        if found:
            return str(Path(found).resolve())
        path = Path(text).expanduser()
        path = path if path.is_absolute() else ROOT / path
        if path.is_file():
            return str(path.resolve())
        raise FileNotFoundError(f"Executable not found: {text}")
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    raise FileNotFoundError(f"Executable not found: {' / '.join(names)}")


def find_kicad(explicit=None):
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    candidates = [p for base in (local / "Programs/KiCad", program_files / "KiCad")
                  for p in sorted(base.glob("*/bin/kicad-cli.exe"), reverse=True)]
    return find_program(explicit, ("kicad-cli", "kicad-cli.exe"), candidates)


def ngspice_deck(deck, channel, output="/dev/stdout", rshunt_ohm=1e12):
    text = deck.read_text(encoding="ascii") if isinstance(deck, Path) else deck
    body, end = text.rstrip().rsplit("\n", 1)
    if end.strip().lower() != ".end":
        raise ValueError("SPICE netlist is missing final .end")
    if any(line.strip().lower() == ".control" for line in body.splitlines()):
        raise ValueError("Expected a plain AC netlist without a .control section")
    positive(rshunt_ohm, "ngspice.rshunt_ohm")
    return (
        f"{body}\n* DC path for otherwise floating capacitor junctions.\n"
        f".options rshunt={rshunt_ohm:.12g}\n.control\nrun\noption numdgt=15\n"
        f"wrdata {output} v(line_{channel})\nquit\n.endc\n.end\n"
    )


def read_ac_table(text):
    """Read wrdata's frequency/real/imaginary columns, ignoring simulator banners."""
    points = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            frequency, real, imaginary = map(float, fields)
        except ValueError:
            continue
        voltage = complex(real, imaginary)
        if not all(math.isfinite(x) for x in (frequency, real, imaginary)) or frequency <= 0:
            raise ValueError("Invalid ngspice AC sample")
        if not 0 < abs(voltage) < math.inf:
            raise ValueError("Invalid ngspice AC magnitude")
        if points and frequency <= points[-1][0]:
            raise ValueError("ngspice AC frequencies must be strictly increasing")
        points.append((frequency, voltage))
    if len(points) < 2:
        raise ValueError(f"No complete ngspice AC table received:\n{text[-1500:]}")
    return points


def read_ngspice_table(text):
    """Compatibility format used by the QSPICE comparison script."""
    return [(f, 20 * math.log10(abs(v)), math.degrees(math.atan2(v.imag, v.real)))
            for f, v in read_ac_table(text)]


def decode_log(data):
    return data.decode("utf-16-le" if b"\0" in data else "utf-8",
                       errors="replace").strip("\ufeff")


@dataclass(frozen=True)
class NgspiceRunner:
    command: tuple[str, ...]
    backend: str
    timeout_s: float
    rshunt_ohm: float

    def run(self, deck, channel, directory, *, stem=None):
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stem = stem or f"preamp_{channel}"
        table_path = directory / f"{stem}.dat"
        # WSL receives the self-contained circuit via stdin and returns its data
        # via stdout, so it never has to translate Windows paths or write /mnt/*.
        destination = "/dev/stdout" if self.backend == "wsl" else table_path.name
        circuit = ngspice_deck(deck, channel, destination, self.rshunt_ohm)
        (directory / f"{stem}.cir").write_text(circuit, encoding="ascii")
        log_path = directory / f"{stem}.log"
        if self.backend == "native":
            table_path.unlink(missing_ok=True)
        try:
            process = subprocess.run(self.command, input=circuit.encode("ascii"),
                                     cwd=directory, capture_output=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as error:
            log_path.write_text(decode_log(error.stdout or b"") + "\n" +
                                decode_log(error.stderr or b""), encoding="utf-8")
            raise RuntimeError(f"ngspice timed out after {self.timeout_s:g}s; see {log_path}") from error
        stdout, stderr = decode_log(process.stdout), decode_log(process.stderr)
        log_path.write_text(stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"ngspice/{self.backend} exited with {process.returncode}; "
                               f"see {log_path}\n{(stderr or stdout)[-1500:]}")
        try:
            table = stdout if self.backend == "wsl" else table_path.read_text(encoding="ascii")
            samples = read_ac_table(table)
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read ngspice results; see {log_path}: {error}") from error
        table_path.write_text("".join(f"{f:.16e} {v.real:.16e} {v.imag:.16e}\n"
                                      for f, v in samples), encoding="ascii")
        return samples


def make_runner(options, *, backend=None, ngspice=None, distro=None):
    backend = backend or options.get("backend", "auto")
    if backend not in ("auto", "native", "wsl"):
        raise ValueError("ngspice.backend: expected auto, native, or wsl")
    timeout = positive(options.get("timeout_s", 90), "ngspice.timeout_s")
    rshunt = positive(options.get("rshunt_ohm", 1e12), "ngspice.rshunt_ohm")
    explicit = ngspice if ngspice is not None else options.get("ngspice")
    if backend != "wsl":
        try:
            program = find_program(explicit, ("ngspice", "ngspice_con.exe", "ngspice.exe"))
        except FileNotFoundError:
            if backend == "native" or explicit is not None:
                raise
        else:
            return NgspiceRunner((program, "-n", "-b"), "native", timeout, rshunt)
    wsl = find_program(None, ("wsl.exe",))
    distro = distro or options.get("wsl_distro", "Ubuntu-24.04")
    if not isinstance(distro, str) or not distro.strip():
        raise ValueError("ngspice.wsl_distro: expected a nonempty distribution name")
    program = explicit or "ngspice"
    return NgspiceRunner((wsl, "-d", distro, "--exec", str(program), "-n", "-b"),
                         "wsl", timeout, rshunt)

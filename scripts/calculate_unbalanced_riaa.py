"""Небалансная схема ne5532_at3600l.tex, один ОУ на канал.

Запуск из корня: scripts/.venv/bin/python scripts/calculate_unbalanced_riaa.py
Зависимости: scripts/.venv/bin/python -m pip install -e scripts
Номиналы: scripts/config.toml, секция [unbalanced]. См. также --help.

G1 = 1 + Zf/Zg, Zg = 91 Ом + 1/(s*1000 мкФ).
ВЧ-полюс RIAA создаёт ПАССИВНАЯ цепь 750 Ом / 100 нФ после ОУ.
Её нагружает выходной разделительный C и параллель 100 кОм с входом AUX.
Поэтому расчёт учитывает весь выходной RC-делитель, а не только 750*100 нФ.
"""

import math

from riaa_common import (NodalCircuit, feedback_impedance, inverse_open_loop_gain,
                         parallel_load, run)


def transfer(frequency, p, analysis):
    s = 2j * math.pi * frequency
    input_tau = p["input_bias_resistor_ohm"] * p["input_coupling_cap_f"]
    highpass = s * input_tau / (1 + s * input_tau)
    zg = p["gain_ground_resistor_ohm"] + 1 / (s * p["gain_ground_cap_f"])
    gain = 1 + feedback_impedance(s, p) / zg
    load = parallel_load(p, analysis)
    zc = 1 / (s * p["output_coupling_cap_f"])
    shunt = 1 / (s * p["output_shunt_cap_f"] + 1 / (load + zc))
    output = shunt / (p["output_series_resistor_ohm"] + shunt) * load / (load + zc)
    return highpass * gain * output


def nodal(frequency, p, analysis, model, finite=False):
    s = 2j * math.pi * frequency
    c = NodalCircuit(("input", "negative", "out", "filter", "line"))
    c.branch("input", None, 1 / p["input_bias_resistor_ohm"])
    c.source_branch("input", s * p["input_coupling_cap_f"], 1)
    c.branch("negative", "out", 1 / feedback_impedance(s, p))
    zg = p["gain_ground_resistor_ohm"] + 1 / (s * p["gain_ground_cap_f"])
    c.branch("negative", None, 1 / zg)
    c.branch("out", "filter", 1 / p["output_series_resistor_ohm"])
    c.branch("filter", None, s * p["output_shunt_cap_f"])
    c.branch("filter", "line", s * p["output_coupling_cap_f"])
    c.branch("line", None, 1 / parallel_load(p, analysis))
    c.amplifier("out", "input", "negative", inverse_open_loop_gain(s, model, finite))
    return c.solve()


if __name__ == "__main__":
    run("unbalanced", transfer, nodal)

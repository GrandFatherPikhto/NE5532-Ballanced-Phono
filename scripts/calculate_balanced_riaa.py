"""Балансный вход / обычный выход, ne5532_ad3600l_ballanced.tex.

Запуск из корня: scripts/.venv/bin/python scripts/calculate_balanced_riaa.py
Зависимости: scripts/.venv/bin/python -m pip install -e scripts
Номиналы: scripts/config.toml, секция [balanced]. См. также --help.

На канал: два входных ОУ + один разностный ОУ с фильтром 75 мкс.
Входной сигнал определяется МЕЖДУ выводами катушки, а не от одного вывода к GND.
Zg = Rg + 1/(s*Ca) + 1/(s*Cb) соединяет инвертирующие входы двух входных ОУ.
Ca и Cb — обычные электролиты по 1000 мкФ, встречно, минусы вместе.
В линейной модели их ёмкость равна 500 мкФ. Полярность, утечки и нелинейность
электролитов линейным AC-расчётом не моделируются; средняя точка не заземлена.
Их дифференциальное усиление G1 = 1 + 2*Zf/Zg.
Разностный каскад с четырьмя равными R и двумя равными C даёт
G2 = 1/(1 + s*R*C), причём Vout = G2*(V1 - V2).
Входные разделительные C и выходная нагрузка учитываются отдельно.
"""

import math

from riaa_common import (NodalCircuit, feedback_impedance, inverse_open_loop_gain,
                         parallel_load, run)


def transfer(frequency, p, analysis):
    s = 2j * math.pi * frequency
    input_tau = p["input_bias_resistor_ohm"] * p["input_coupling_cap_f"]
    highpass = s * input_tau / (1 + s * input_tau)
    zg = (p["gain_bridge_resistor_ohm"] + 1 / (s * p["gain_bridge_cap_a_f"])
          + 1 / (s * p["gain_bridge_cap_b_f"]))
    gain = 1 + 2 * feedback_impedance(s, p) / zg
    receiver = 1 / (1 + s * p["receiver_resistor_ohm"] * p["receiver_cap_f"])
    load = parallel_load(p, analysis)
    output = load / (load + p["output_series_resistor_ohm"] + 1 / (s * p["output_coupling_cap_f"]))
    return highpass * gain * receiver * output


def nodal(frequency, p, analysis, model, finite=False, common=False):
    s = 2j * math.pi * frequency
    c = NodalCircuit(("ip", "im", "np", "nm", "bridge_r", "bridge_mid",
                      "op", "om", "p", "m", "out", "ac", "line"))
    # Дифференциальная проверка: +0.5 и -0.5 В, то есть 1 В между входами.
    # Синфазная проверка: +1 и +1 В, то есть нулевое дифференциальное напряжение.
    for node, source in (("ip", 1 if common else .5), ("im", 1 if common else -.5)):
        c.branch(node, None, 1 / p["input_bias_resistor_ohm"])
        c.source_branch(node, s * p["input_coupling_cap_f"], source)
    c.branch("np", "op", 1 / feedback_impedance(s, p))
    c.branch("nm", "om", 1 / feedback_impedance(s, p))
    # Средняя точка катодов — отдельный плавающий узел, без ветви к GND.
    c.branch("np", "bridge_r", 1 / p["gain_bridge_resistor_ohm"])
    c.branch("bridge_r", "bridge_mid", s * p["gain_bridge_cap_a_f"])
    c.branch("bridge_mid", "nm", s * p["gain_bridge_cap_b_f"])
    r = p["receiver_resistor_ohm"]
    c.branch("op", "p", 1 / r)
    c.branch("om", "m", 1 / r)
    c.branch("p", None, 1 / r + s * p["receiver_cap_f"])
    c.branch("m", "out", 1 / r + s * p["receiver_cap_f"])
    c.branch("out", "ac", 1 / p["output_series_resistor_ohm"])
    c.branch("ac", "line", s * p["output_coupling_cap_f"])
    c.branch("line", None, 1 / parallel_load(p, analysis))
    inv_a = inverse_open_loop_gain(s, model, finite)
    c.amplifier("op", "ip", "np", inv_a)
    c.amplifier("om", "im", "nm", inv_a)
    c.amplifier("out", "p", "m", inv_a)
    return c.solve()


if __name__ == "__main__":
    run("balanced", transfer, nodal)

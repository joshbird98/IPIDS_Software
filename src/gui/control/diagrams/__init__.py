"""Machine diagrams: the operator interface drawn as the machine is laid out (a test, November 2026).

Four panels picture the machine as the thesis' drawings do, live:

    vacuum.py     Vacuum Diagram: pumps, gate valves and gauges on the vacuum vessels
    beamline.py   Beam Path Diagram: the optics along the beam, from TESS to the end-station
    safety.py     Safety Chain Diagram: what the safety relay's enable and loop are made of
    source.py     TESS Source Diagram: the source's electrodes, filament and caesium oven

Each draws what the text panels show and nothing of its own: a diagram reads its panels (refreshed
while it is in view), and a part clicked opens a faceplate (faceplate.py) whose controls mirror the
panel's (mirror.py) - same rules, same two-click confirmation, same commands. What the software can't
read is drawn dotted. The colours are the panels' (src/gui/theme.py): grey is normal, green on,
blue automatic, violet held by a sequence, amber and red what needs attention.

    canvas.py     the drawing surface: scale to fit, text at screen size, pointing and clicking
    symbols.py    pumps, valves, gauges and the like
"""

"""Machine diagrams: the operator interface drawn as the machine is laid out (a test, November 2026).

Four panels can also be shown as a diagram that pictures the machine as the thesis' drawings do, live
(a mimic diagram; each panel's List | Diagram switch: src/gui/control/panel_views.py):

    vacuum.py     Vacuum System: pumps, gate valves and gauges on the vacuum vessels
    beamline.py   Beamline Optics: the optics along the beam path, from TESS to the end-station
    safety.py     Interlocks: what the safety relay's enable and loop are made of (the safety chain)
    source.py     TESS Ion Source Control: the source's electrodes, filament and caesium oven

Each draws what the text panels show and nothing of its own: a diagram reads its panels (refreshed
while it is in view), and a part clicked opens a faceplate (faceplate.py) whose controls mirror the
panel's (mirror.py) - same rules, same two-click confirmation, same commands. What the software can't
read is drawn dotted. The colours are the panels' (src/gui/theme.py): grey is normal, green on,
blue automatic, violet held by a sequence, amber and red what needs attention.

    panel.py      what the four have in common: the drawing, its refresh, the faceplate open
    canvas.py     the drawing surface: scale to fit (the whole drawing shrinking below its readable
                  size), text at screen size, repainting only what changed, pointing and clicking,
                  the key, and the layout check the tests run (labels clear of each other and the lines)
    symbols.py    pumps, valves, gauges, lenses, slits, switches and the like
    faceplate.py  the card a part opens; plates.py, the ones more than one diagram opens
    mirror.py     a faceplate's controls: the panels' own, mirrored
"""

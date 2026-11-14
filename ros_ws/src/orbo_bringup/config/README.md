# Controller configuration

`control.launch.py` generates the drive-controller parameters from the selected
calibration YAML. The lifted Mac bench uses
`orbo_description/config/bench-cad.yaml`; its dimensions are nominal CAD values,
not a ground-driving calibration. The production calibration template is
`orbo_description/config/calibration.template.yaml`.

Generated parameter files and hardware captures are local runtime data. Do not
add them to this directory.

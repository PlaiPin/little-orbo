# Controller configuration

`control.launch.py` generates the drive-controller parameters from the selected
calibration YAML. The lifted Mac bench uses
`orbo_description/config/bench-cad.yaml`; its dimensions are nominal CAD values,
not a ground-driving calibration. The production calibration template is
`orbo_description/config/calibration.template.yaml`.

Generated parameter files and hardware captures are local runtime data. Do not
add them to this directory.

`teleop-gamepad.template.toml` supplies the Xbox/SDL left-stick and LB deadman
mapping for `joy.launch.py driver:=game_controller_node`. Merge it into the
runtime TOML, replacing the keyboard mapping, then qualify the actual controller.
It deliberately starts unqualified. See the
[remote joystick setup](../../../README.md#remote-xbox-joystick-control).

"""Arm controls for the matching Wi-Fi/USB firmware. Pulse values are commands, not feedback."""
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)


class ArmSlider(QWidget):
    def __init__(self, title, low, high, value, suffix="%"):
        super().__init__()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(low, high)
        self.slider.setValue(value)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        label = QLabel()
        label.setMinimumWidth(60)
        self.slider.valueChanged.connect(lambda v: label.setText(f"{v}{suffix}"))
        label.setText(f"{value}{suffix}")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(title))
        row.addWidget(self.slider, 1)
        row.addWidget(label)

    def value(self):
        return self.slider.value()


class ArmsPanel(QGroupBox):
    def __init__(self, send, prefs):
        super().__init__("Little hands")
        self.send = send
        self.prefs = prefs
        self.confirmed = False
        self.pending = None
        self.last_config_send = 0.0
        self.last_status = 0.0
        self.enabled = QCheckBox("Enable arms")
        self.enabled.setEnabled(False)
        self.enabled.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.enabled.toggled.connect(self.enable_changed)
        self.pattern = QComboBox()
        self.pattern.addItems(["Rest / center", "Sway together", "Paddle / walking hands",
                               "Hello wave (right hand)", "Happy wiggle", "Manual pose"])
        self.pattern.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tempo = ArmSlider("Speed", 20, 150, 50, "%")
        self.amplitude = ArmSlider("Travel", 0, 100, 35)
        self.left = ArmSlider("Left pose", -100, 100, 0)
        self.right = ArmSlider("Right pose", -100, 100, 0)
        self.manual = QWidget()
        manual_layout = QHBoxLayout(self.manual)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(self.left)
        manual_layout.addWidget(self.right)
        self.manual.setVisible(False)
        self.pattern.currentIndexChanged.connect(lambda i: self.manual.setVisible(i == 5))
        rest = QPushButton("Rest")
        off = QPushButton("Arms off")
        for button in (rest, off):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rest.clicked.connect(lambda: self.pattern.setCurrentIndex(0))
        off.clicked.connect(self.stop)
        row = QHBoxLayout()
        for widget in (self.enabled, self.pattern, rest, off):
            row.addWidget(widget)
        self.status = QLabel("Arms off. Connect and apply verified travel limits below.")
        self.status.setWordWrap(True)

        self.calibration = QGroupBox("Arm setup — pulse widths in microseconds")
        grid = QGridLayout(self.calibration)
        warning = QLabel("Starting values are unverified. Enter safe limits for your mounted arms.\n"
                         "Enabling arms first commands the center. Signals: left D7, right D10.")
        warning.setWordWrap(True)
        grid.addWidget(warning, 0, 0, 1, 5)
        for col, title in enumerate(("Arm", "Minimum", "Center", "Maximum", "Reverse")):
            grid.addWidget(QLabel(title), 1, col)
        self.values = []
        self.reverse = []
        for i, name in enumerate(("Left", "Right")):
            grid.addWidget(QLabel(name), i + 2, 0)
            values = []
            for j, default in enumerate((1400, 1500, 1600)):
                spin = QSpinBox()
                spin.setRange(500, 2500)
                spin.setValue(prefs.value(f"arms/{i}/{j}", default, type=int))
                spin.setSingleStep(10)
                values.append(spin)
                grid.addWidget(spin, i + 2, j + 1)
            self.values.append(values)
            reverse = QCheckBox()
            reverse.setChecked(prefs.value(f"arms/reverse/{i}", i == 1, type=bool))
            reverse.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.reverse.append(reverse)
            grid.addWidget(reverse, i + 2, 4)
        self.apply = QPushButton("Apply these limits (arms stay off)")
        self.apply.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.apply.clicked.connect(self.apply_config)
        grid.addWidget(self.apply, 4, 0, 1, 5)
        for values in self.values:
            for spin in values:
                spin.valueChanged.connect(self.invalidate)
        for reverse in self.reverse:
            reverse.toggled.connect(self.invalidate)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.tempo)
        layout.addWidget(self.amplitude)
        layout.addWidget(self.manual)
        layout.addWidget(self.status)
        layout.addWidget(self.calibration)

    def invalidate(self):
        self.stop()
        self.confirmed = False
        self.pending = None
        self.enabled.setEnabled(False)

    def apply_config(self):
        if time.monotonic() - self.last_status > 1.0:
            self.status.setText("Connect to the new arm firmware before applying limits.")
            return
        values = [spin.value() for arm in self.values for spin in arm]
        if any(not values[i] < values[i + 1] < values[i + 2] for i in (0, 3)):
            self.status.setText("Each arm needs minimum < center < maximum.")
            return
        self.stop()
        self.confirmed = False
        self.enabled.setEnabled(False)
        signs = [-1 if checkbox.isChecked() else 1 for checkbox in self.reverse]
        self.pending = "ARMCFG " + " ".join(map(str, values + signs))
        self.send(self.pending)
        self.last_config_send = time.monotonic()
        for i, arm in enumerate(self.values):
            for j, spin in enumerate(arm):
                self.prefs.setValue(f"arms/{i}/{j}", spin.value())
            self.prefs.setValue(f"arms/reverse/{i}", self.reverse[i].isChecked())

    def enable_changed(self, on):
        self.calibration.setEnabled(not on)
        if not on:
            self.send("ARM 0 50 0 0 0")

    def stop(self):
        self.enabled.setChecked(False)
        self.send("ARM 0 50 0 0 0")

    def reset(self):
        self.invalidate()
        self.last_status = 0
        self.status.setText("Arms off. Connect and apply verified travel limits.")

    def tick(self):
        now = time.monotonic()
        if now - self.last_status > 1.0:
            self.invalidate()
            self.status.setText("No arm telemetry — arms off. Matching firmware required.")
            return
        if self.pending and now - self.last_config_send >= 0.25:
            self.send(self.pending)
            self.last_config_send = now
        mode = self.pattern.currentIndex() + 1 if self.enabled.isChecked() and self.confirmed else 0
        self.send(f"ARM {mode} {self.tempo.value()} {self.amplitude.value()} {self.left.value()} {self.right.value()}")

    def receive(self, kind, rest):
        if kind == "ARMCFG":
            if self.pending == f"ARMCFG {rest}":
                self.pending = None
                self.confirmed = True
                self.enabled.setEnabled(True)
            return
        try:
            enabled, mode, left, right, configured, ready = map(int, rest.split())
        except ValueError:
            return
        self.last_status = time.monotonic()
        if not configured or not ready:
            self.confirmed = False
            self.enabled.setChecked(False)
            self.enabled.setEnabled(False)
        state = "ON" if enabled else "OFF"
        setup = "Limits applied" if self.confirmed else "Apply limits to enable"
        self.status.setText(f"Arms {state}  |  Commanded PWM: L {left} / R {right} us  |  {setup}"
                            if ready else "Arm PWM unavailable — check firmware log / pins.")

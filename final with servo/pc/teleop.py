"""
Teleop app: drive the robot from the PC, wired (ESP32 USB-C cable) or wireless (Wi-Fi).
The ESP32 runs ../firmware, which listens on both at once, so you just pick one here.

    pip install -r requirements.txt
    python teleop.py

Keys (the app window must be focused):
    W / S or Up / Down      forward / backward (hold)
    A / D or Left / Right   turn left / right (hold; combine with W / S to curve)
    Space or Esc            stop
"""
import socket
import sys
import time

import serial
from PyQt6.QtCore import QEvent, QSettings, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
                             QRadioButton, QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget)
from serial.tools import list_ports
from arms_panel import ArmsPanel

ESPRESSIF_VID = 0x303A          # the ESP32-S3's built-in USB port
UDP_PORT = 5005                 # must match the firmware (menuconfig -> Teleop robot)
DEFAULT_ADDRESS = "192.168.4.1"
MAX_RPM = 55
DEFAULT_SPEED_RPM = 25
DEFAULT_TURN_RPM = 15
SEND_HZ = 20                    # drive commands per second (also the keep-alive)
LINK_TIMEOUT_S = 1.0            # no STATE for this long -> "no reply"

KEYS = {
    Qt.Key.Key_W: "fwd", Qt.Key.Key_Up: "fwd",
    Qt.Key.Key_S: "back", Qt.Key.Key_Down: "back",
    Qt.Key.Key_A: "left", Qt.Key.Key_Left: "left",
    Qt.Key.Key_D: "right", Qt.Key.Key_Right: "right",
}
STOP_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Escape)


def wheel_speeds(held, speed, turn):
    """Held keys -> (left rpm, right rpm), positive = robot forward. Turning left slows the left wheel."""
    v = speed * (("fwd" in held) - ("back" in held))
    w = turn * (("left" in held) - ("right" in held))
    return v - w, v + w


def motion_name(held):
    parts = [n for n in ("fwd", "back", "left", "right") if n in held]
    return " + ".join(p.upper() for p in parts) or "STOPPED"


def split(line):
    kind, _, rest = line.strip().partition(" ")
    return kind, rest


class LinkError(Exception):
    pass


class WiredLink:
    """Text lines over the ESP32's USB serial port."""

    def __init__(self, port):
        self.port, self.rx = port, b""
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout, s.write_timeout = port, 115200, 0, 0.2
            s.dtr = s.rts = False       # don't reset the ESP32 when opening
            s.open()
            s.reset_input_buffer()      # drop old output that piled up while no app was reading
        except serial.SerialException as e:
            raise LinkError(f"cannot open {port}: {e} (idf.py monitor or another app using it?)") from None
        self.ser = s

    def name(self):
        return self.port

    def no_reply_hint(self):
        return f"No answer on {self.port} - right port? teleop firmware flashed?"

    def send(self, text):
        try:
            self.ser.write((text + "\n").encode())
        except serial.SerialException as e:
            raise LinkError(f"USB connection lost ({e})") from None

    def poll(self):
        """Returns [(kind, rest)] received since the last call."""
        try:
            n = self.ser.in_waiting
            if n:
                self.rx += self.ser.read(n)
        except (serial.SerialException, OSError) as e:
            raise LinkError(f"USB connection lost ({e})") from None
        *lines, self.rx = self.rx.split(b"\n")
        return [split(l.decode(errors="replace")) for l in lines if l.strip()]

    def close(self):
        try:
            self.ser.close()
        except serial.SerialException:
            pass


class WirelessLink:
    """One command per UDP packet to the robot's Wi-Fi address."""

    def __init__(self, sock, address):
        try:
            ip = socket.gethostbyname(address)
        except OSError:
            raise LinkError(f"cannot find '{address}' - use the robot's IP (192.168.4.1)") from None
        self.sock, self.robot = sock, (ip, UDP_PORT)

    def name(self):
        return self.robot[0]

    def no_reply_hint(self):
        return f"No reply from {self.robot[0]} - PC joined the PetBot Wi-Fi? battery on?"

    def send(self, text):
        try:
            self.sock.sendto(text.encode(), self.robot)
        except OSError as e:
            raise LinkError(f"send failed: {e}") from None

    def poll(self):
        return []                       # the window reads the shared socket (see receive_udp)

    def close(self):
        pass


class SpeedSlider(QWidget):
    def __init__(self, label, value):
        super().__init__()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, MAX_RPM)
        self.slider.setValue(value)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.value_label = QLabel(f"{value} rpm")
        self.value_label.setMinimumWidth(55)
        self.slider.valueChanged.connect(lambda v: self.value_label.setText(f"{v} rpm"))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(label))
        row.addWidget(self.slider, 1)
        row.addWidget(self.value_label)

    def value(self):
        return self.slider.value()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Little Orbo — Wi-Fi drive + little hands")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.prefs = QSettings("Teleoperate", "Teleop")
        self.link = None
        self.held = set()
        self.goal = (0.0, 0.0)
        self.connect_t = 0.0
        self.last_state_t = 0.0
        self.link_ok = False
        self.motors_ok = None

        # one UDP socket for the whole session: Find robot + wireless driving
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)

        # --- connection: mode selector + the options of each mode
        self.wired_radio = QRadioButton("Wired (USB cable)")
        self.wireless_radio = QRadioButton("Wireless (Wi-Fi)")
        self.mode_group = QButtonGroup(self)
        for r in (self.wired_radio, self.wireless_radio):
            r.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.mode_group.addButton(r)

        self.port_box = QComboBox()
        self.port_box.setMinimumWidth(280)
        self.port_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        refresh = QPushButton("Refresh")
        refresh.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        refresh.clicked.connect(self.refresh_ports)
        wired_page = QWidget()
        wl = QHBoxLayout(wired_page)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(QLabel("Port"))
        wl.addWidget(self.port_box, 1)
        wl.addWidget(refresh)

        self.address = QLineEdit(self.prefs.value("address", DEFAULT_ADDRESS))
        self.address.setPlaceholderText("robot address (192.168.4.1 on the PetBot Wi-Fi)")
        self.address.returnPressed.connect(self.toggle_connect)
        find = QPushButton("Find robot")
        find.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        find.clicked.connect(self.find)
        wireless_page = QWidget()
        rl = QHBoxLayout(wireless_page)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("Robot"))
        rl.addWidget(self.address, 1)
        rl.addWidget(find)

        self.pages = QStackedWidget()
        self.pages.addWidget(wired_page)
        self.pages.addWidget(wireless_page)
        self.wired_radio.toggled.connect(lambda on: self.pages.setCurrentIndex(0 if on else 1))

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.connect_btn.clicked.connect(self.toggle_connect)
        self.link_label = QLabel("Not connected")
        conn = QGroupBox("Connection")
        g = QGridLayout(conn)
        g.addWidget(self.wired_radio, 0, 0)
        g.addWidget(self.wireless_radio, 0, 1)
        g.addWidget(self.pages, 1, 0, 1, 2)
        g.addWidget(self.connect_btn, 0, 2, 2, 1)
        g.addWidget(self.link_label, 2, 0, 1, 3)
        self.conn_widgets = [self.wired_radio, self.wireless_radio, self.pages]

        # --- drive
        self.speed = SpeedSlider("Speed (W/S)", DEFAULT_SPEED_RPM)
        self.turn = SpeedSlider("Turn (A/D) ", DEFAULT_TURN_RPM)
        self.rev_left = QCheckBox("Reverse left")
        self.rev_right = QCheckBox("Reverse right")
        self.rev_left.setChecked(self.prefs.value("rev_left", False, type=bool))
        self.rev_right.setChecked(self.prefs.value("rev_right", True, type=bool))
        self.torque = QCheckBox("Torque on (untick = free wheels)")
        self.torque.setChecked(True)
        self.torque.toggled.connect(lambda on: self.send("TORQUE %d" % on))
        for c in (self.rev_left, self.rev_right, self.torque):
            c.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stop = QPushButton("STOP  (Space)")
        stop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stop.setMinimumHeight(40)
        stop.clicked.connect(self.stop)
        self.motion = QLabel("STOPPED")
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        self.motion.setFont(f)
        self.motion.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drive = QGroupBox("Drive")
        v = QVBoxLayout(drive)
        v.addWidget(self.motion)
        v.addWidget(QLabel("Hold W/A/S/D or the arrow keys. Space / Esc = stop."))
        v.addWidget(self.speed)
        v.addWidget(self.turn)
        row = QHBoxLayout()
        row.addWidget(self.rev_left)
        row.addWidget(self.rev_right)
        row.addStretch(1)
        row.addWidget(self.torque)
        v.addLayout(row)
        v.addWidget(stop)

        self.arms = ArmsPanel(self.send, self.prefs)

        # --- status
        self.status = QLabel()
        self.status.setFont(QFont("Consolas", 10))
        status = QGroupBox("Status")
        QVBoxLayout(status).addWidget(self.status)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(500)
        self.log_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(conn)
        lay.addWidget(drive)
        lay.addWidget(self.arms)
        lay.addWidget(status)
        lay.addWidget(self.log_box, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(780, screen.width()), min(940, screen.height() - 80))
        self.log_box.setMaximumHeight(100)

        wireless = True
        (self.wireless_radio if wireless else self.wired_radio).setChecked(True)
        self.pages.setCurrentIndex(1 if wireless else 0)
        self.refresh_ports()
        self.show_status(None, None)
        self.send_timer = QTimer(self, interval=round(1000 / SEND_HZ), timeout=self.tick)
        self.recv_timer = QTimer(self, interval=10, timeout=self.receive)
        self.recv_timer.start()
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------ helpers
    def log(self, text):
        self.log_box.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def refresh_ports(self):
        self.port_box.clear()
        saved = self.prefs.value("port", "")
        ports = sorted(list_ports.comports(), key=lambda p: p.device)
        pick = None
        for i, p in enumerate(ports):
            esp = p.vid == ESPRESSIF_VID
            self.port_box.addItem(f"{p.device}  -  {p.description}" + ("  (ESP32)" if esp else ""), p.device)
            if p.device == saved or (pick is None and esp):
                pick = i
        if pick is not None:
            self.port_box.setCurrentIndex(pick)
        if not ports:
            self.port_box.addItem("(no serial ports - plug the robot's USB-C cable in, then Refresh)", None)

    def signs(self):
        return (-1 if self.rev_left.isChecked() else 1, -1 if self.rev_right.isChecked() else 1)

    def show_status(self, speeds, volts):
        gl, gr = self.goal
        al, ar = speeds if speeds else (float("nan"), float("nan"))
        vtxt = "--" if not volts else f"{volts:.1f} V" + ("  LOW - charge the battery" if volts < 10.5 else "")
        self.status.setText(f"            goal     actual\n"
                            f"Left   {gl:+7.1f}   {al:+7.1f} rpm\n"
                            f"Right  {gr:+7.1f}   {ar:+7.1f} rpm\n"
                            f"Battery  {vtxt}")

    def send(self, text):
        if self.link:
            try:
                self.link.send(text)
            except LinkError as e:
                self.disconnect(f"{e}. Connect again when it's back.")

    # ------------------------------------------------------------ connection
    def find(self):
        self.log("Looking for robots on this Wi-Fi ...")
        # Broadcast, plus the robot's fixed address: with Ethernet also plugged in, Windows
        # often sends broadcasts out of the Ethernet port only.
        for target in ("255.255.255.255", DEFAULT_ADDRESS):
            try:
                self.sock.sendto(b"FIND", (target, UDP_PORT))
            except OSError:
                pass

    def toggle_connect(self):
        if self.link:
            self.disconnect("Disconnected")
            return
        try:
            if self.wired_radio.isChecked():
                port = self.port_box.currentData()
                if not port:
                    self.log("No port selected")
                    return
                self.link = WiredLink(port)
                self.prefs.setValue("port", port)
            else:
                address = self.address.text().strip()
                if not address:
                    self.log("Type the robot's address (192.168.4.1) or press Find robot")
                    return
                self.link = WirelessLink(self.sock, address)
                self.prefs.setValue("address", address)
        except LinkError as e:
            self.log(f"Cannot connect: {e}")
            return
        self.prefs.setValue("mode", "wired" if self.wired_radio.isChecked() else "wireless")
        self.connect_t = time.monotonic()
        self.last_state_t = 0.0
        self.link_ok = False
        self.motors_ok = None
        for w in self.conn_widgets:
            w.setEnabled(False)
        self.connect_btn.setText("Disconnect")
        self.link_label.setText(f"Waiting for the robot on {self.link.name()} ...")
        mode = "wired" if isinstance(self.link, WiredLink) else "wireless"
        self.send("FIND")
        self.send("TORQUE %d" % self.torque.isChecked())
        if not self.link:
            return
        self.send_timer.start()
        self.setFocus()
        self.log(f"Connecting ({mode}, {self.link.name()}). Click this window and hold W/A/S/D to drive.")

    def disconnect(self, reason):
        self.held.clear()
        self.goal = (0.0, 0.0)
        self.send_timer.stop()
        link, self.link = self.link, None
        self.arms.reset()
        if link:
            try:
                link.send("DRIVE 0 0")
                link.send("ARM 0 50 0 0 0")
            except LinkError:
                pass
            link.close()
        self.motion.setText("STOPPED")
        for w in self.conn_widgets:
            w.setEnabled(True)
        self.connect_btn.setText("Connect")
        self.link_label.setText("Not connected")
        self.show_status(None, None)
        self.log(reason)

    def receive(self):
        messages = []
        if self.link:
            try:
                messages += self.link.poll()
            except LinkError as e:
                self.disconnect(f"{e}. Plug it back in, then Connect.")
        messages += self.receive_udp()
        for kind, rest in messages:
            if kind == "STATE":
                self.on_state(rest.split())
            elif kind in ("ARMS", "ARMCFG"):
                self.arms.receive(kind, rest)
            elif kind == "MSG":
                self.log(f"robot: {rest}")
            elif kind == "HERE":
                pass                        # handled in receive_udp / connect
            elif self.link:
                self.log(f"esp32: {kind} {rest}".rstrip())   # boot messages, crash reports
        if self.link:
            self.check_link()

    def receive_udp(self):
        out = []
        for _ in range(128):                 # Bound receive work so heartbeats cannot starve.
            try:
                data, addr = self.sock.recvfrom(512)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:                 # Do not spin forever on a broken socket.
                break
            kind, rest = split(data.decode(errors="replace"))
            if kind == "HERE" and not self.link:
                self.log(f"  found {rest} at {addr[0]}")
                self.address.setText(addr[0])
            elif isinstance(self.link, WirelessLink) and addr == self.link.robot:
                out.append((kind, rest))
        return out

    def on_state(self, f):
        try:
            l, r, volts, torque, ok = float(f[0]), float(f[1]), float(f[2]), int(f[3]), int(f[4])
        except (ValueError, IndexError):
            return
        self.last_state_t = time.monotonic()
        self.motors_ok = ok
        if torque != self.torque.isChecked():
            self.send("TORQUE %d" % self.torque.isChecked())    # ESP32 restarted: re-apply
        ls, rs = self.signs()
        self.show_status((l * ls, r * rs) if ok else None, volts if ok else None)

    def check_link(self):
        ok = time.monotonic() - self.last_state_t < LINK_TIMEOUT_S
        if ok:
            self.link_label.setText(f"Connected ({self.link.name()}). Motors: " +
                                    ("OK" if self.motors_ok else "NOT READY (battery on? see log)"))
        elif self.link_ok or time.monotonic() - self.connect_t > 2.0:
            self.link_label.setText(self.link.no_reply_hint())
            if self.link_ok:
                self.stop()
                self.log("Robot stopped answering (it stops the wheels by itself)")
                self.show_status(None, None)
        self.link_ok = ok

    # ------------------------------------------------------------ driving
    def stop(self):
        self.held.clear()
        self.arms.stop()
        self.motion.setText(motion_name(self.held))
        if self.link:
            self.tick()                     # send the stop right away

    def tick(self):
        if not self.link_ok:
            self.held.clear()
        self.goal = wheel_speeds(self.held, self.speed.value(), self.turn.value())
        ls, rs = self.signs()
        self.send("DRIVE %.1f %.1f" % (self.goal[0] * ls, self.goal[1] * rs))
        self.arms.tick()

    # ------------------------------------------------------------ keyboard
    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            if isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox)):
                if t == QEvent.Type.KeyPress and ev.key() in STOP_KEYS:
                    self.stop()
                if t == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
                    self.setFocus()         # Esc leaves the address box
                    return True
                return False                # typing in a box, not driving
            key = ev.key()
            if key in KEYS or key in STOP_KEYS:
                if not ev.isAutoRepeat():
                    if key in STOP_KEYS:
                        if t == QEvent.Type.KeyPress:
                            self.stop()
                    elif t == QEvent.Type.KeyPress:
                        self.held.add(KEYS[key])
                    else:
                        self.held.discard(KEYS[key])
                    self.motion.setText(motion_name(self.held))
                return True
        elif t == QEvent.Type.ApplicationDeactivate:
            self.stop()                     # key releases are lost when the window loses focus
        elif t == QEvent.Type.FocusIn and isinstance(obj, (QLineEdit, QAbstractSpinBox)):
            self.stop()
        return False

    def closeEvent(self, ev):
        self.prefs.setValue("rev_left", self.rev_left.isChecked())
        self.prefs.setValue("rev_right", self.rev_right.isChecked())
        if self.link:
            self.disconnect("Closing")
        ev.accept()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

"""
MotorWatch IQ — Settings Tab (M7)
===================================
Tab 4 do launcher.py — configuração de conexões e thresholds por motor.

Integração no launcher.py:
    from settings_tab import SettingsTab
    self.settings_tab = SettingsTab()
    self.settings_tab.settings_applied.connect(self._on_settings_applied)
    tabs.addTab(self.settings_tab, "⚙  Settings")

Sinais emitidos:
    settings_saved()   — settings.json gravado, serviços não tocados
    settings_applied() — solicitação de restart com novos valores

Fluxo:
    Editar campos → Save (grava JSON, badge "pending restart" aparece)
                  → Apply (confirm dialog → launcher para + reinicia serviços)
    Reset to defaults → restaura ISO para a grandeza/motor seleccionado
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget, QComboBox, QMessageBox,
)

from settings_loader import (
    ISO_DEFAULTS, SETTINGS_PATH, get_plc_config, load_settings, save_settings,
)

# ─── Catppuccin Mocha ─────────────────────────────────────────────────────────
MOCHA = {
    "base":     "#1e1e2e", "mantle":   "#181825", "crust":    "#11111b",
    "surface0": "#313244", "surface1": "#45475a", "surface2": "#585b70",
    "overlay0": "#6c7086", "overlay1": "#7f849c",
    "text":     "#cdd6f4", "subtext0": "#a6adc8", "subtext1": "#bac2de",
    "red":      "#f38ba8", "yellow":   "#f9e2af", "green":    "#a6e3a1",
    "blue":     "#89b4fa", "mauve":    "#cba6f7", "teal":     "#94e2d5",
    "peach":    "#fab387",
}

# Labels legíveis para cada grandeza
_THRESHOLD_LABELS: dict[str, tuple[str, str, float, float]] = {
    # key: (label_prealarm, label_alarm, min, max)
    "vrms":  ("v-RMS Pre-alarm (mm/s)",  "v-RMS Alarm (mm/s)",  0.0,   50.0),
    "apeak": ("a-Peak Pre-alarm (mg)",   "a-Peak Alarm (mg)",   0.0,   16000.0),
    "temp":  ("Temp Pre-alarm (°C)",     "Temp Alarm (°C)",     0.0,   200.0),
}

_PARAM_MAP = {
    # key → (prealarm_field, alarm_field)
    "vrms":  ("vrms_prealarm_mms",  "vrms_alarm_mms"),
    "apeak": ("apeak_prealarm_mg",  "apeak_alarm_mg"),
    "temp":  ("temp_prealarm_c",    "temp_alarm_c"),
}


def _make_label(text: str, size: int = 10, bold: bool = False,
                color: str = MOCHA["text"]) -> QLabel:
    lbl = QLabel(text)
    font = QFont("Consolas", size)
    font.setBold(bold)
    lbl.setFont(font)
    lbl.setStyleSheet(f"color: {color};")
    return lbl


def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color: {MOCHA['surface1']};")
    return f


def _spinbox(min_val: float, max_val: float, step: float = 0.1,
             decimals: int = 1) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setMinimum(min_val)
    sb.setMaximum(max_val)
    sb.setSingleStep(step)
    sb.setDecimals(decimals)
    sb.setFont(QFont("Consolas", 10))
    sb.setStyleSheet(f"""
        QDoubleSpinBox {{
            background: {MOCHA['surface1']};
            color: {MOCHA['text']};
            border: 1px solid {MOCHA['surface2']};
            border-radius: 4px;
            padding: 2px 6px;
        }}
        QDoubleSpinBox:focus {{ border-color: {MOCHA['blue']}; }}
    """)
    return sb


def _int_spinbox(min_val: int, max_val: int, step: int = 1) -> QSpinBox:
    sb = QSpinBox()
    sb.setMinimum(min_val)
    sb.setMaximum(max_val)
    sb.setSingleStep(step)
    sb.setFont(QFont("Consolas", 10))
    sb.setStyleSheet(f"""
        QSpinBox {{
            background: {MOCHA['surface1']};
            color: {MOCHA['text']};
            border: 1px solid {MOCHA['surface2']};
            border-radius: 4px;
            padding: 2px 6px;
        }}
        QSpinBox:focus {{ border-color: {MOCHA['blue']}; }}
    """)
    return sb


def _lineedit(placeholder: str = "", password: bool = False) -> QLineEdit:
    le = QLineEdit()
    le.setPlaceholderText(placeholder)
    if password:
        le.setEchoMode(QLineEdit.Password)
    le.setFont(QFont("Consolas", 10))
    le.setStyleSheet(f"""
        QLineEdit {{
            background: {MOCHA['surface1']};
            color: {MOCHA['text']};
            border: 1px solid {MOCHA['surface2']};
            border-radius: 4px;
            padding: 2px 8px;
            min-height: 24px;
        }}
        QLineEdit:focus {{ border-color: {MOCHA['blue']}; }}
    """)
    return le


# ══════════════════════════════════════════════════════════════════════════════
# ThresholdMotorWidget — bloco de thresholds para 1 motor
# ══════════════════════════════════════════════════════════════════════════════

class ThresholdMotorWidget(QGroupBox):
    """
    Bloco de configuração de thresholds para um motor.
    Cada grandeza (vrms/apeak/temp) tem checkbox "Use ISO default".
    Quando marcado, os spinboxes ficam desactivados e mostram o valor ISO.
    """

    changed = Signal()  # qualquer alteração

    def __init__(self, motor_id: str, parent=None):
        super().__init__(f"Motor {motor_id}", parent)
        self.motor_id = motor_id
        self._spinboxes: dict[str, QDoubleSpinBox] = {}
        self._checks:   dict[str, QCheckBox] = {}
        self._build()
        self.load_from_settings()

    def _build(self):
        self.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.setStyleSheet(f"""
            QGroupBox {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                color: {MOCHA['blue']};
                font-family: Consolas;
                font-size: 10pt;
                font-weight: bold;
            }}
        """)

        grid = QGridLayout(self)
        grid.setSpacing(6)
        grid.setContentsMargins(12, 16, 12, 10)

        # Cabeçalho de colunas
        for col, txt in enumerate(("Parameter", "Pre-alarm", "Alarm", "Use ISO default")):
            lbl = _make_label(txt, 9, bold=True, color=MOCHA["subtext0"])
            grid.addWidget(lbl, 0, col)

        grid.addWidget(_hline(), 1, 0, 1, 4)

        row = 2
        for param_key, (lbl_pre, lbl_alm, vmin, vmax) in _THRESHOLD_LABELS.items():
            pre_field, alm_field = _PARAM_MAP[param_key]

            # Spinboxes
            decimals = 1
            step     = 0.1 if param_key != "apeak" else 10.0
            if param_key == "apeak":
                decimals = 0
            sb_pre = _spinbox(vmin, vmax, step, decimals)
            sb_alm = _spinbox(vmin, vmax, step, decimals)

            # Checkbox ISO default
            chk = QCheckBox("ISO default")
            chk.setFont(QFont("Consolas", 10))
            chk.setStyleSheet(f"""
                QCheckBox {{ color: {MOCHA['subtext0']}; spacing: 6px; }}
                QCheckBox::indicator {{
                    width: 14px; height: 14px;
                    border: 1px solid {MOCHA['surface2']};
                    border-radius: 3px;
                    background: {MOCHA['surface1']};
                }}
                QCheckBox::indicator:checked {{
                    background: {MOCHA['blue']};
                    border-color: {MOCHA['blue']};
                    image: none;
                }}
            """)

            # Conectar checkbox → habilitar/desabilitar spinboxes
            def _on_check(state, pre=sb_pre, alm=sb_alm, pk=param_key):
                enabled = (state == 0)  # 0 = unchecked
                pre.setEnabled(enabled)
                alm.setEnabled(enabled)
                if not enabled:
                    # Restaura visualmente os valores ISO
                    iso_pre, iso_alm = _PARAM_MAP[pk]
                    pre.setValue(ISO_DEFAULTS[iso_pre])
                    alm.setValue(ISO_DEFAULTS[iso_alm])
                self.changed.emit()

            chk.stateChanged.connect(_on_check)
            sb_pre.valueChanged.connect(lambda _: self.changed.emit())
            sb_alm.valueChanged.connect(lambda _: self.changed.emit())

            # Coluna 0: nome do parâmetro
            param_lbl = _make_label(param_key.upper(), 10, bold=True, color=MOCHA["text"])
            grid.addWidget(param_lbl, row, 0)
            grid.addWidget(sb_pre,   row, 1)
            grid.addWidget(sb_alm,   row, 2)
            grid.addWidget(chk,      row, 3)

            self._spinboxes[pre_field] = sb_pre
            self._spinboxes[alm_field] = sb_alm
            self._checks[param_key]    = chk
            row += 1

        grid.setColumnStretch(0, 1)

    def load_from_settings(self):
        """Lê settings.json e popula os campos."""
        data   = load_settings()
        motors = data.get("thresholds", {}).get("motors", {})
        motor_cfg = motors.get(self.motor_id, {})

        for param_key in ("vrms", "apeak", "temp"):
            pre_field, alm_field = _PARAM_MAP[param_key]
            pre_entry = motor_cfg.get(pre_field, {"value": ISO_DEFAULTS[pre_field], "use_default": True})
            alm_entry = motor_cfg.get(alm_field, {"value": ISO_DEFAULTS[alm_field], "use_default": True})
            use_default = pre_entry.get("use_default", True)

            sb_pre = self._spinboxes[pre_field]
            sb_alm = self._spinboxes[alm_field]
            chk    = self._checks[param_key]

            sb_pre.blockSignals(True)
            sb_alm.blockSignals(True)
            chk.blockSignals(True)

            sb_pre.setValue(float(pre_entry.get("value", ISO_DEFAULTS[pre_field])))
            sb_alm.setValue(float(alm_entry.get("value", ISO_DEFAULTS[alm_field])))
            chk.setChecked(use_default)
            sb_pre.setEnabled(not use_default)
            sb_alm.setEnabled(not use_default)

            sb_pre.blockSignals(False)
            sb_alm.blockSignals(False)
            chk.blockSignals(False)

    def collect(self) -> dict:
        """Devolve dicionário pronto para settings.json['thresholds']['motors'][mid]."""
        result = {}
        for param_key in ("vrms", "apeak", "temp"):
            pre_field, alm_field = _PARAM_MAP[param_key]
            use_default = self._checks[param_key].isChecked()
            result[pre_field] = {
                "value":       self._spinboxes[pre_field].value(),
                "use_default": use_default,
            }
            result[alm_field] = {
                "value":       self._spinboxes[alm_field].value(),
                "use_default": use_default,
            }
        return result


# ══════════════════════════════════════════════════════════════════════════════
# ConnectionsWidget — OPC UA + Snap7 + modo default
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionsWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self.load_from_settings()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # ── OPC UA ────────────────────────────────────────────────────────────
        opcua_group = self._make_group("OPC UA (primary)")
        opcua_grid = QGridLayout()
        opcua_grid.setSpacing(8)
        opcua_grid.setColumnStretch(1, 1)

        self._opc_ip       = _lineedit("192.168.0.10")
        self._opc_port     = _int_spinbox(1, 65535)
        self._opc_url      = _lineedit("opc.tcp://...")
        self._opc_security = QComboBox()
        for s in ("None", "Basic128Rsa15", "Basic256", "Basic256Sha256"):
            self._opc_security.addItem(s)
        self._opc_security.setFont(QFont("Consolas", 10))
        self._opc_security.setStyleSheet(f"""
            QComboBox {{
                background: {MOCHA['surface1']}; color: {MOCHA['text']};
                border: 1px solid {MOCHA['surface2']}; border-radius: 4px;
                padding: 2px 8px; font-family: Consolas; font-size: 10px;
            }}
            QComboBox QAbstractItemView {{
                background: {MOCHA['surface0']}; color: {MOCHA['text']};
                selection-background-color: {MOCHA['surface1']};
            }}
        """)
        self._opc_user     = _lineedit("(optional)")
        self._opc_password = _lineedit("(optional)", password=True)

        for w in (self._opc_ip, self._opc_url, self._opc_user, self._opc_password):
            w.textChanged.connect(lambda _: self.changed.emit())
        self._opc_port.valueChanged.connect(lambda _: self._update_opc_url())
        self._opc_ip.textChanged.connect(lambda _: self._update_opc_url())
        self._opc_security.currentIndexChanged.connect(lambda _: self.changed.emit())

        fields = [
            ("IP Address",      self._opc_ip),
            ("Port",            self._opc_port),
            ("URL",             self._opc_url),
            ("Security Policy", self._opc_security),
            ("Username",        self._opc_user),
            ("Password",        self._opc_password),
        ]
        for i, (lbl_txt, widget) in enumerate(fields):
            opcua_grid.addWidget(_make_label(lbl_txt, 10, color=MOCHA["subtext0"]), i, 0)
            opcua_grid.addWidget(widget, i, 1)

        opcua_group.layout().addLayout(opcua_grid)
        root.addWidget(opcua_group)

        # ── Snap7 ─────────────────────────────────────────────────────────────
        snap7_group = self._make_group("Snap7 (fallback)")
        snap7_grid = QGridLayout()
        snap7_grid.setSpacing(8)
        snap7_grid.setColumnStretch(1, 1)

        self._s7_rack     = _int_spinbox(0, 7)
        self._s7_slot     = _int_spinbox(0, 31)
        self._s7_interval = _int_spinbox(100, 10000, 100)

        for w in (self._s7_rack, self._s7_slot, self._s7_interval):
            w.valueChanged.connect(lambda _: self.changed.emit())

        snap7_fields = [
            ("Rack",                 self._s7_rack),
            ("Slot",                 self._s7_slot),
            ("Poll interval (ms)",   self._s7_interval),
        ]
        for i, (lbl_txt, widget) in enumerate(snap7_fields):
            snap7_grid.addWidget(_make_label(lbl_txt, 10, color=MOCHA["subtext0"]), i, 0)
            snap7_grid.addWidget(widget, i, 1)

        snap7_group.layout().addLayout(snap7_grid)
        root.addWidget(snap7_group)
        root.addStretch()

    def _make_group(self, title: str) -> QGroupBox:
        g = QGroupBox(title)
        g.setFont(QFont("Segoe UI", 10, QFont.Bold))
        g.setStyleSheet(f"""
            QGroupBox {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                color: {MOCHA['teal']};
                font-family: Consolas;
                font-size: 10pt;
                font-weight: bold;
            }}
        """)
        g.setLayout(QVBoxLayout())
        g.layout().setContentsMargins(12, 16, 12, 10)
        g.layout().setSpacing(8)
        return g

    def _update_opc_url(self):
        """Auto-preenche o campo URL quando IP ou porta mudam."""
        ip   = self._opc_ip.text().strip() or "192.168.0.10"
        port = self._opc_port.value()
        url  = f"opc.tcp://{ip}:{port}"
        self._opc_url.blockSignals(True)
        self._opc_url.setText(url)
        self._opc_url.blockSignals(False)
        self.changed.emit()

    def load_from_settings(self):
        cfg = get_plc_config()
        # OPC UA
        opc = cfg.get("opcua", {})
        self._opc_ip.setText(opc.get("ip", "192.168.0.10"))
        self._opc_port.setValue(int(opc.get("port", 4840)))
        self._opc_url.setText(opc.get("url", ""))
        sec_idx = self._opc_security.findText(opc.get("security", "None"))
        if sec_idx >= 0:
            self._opc_security.setCurrentIndex(sec_idx)
        self._opc_user.setText(opc.get("user", ""))
        self._opc_password.setText(opc.get("password", ""))
        # Snap7
        s7 = cfg.get("snap7", {})
        self._s7_rack.setValue(int(s7.get("rack", 0)))
        self._s7_slot.setValue(int(s7.get("slot", 1)))
        self._s7_interval.setValue(int(s7.get("poll_interval_ms", 1000)))

    def collect(self) -> dict:
        return {
            "opcua": {
                "ip":       self._opc_ip.text().strip(),
                "port":     self._opc_port.value(),
                "url":      self._opc_url.text().strip(),
                "security": self._opc_security.currentText(),
                "user":     self._opc_user.text().strip(),
                "password": self._opc_password.text(),
            },
            "snap7": {
                "rack":             self._s7_rack.value(),
                "slot":             self._s7_slot.value(),
                "poll_interval_ms": self._s7_interval.value(),
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
# SettingsTab — Tab 4 principal
# ══════════════════════════════════════════════════════════════════════════════

class SettingsTab(QWidget):
    """
    Tab 4 — Settings.
    Emite settings_saved() e settings_applied() para o launcher reagir.
    """

    settings_saved   = Signal()   # JSON gravado, sem restart
    settings_applied = Signal()   # pedido de restart com novos valores

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending = False      # True = saved mas não applied
        self._build()
        self._mark_clean()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        # Header
        hdr = QLabel("Settings")
        hdr.setFont(QFont("Segoe UI", 18, QFont.Bold))
        hdr.setStyleSheet(f"color: {MOCHA['blue']}; letter-spacing: 1px;")
        sub = QLabel("Connections, thresholds and default startup mode")
        sub.setFont(QFont("Segoe UI", 10))
        sub.setStyleSheet(f"color: {MOCHA['subtext0']};")
        root.addWidget(hdr)
        root.addWidget(sub)
        root.addWidget(_hline())

        # Pending badge
        self._lbl_pending = QLabel("⚠  Pending restart — click Apply to activate changes")
        self._lbl_pending.setFont(QFont("Consolas", 10, QFont.Bold))
        self._lbl_pending.setStyleSheet(f"""
            color: {MOCHA['yellow']};
            background: #3d2f0e;
            border: 1px solid {MOCHA['yellow']}60;
            border-radius: 5px;
            padding: 5px 12px;
        """)
        self._lbl_pending.setVisible(False)
        root.addWidget(self._lbl_pending)

        # Inner tabs: Connections | Thresholds
        inner_tabs = QTabWidget()
        inner_tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background: {MOCHA['base']}; }}
            QTabBar::tab {{
                background: {MOCHA['surface0']}; color: {MOCHA['subtext0']};
                font-family: Consolas; font-size: 10px;
                padding: 6px 18px; border: none;
                border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                background: {MOCHA['base']}; color: {MOCHA['teal']};
                border-bottom: 2px solid {MOCHA['teal']};
            }}
            QTabBar::tab:hover {{ color: {MOCHA['text']}; }}
        """)

        # Tab: Connections
        self._conn_widget = ConnectionsWidget()
        self._conn_widget.changed.connect(self._mark_dirty)
        inner_tabs.addTab(self._conn_widget, "🔌  Connections")

        # Tab: Thresholds
        thr_scroll = QScrollArea()
        thr_scroll.setWidgetResizable(True)
        thr_scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {MOCHA['base']}; }}")
        thr_container = QWidget()
        thr_container.setStyleSheet(f"background: {MOCHA['base']};")
        thr_layout = QGridLayout(thr_container)
        thr_layout.setSpacing(10)
        thr_layout.setContentsMargins(4, 4, 4, 4)

        self._motor_widgets: dict[str, ThresholdMotorWidget] = {}
        for i, mid in enumerate(("1", "2", "3", "4")):
            w = ThresholdMotorWidget(mid)
            w.changed.connect(self._mark_dirty)
            self._motor_widgets[mid] = w
            thr_layout.addWidget(w, i // 2, i % 2)

        thr_scroll.setWidget(thr_container)
        inner_tabs.addTab(thr_scroll, "📊  Thresholds")

        root.addWidget(inner_tabs, 1)
        root.addWidget(_hline())

        # Barra de botões
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(8)

        self._btn_reset   = self._make_btn("↺  Reset to ISO defaults", MOCHA["overlay1"])
        self._btn_save    = self._make_btn("💾  Save",                  MOCHA["teal"])
        self._btn_apply   = self._make_btn("▶  Apply & Restart",        MOCHA["green"])

        self._btn_reset.clicked.connect(self._reset_to_defaults)
        self._btn_save.clicked.connect(self._save)
        self._btn_apply.clicked.connect(self._apply)
        self._btn_apply.setEnabled(False)

        btn_bar.addWidget(self._btn_reset)
        btn_bar.addStretch()
        btn_bar.addWidget(self._btn_save)
        btn_bar.addWidget(self._btn_apply)
        root.addLayout(btn_bar)

    def _make_btn(self, text: str, color: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedHeight(34)
        btn.setFont(QFont("Consolas", 10, QFont.Bold))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {MOCHA['surface0']}; color: {color};
                border: 1px solid {color}50; border-radius: 6px;
                padding: 0 16px;
            }}
            QPushButton:hover   {{ background: {color}20; border-color: {color}; }}
            QPushButton:pressed {{ background: {color}35; }}
            QPushButton:disabled {{
                color: {MOCHA['overlay0']};
                border-color: {MOCHA['surface1']};
                background: {MOCHA['surface0']};
            }}
        """)
        return btn

    # ── Estado dirty/clean ────────────────────────────────────────────────────

    def _mark_dirty(self):
        """Chamado quando qualquer campo é editado."""
        self._btn_save.setStyleSheet(
            self._btn_save.styleSheet().replace(MOCHA["teal"], MOCHA["yellow"])
        )
        # Não marca pending ainda — só depois de Save

    def _mark_clean(self):
        self._pending = False
        self._lbl_pending.setVisible(False)
        self._btn_apply.setEnabled(False)

    def _mark_pending(self):
        self._pending = True
        self._lbl_pending.setVisible(True)
        self._btn_apply.setEnabled(True)

    # ── Acções ────────────────────────────────────────────────────────────────

    def _collect_all(self) -> dict:
        data = load_settings()
        data["plc"] = self._conn_widget.collect()
        motors = data.setdefault("thresholds", {}).setdefault("motors", {})
        for mid, w in self._motor_widgets.items():
            motors[mid] = w.collect()
        return data

    def _save(self):
        data = self._collect_all()
        ok   = save_settings(data)
        if ok:
            self._mark_pending()
            # Restaura cor do botão Save
            self._btn_save.setStyleSheet(
                self._make_btn("", MOCHA["teal"]).styleSheet()
            )
            # Recarrega widgets com os dados gravados
            self._conn_widget.load_from_settings()
            for w in self._motor_widgets.values():
                w.load_from_settings()
            self.settings_saved.emit()
        else:
            QMessageBox.critical(self, "MotorWatch IQ",
                                 f"Failed to save settings.\nCheck permissions for:\n{SETTINGS_PATH}")

    def _apply(self):
        reply = QMessageBox.question(
            self,
            "Apply & Restart",
            "Services will be stopped and restarted with the new settings.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._mark_clean()
            self.settings_applied.emit()

    def _reset_to_defaults(self):
        reply = QMessageBox.question(
            self,
            "Reset to ISO Defaults",
            "All thresholds for all motors will be reset to ISO 20816-3 values.\n\n"
            "This will not affect connection settings.\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            for w in self._motor_widgets.values():
                for param_key in ("vrms", "apeak", "temp"):
                    chk = w._checks[param_key]
                    chk.blockSignals(True)
                    chk.setChecked(True)
                    chk.blockSignals(False)
                    chk.stateChanged.emit(2)  # força atualização dos spinboxes
            self._mark_dirty()

    # ── API pública (usada pelo launcher) ─────────────────────────────────────

    def has_pending(self) -> bool:
        return self._pending

    def reload(self):
        """Recarrega todos os campos a partir do settings.json."""
        self._conn_widget.load_from_settings()
        for w in self._motor_widgets.values():
            w.load_from_settings()
        self._mark_clean()

"""Native, local-only administration window for Nivelle Core."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .app import Services


def _display_time(value: object) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class CoreAdminWindow(QMainWindow):
    """Present Core security state without holding any authentication token."""

    refresh_requested = Signal()
    pairing_code_requested = Signal()
    admin_change_requested = Signal(str, bool)
    revoke_requested = Signal(str)

    def __init__(self, data_directory: str) -> None:
        super().__init__()
        self.setObjectName("coreAdminWindow")
        self.setWindowTitle("Nivelle Core · 로컬 보안 관리")
        self.resize(1040, 760)
        self._clients: dict[str, dict[str, object]] = {}
        self._pairing_code: str | None = None

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Nivelle Core 보안 관리")
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        subtitle = QLabel(
            "이 창은 Core PC에서만 동작하며 서버 신원, 페어링, 관리자 권한을 관리합니다."
        )
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        self.gateway_badge = QLabel("Core 시작 중")
        self.gateway_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gateway_badge.setMinimumWidth(120)
        self.gateway_badge.setStyleSheet(
            "padding: 8px 12px; border-radius: 12px; "
            "background: #6b7280; color: white; font-weight: 600;"
        )
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        header.addLayout(titles, 1)
        header.addWidget(self.gateway_badge)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        identity_group = QGroupBox("Core 신원 및 네트워크")
        identity_layout = QVBoxLayout(identity_group)
        self.server_id_label = QLabel("서버 ID: 준비 중")
        self.server_id_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.network_label = QLabel("네트워크: 준비 중")
        self.network_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.data_directory_label = QLabel(f"데이터 폴더: {data_directory}")
        self.data_directory_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        identity_layout.addWidget(self.server_id_label)
        identity_layout.addWidget(self.network_label)
        identity_layout.addWidget(self.data_directory_label)
        layout.addWidget(identity_group)

        pairing_group = QGroupBox("새 Link 인증")
        pairing_layout = QHBoxLayout(pairing_group)
        pairing_text = QVBoxLayout()
        pairing_notice = QLabel(
            "일회용 6자리 코드는 10분 동안 유효합니다. 신뢰하는 Link PC에만 전달하세요."
        )
        pairing_notice.setWordWrap(True)
        self.pairing_code_label = QLabel("------")
        code_font = QFont()
        code_font.setPointSize(28)
        code_font.setBold(True)
        code_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        self.pairing_code_label.setFont(code_font)
        self.pairing_code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.pairing_expiry_label = QLabel("코드를 불러오는 중입니다.")
        pairing_text.addWidget(pairing_notice)
        pairing_text.addWidget(self.pairing_code_label)
        pairing_text.addWidget(self.pairing_expiry_label)
        pairing_buttons = QVBoxLayout()
        self.issue_code_button = QPushButton("새 코드 발급")
        self.copy_code_button = QPushButton("코드 복사")
        self.issue_code_button.clicked.connect(self.pairing_code_requested.emit)
        self.copy_code_button.clicked.connect(self._copy_pairing_code)
        self.copy_code_button.setEnabled(False)
        pairing_buttons.addWidget(self.issue_code_button)
        pairing_buttons.addWidget(self.copy_code_button)
        pairing_buttons.addStretch()
        pairing_layout.addLayout(pairing_text, 1)
        pairing_layout.addLayout(pairing_buttons)
        layout.addWidget(pairing_group)

        clients_group = QGroupBox("인증된 Link 클라이언트")
        clients_layout = QVBoxLayout(clients_group)
        self.clients_table = QTableWidget(0, 6)
        self.clients_table.setHorizontalHeaderLabels(
            ["장치", "권한", "인증", "연결", "마지막 사용", "클라이언트 ID"]
        )
        self.clients_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.clients_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.clients_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.clients_table.verticalHeader().setVisible(False)
        header_view = self.clients_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.clients_table.itemSelectionChanged.connect(self._update_action_state)
        clients_layout.addWidget(self.clients_table)

        actions = QHBoxLayout()
        self.client_help = QLabel(
            "마지막 관리자는 보호됩니다. 인증 해제는 해당 토큰과 현재 연결을 즉시 종료합니다."
        )
        self.client_help.setWordWrap(True)
        self.admin_button = QPushButton("관리자 권한 변경")
        self.revoke_button = QPushButton("인증 해제")
        self.admin_button.clicked.connect(self._request_admin_change)
        self.revoke_button.clicked.connect(self._request_revoke)
        actions.addWidget(self.client_help, 1)
        actions.addWidget(self.admin_button)
        actions.addWidget(self.revoke_button)
        clients_layout.addLayout(actions)
        layout.addWidget(clients_group, 1)

        security_notice = QFrame()
        security_notice.setStyleSheet(
            "QFrame { background: #eef6ff; border: 1px solid #b7d8ff; "
            "border-radius: 6px; }"
        )
        notice_layout = QVBoxLayout(security_notice)
        notice = QLabel(
            "보안: 원본 인증 토큰은 이 UI에 표시되지 않습니다. Core는 토큰의 "
            "PBKDF2-SHA256 해시만 저장하며, 이 화면의 관리 기능은 원격 HTTP 관리 "
            "경로를 만들지 않고 로컬 Core 프로세스 내부에서 실행됩니다."
        )
        notice.setWordWrap(True)
        notice_layout.addWidget(notice)
        layout.addWidget(security_notice)

        self.statusBar().showMessage("Core Gateway가 준비되기를 기다리는 중입니다.")
        self.setCentralWidget(root)
        self._update_action_state()

    def set_gateway_error(self, message: str) -> None:
        self.gateway_badge.setText("Core 오류")
        self.gateway_badge.setStyleSheet(
            "padding: 8px 12px; border-radius: 12px; "
            "background: #b91c1c; color: white; font-weight: 600;"
        )
        self.statusBar().showMessage(message)

    def apply_snapshot(self, snapshot: dict[str, object]) -> None:
        server_id = str(snapshot.get("server_id") or "준비 중")
        self.server_id_label.setText(f"서버 ID: {server_id}")
        network = snapshot.get("network")
        if isinstance(network, dict):
            bind = str(network.get("bind_endpoint") or "-")
            advertised = str(network.get("advertised_endpoint") or "감지되지 않음")
            source = str(network.get("advertised_source") or "-")
            self.network_label.setText(
                f"수신: {bind}  ·  Link 접속 주소: {advertised}  ·  출처: {source}"
            )
        else:
            self.network_label.setText("네트워크: 런타임 정보 없음")

        self.gateway_badge.setText("Core 실행 중")
        self.gateway_badge.setStyleSheet(
            "padding: 8px 12px; border-radius: 12px; "
            "background: #15803d; color: white; font-weight: 600;"
        )
        pairing = snapshot.get("pairing")
        if isinstance(pairing, dict):
            code = pairing.get("code")
            self._pairing_code = str(code) if code else None
            self.pairing_code_label.setText(self._pairing_code or "------")
            self.copy_code_button.setEnabled(self._pairing_code is not None)
            if self._pairing_code is not None:
                self.pairing_expiry_label.setText(
                    f"만료: {_display_time(pairing.get('expires_at'))}"
                )
            else:
                self.pairing_expiry_label.setText("현재 유효한 코드가 없습니다.")

        clients = snapshot.get("clients")
        client_rows = clients if isinstance(clients, list) else []
        selected_id = self._selected_client_id()
        self._clients = {
            str(item.get("id")): item
            for item in client_rows
            if isinstance(item, dict) and item.get("id")
        }
        self.clients_table.setRowCount(len(self._clients))
        for row_index, client in enumerate(self._clients.values()):
            client_id = str(client["id"])
            revoked = bool(client.get("revoked_at"))
            values = (
                str(client.get("name") or "이름 없음"),
                "관리자" if bool(client.get("is_admin")) else "일반",
                "해제됨" if revoked else "유효",
                "온라인" if bool(client.get("online")) and not revoked else "오프라인",
                _display_time(client.get("last_seen_at")),
                client_id,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, client_id)
                self.clients_table.setItem(row_index, column, item)
            if client_id == selected_id:
                self.clients_table.selectRow(row_index)
        self._update_action_state()
        self.statusBar().showMessage(
            f"인증 기록 {len(self._clients)}개 · 방금 새로고침", 5000
        )

    def show_pairing_code(self, code: str, expires_at: object) -> None:
        self._pairing_code = code
        self.pairing_code_label.setText(code)
        self.pairing_expiry_label.setText(f"만료: {_display_time(expires_at)}")
        self.copy_code_button.setEnabled(True)

    def show_operation_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)
        QMessageBox.warning(self, "Core 보안 관리", message)

    def _selected_client_id(self) -> str | None:
        row = self.clients_table.currentRow()
        if row < 0:
            return None
        item = self.clients_table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return str(value) if value else None

    def _selected_client(self) -> dict[str, object] | None:
        client_id = self._selected_client_id()
        return self._clients.get(client_id) if client_id is not None else None

    def _update_action_state(self) -> None:
        client = self._selected_client()
        active = client is not None and not bool(client.get("revoked_at"))
        self.admin_button.setEnabled(active)
        self.revoke_button.setEnabled(active)
        if active and client is not None:
            self.admin_button.setText(
                "일반 권한으로 변경"
                if bool(client.get("is_admin"))
                else "관리자 권한 부여"
            )
        else:
            self.admin_button.setText("관리자 권한 변경")

    def _request_admin_change(self) -> None:
        client = self._selected_client()
        if client is None:
            return
        client_id = str(client["id"])
        enabled = not bool(client.get("is_admin"))
        action = "부여" if enabled else "해제"
        answer = QMessageBox.question(
            self,
            "관리자 권한 변경",
            f"'{client.get('name')}' 장치의 관리자 권한을 {action}하시겠습니까?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.admin_change_requested.emit(client_id, enabled)

    def _request_revoke(self) -> None:
        client = self._selected_client()
        if client is None:
            return
        answer = QMessageBox.warning(
            self,
            "Link 인증 해제",
            f"'{client.get('name')}' 장치의 인증을 해제하시겠습니까?\n\n"
            "저장된 토큰과 현재 연결이 즉시 무효화됩니다. 다시 연결하려면 새 "
            "페어링 코드가 필요합니다.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.revoke_requested.emit(str(client["id"]))

    def _copy_pairing_code(self) -> None:
        if self._pairing_code is None:
            return
        QApplication.clipboard().setText(self._pairing_code)
        self.statusBar().showMessage("페어링 코드를 클립보드에 복사했습니다.", 3000)

    def closeEvent(self, event: QCloseEvent) -> None:
        answer = QMessageBox.question(
            self,
            "Nivelle Core 종료",
            "Core 보안 관리 창을 닫으면 Gateway도 종료됩니다. 계속하시겠습니까?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            event.accept()
        else:
            event.ignore()


class _GatewayThread(QThread):
    startup_failed = Signal(str)

    def __init__(self, app: FastAPI, host: str, port: int, log_level: str) -> None:
        super().__init__()
        config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        try:
            self.server.run()
        except (OSError, RuntimeError, SystemExit) as exc:
            self.startup_failed.emit(str(exc) or type(exc).__name__)

    def request_stop(self) -> None:
        self.server.should_exit = True


class _FutureRelay(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str, str)


class CoreAdminApplication(QObject):
    """Bridge Qt's event loop to the already running Core asyncio loop."""

    def __init__(self, services: Services, window: CoreAdminWindow) -> None:
        super().__init__(window)
        self.services = services
        self.window = window
        self.relay = _FutureRelay(self)
        self.relay.succeeded.connect(self._operation_succeeded)
        self.relay.failed.connect(self._operation_failed)
        self.window.refresh_requested.connect(self.refresh)
        self.window.pairing_code_requested.connect(self.issue_pairing_code)
        self.window.admin_change_requested.connect(self.set_admin)
        self.window.revoke_requested.connect(self.revoke)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        QTimer.singleShot(100, self.refresh)

    def refresh(self) -> None:
        self._submit("snapshot", self.services.core_admin.snapshot())

    def issue_pairing_code(self) -> None:
        self._submit("pairing", self.services.core_admin.issue_pairing_code())

    def set_admin(self, client_id: str, enabled: bool) -> None:
        self._submit(
            "set_admin",
            self.services.core_admin.set_admin(client_id, enabled=enabled),
        )

    def revoke(self, client_id: str) -> None:
        self._submit("revoke", self.services.core_admin.revoke_client(client_id))

    def _submit(self, action: str, coroutine: Any) -> None:
        loop = self.services.runtime_loop
        if loop is None or not loop.is_running():
            coroutine.close()
            self.window.statusBar().showMessage("Core Gateway가 아직 준비 중입니다.")
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        future.add_done_callback(lambda value: self._relay_result(action, value))

    def _relay_result(self, action: str, future: Future[Any]) -> None:
        try:
            value = future.result()
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the UI
            self.relay.failed.emit(action, str(exc) or type(exc).__name__)
        else:
            self.relay.succeeded.emit(action, value)

    def _operation_succeeded(self, action: str, value: object) -> None:
        if action == "snapshot" and isinstance(value, dict):
            self.window.apply_snapshot(value)
            return
        if action == "pairing" and isinstance(value, dict):
            code = value.get("code")
            if code:
                self.window.show_pairing_code(str(code), value.get("expires_at"))
        self.refresh()

    def _operation_failed(self, action: str, message: str) -> None:
        labels = {
            "snapshot": "Core 상태를 불러오지 못했습니다.",
            "pairing": "페어링 코드를 발급하지 못했습니다.",
            "set_admin": "관리자 권한을 변경하지 못했습니다.",
            "revoke": "인증을 해제하지 못했습니다.",
        }
        self.window.show_operation_error(f"{labels.get(action, '작업 실패')} {message}")


def run_core_admin_ui(
    app: FastAPI,
    *,
    host: str,
    port: int,
    log_level: str,
) -> int:
    """Run Qt in the main thread and Uvicorn in a controlled worker thread."""

    qt_app = QApplication.instance()
    owns_application = qt_app is None
    if qt_app is None:
        qt_app = QApplication([])
    services: Services = app.state.services
    window = CoreAdminWindow(str(services.root))
    controller = CoreAdminApplication(services, window)
    gateway = _GatewayThread(app, host, port, log_level)
    gateway.startup_failed.connect(window.set_gateway_error)
    gateway.finished.connect(
        lambda: window.set_gateway_error("Core Gateway가 종료되었습니다.")
    )
    gateway.start()
    window.show()
    exit_code = qt_app.exec() if owns_application else 0
    gateway.request_stop()
    if not gateway.wait(10_000):
        gateway.server.force_exit = True
        gateway.wait(5_000)
    controller.deleteLater()
    window.deleteLater()
    return int(exit_code)

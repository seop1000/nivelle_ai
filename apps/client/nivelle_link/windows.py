from typing import Any

from nivelle_protocol.identity import (
    AGENT_COMPONENT_NAME,
    ARCHIVE_COMPONENT_NAME,
    CALL_NAME,
    CORE_COMPONENT_NAME,
    DEFAULT_LORE,
    DEFAULT_PERSONA_DIRECTIVES,
    DEFAULT_RELATIONSHIP,
    DEFAULT_ROLE,
    DEFAULT_TONE,
    FULL_CHARACTER_NAME,
    KOREAN_CALL_NAME,
    KOREAN_FULL_NAME,
    LINK_COMPONENT_NAME,
    PERSONA_VERSION,
    PRODUCT_NAME,
    USER_NAME,
)
from nivelle_protocol.settings import ConnectionProfile
from nivelle_protocol.tools import TOOL_REGISTRY
from nivelle_protocol.version import APP_VERSION, PROTOCOL_VERSION
from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .storage import is_loopback_connection_host, validate_connection_host


class ServerConsoleWindow(QMainWindow):
    refresh_requested = Signal()
    save_requested = Signal(str, object)
    rollback_requested = Signal(int)
    pairing_code_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{CORE_COMPONENT_NAME} · 서버 관리")
        self.resize(980, 700)
        self._online = True
        self._loading = False
        self._save_buttons: list[QPushButton] = []
        self._mutable_controls: list[QWidget] = []
        self._settings: dict[str, Any] = {}
        self._server_supports_advertised_host = False

        root = QWidget()
        layout = QHBoxLayout(root)
        self.sections = QListWidget()
        self.sections.addItems(["개요", "서버", "모델", "추론", "변경 이력", "Agent"])
        self.sections.setMaximumWidth(180)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        toolbar = QHBoxLayout()
        title = QLabel("서버 상태 및 설정")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.refresh_button)
        right_layout.addLayout(toolbar)

        self.message = QLabel("서버에서 상태와 설정을 불러오려면 새로고침을 누르세요.")
        self.message.setWordWrap(True)
        right_layout.addWidget(self.message)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_overview_page())
        self.pages.addWidget(self._build_server_page())
        self.pages.addWidget(self._build_models_page())
        self.pages.addWidget(self._build_inference_page())
        self.pages.addWidget(self._build_revisions_page())
        self.pages.addWidget(self._build_agent_status_page())
        right_layout.addWidget(self.pages)

        layout.addWidget(self.sections, 1)
        layout.addWidget(right, 4)
        self.setCentralWidget(root)
        self.sections.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sections.setCurrentRow(0)

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.overview = QTextBrowser()
        self.overview.setPlainText("아직 서버 상태를 불러오지 않았습니다.")
        layout.addWidget(self.overview)
        return page

    def _build_server_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        notice = QLabel(
            "바인드 주소와 포트 변경은 서버를 다시 시작한 뒤 적용됩니다. "
            "광고 주소는 Link에 안내할 실제 서버 주소이며, 비워 두면 자동 감지합니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.server_host = QLineEdit()
        self.server_host.setPlaceholderText("예: 0.0.0.0 (모든 인터페이스에서 수신)")
        self.server_advertised_host = QLineEdit()
        self.server_advertised_host.setPlaceholderText("비워두면 활성 LAN IPv4 자동 감지")
        self.server_port = QSpinBox()
        self.server_port.setRange(1, 65535)
        self.server_log_level = QComboBox()
        self.server_log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.server_mock_mode = QCheckBox("내장 모의 응답 사용")
        self.server_mock_mode.setEnabled(False)
        self.server_mock_mode.setToolTip("Mock 전환은 모델 화면의 실행 모드에서 설정합니다.")
        self._mutable_controls.extend(
            [
                self.server_host,
                self.server_advertised_host,
                self.server_port,
                self.server_log_level,
            ]
        )
        form.addRow("수신 바인드 주소", self.server_host)
        form.addRow("광고 주소 (비움 = 자동)", self.server_advertised_host)
        form.addRow("포트", self.server_port)
        form.addRow("로그 수준", self.server_log_level)
        form.addRow("Mock 모드", self.server_mock_mode)
        layout.addLayout(form)
        self.server_effective_network = QLabel(
            "실제 네트워크 주소는 서버 상태를 새로고침하면 표시됩니다."
        )
        self.server_effective_network.setWordWrap(True)
        self.server_effective_network.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.server_effective_network)
        pairing_group = QGroupBox("새 Link 등록")
        pairing_layout = QVBoxLayout(pairing_group)
        pairing_notice = QLabel(
            "다른 PC의 Link를 추가하려면 일회성 코드를 생성한 뒤 그 PC에 입력하세요."
        )
        pairing_notice.setWordWrap(True)
        self.pairing_code = QLabel("아직 생성된 코드가 없습니다.")
        self.pairing_code.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.pairing_code_button = QPushButton("새 페어링 코드 생성")
        self.pairing_code_button.clicked.connect(self.pairing_code_requested.emit)
        pairing_layout.addWidget(pairing_notice)
        pairing_layout.addWidget(self.pairing_code)
        pairing_layout.addWidget(self.pairing_code_button)
        layout.addWidget(pairing_group)
        self._mutable_controls.append(self.pairing_code_button)
        layout.addStretch()
        layout.addWidget(self._save_button("server"))
        return page

    def _build_models_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        notice = QLabel(
            "모델 실행 파일이나 모델 파일 경로를 바꾸면 llama-server를 다시 시작해야 합니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.models_mode = QComboBox()
        self.models_mode.addItems(["mock", "managed", "external"])
        self.llama_server_path = QLineEdit()
        self.llama_server_path.setPlaceholderText("예: runtime\\llama\\llama-server.exe")
        self.provider_endpoint = QLineEdit()
        self.provider_endpoint.setPlaceholderText("예: http://127.0.0.1:8080")
        self.fallback_enabled = QCheckBox("대체 모델 사용 허용")
        self.fallback_enabled.setToolTip(
            "부분 응답이 시작되기 전의 연결·timeout·provider 오류에만 fallback을 사용합니다."
        )
        form.addRow("실행 모드", self.models_mode)
        form.addRow("llama-server 경로", self.llama_server_path)
        form.addRow("모델 Provider URL", self.provider_endpoint)
        form.addRow("Fallback", self.fallback_enabled)
        layout.addLayout(form)

        layout.addWidget(QLabel("등록 모델"))
        self.models_table = QTableWidget(0, 6)
        self.models_table.setHorizontalHeaderLabels(
            ["ID", "이름", "파일 경로", "Endpoint", "역할", "사용"]
        )
        self.models_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.models_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.models_table)
        row_actions = QHBoxLayout()
        self.add_model_button = QPushButton("모델 추가")
        self.remove_model_button = QPushButton("선택 삭제")
        self.add_model_button.clicked.connect(self._add_model_row)
        self.remove_model_button.clicked.connect(self._remove_selected_model)
        row_actions.addWidget(self.add_model_button)
        row_actions.addWidget(self.remove_model_button)
        row_actions.addStretch()
        layout.addLayout(row_actions)
        layout.addWidget(self._save_button("models"))
        self._mutable_controls.extend(
            [
                self.models_mode,
                self.llama_server_path,
                self.provider_endpoint,
                self.fallback_enabled,
                self.models_table,
                self.add_model_button,
                self.remove_model_button,
            ]
        )
        return page

    def _build_inference_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        notice = QLabel(
            "온도와 샘플링 값은 다음 요청부터 적용됩니다. 컨텍스트, GPU 레이어, 스레드, "
            "배치 크기는 llama-server 재시작이 필요할 수 있습니다."
        )
        notice.setWordWrap(True)
        page_layout.addWidget(notice)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.inference_inputs: dict[str, QSpinBox | QDoubleSpinBox | QCheckBox] = {}

        integer_fields = {
            "context_size": (512, 131072, "컨텍스트 크기"),
            "gpu_layers": (0, 2147483647, "GPU 레이어"),
            "threads": (1, 2147483647, "CPU 스레드"),
            "batch_size": (1, 2147483647, "배치 크기"),
            "micro_batch_size": (1, 2147483647, "마이크로 배치"),
            "top_k": (0, 2147483647, "Top K"),
            "max_output_tokens": (1, 2147483647, "최대 출력 토큰"),
            "seed": (-1, 2147483647, "시드 (-1: 무작위)"),
            "concurrent_requests": (1, 2147483647, "동시 요청 수"),
        }
        for key, (minimum, maximum, label) in integer_fields.items():
            integer_widget = QSpinBox()
            integer_widget.setRange(minimum, maximum)
            self.inference_inputs[key] = integer_widget
            form.addRow(label, integer_widget)

        double_fields = {
            "temperature": (0.0, 2.0, 3, "Temperature"),
            "top_p": (0.001, 1.0, 3, "Top P"),
            "repeat_penalty": (0.0, 1000000000.0, 3, "반복 패널티"),
            "request_timeout": (0.1, 1000000000.0, 1, "요청 제한 시간(초)"),
        }
        for key, (
            double_minimum,
            double_maximum,
            decimals,
            label,
        ) in double_fields.items():
            double_widget = QDoubleSpinBox()
            double_widget.setRange(double_minimum, double_maximum)
            double_widget.setDecimals(decimals)
            double_widget.setSingleStep(0.1)
            self.inference_inputs[key] = double_widget
            form.addRow(label, double_widget)

        streaming = QCheckBox("스트리밍 응답 사용")
        self.inference_inputs["streaming"] = streaming
        form.addRow("스트리밍", streaming)
        scroll.setWidget(content)
        page_layout.addWidget(scroll)
        page_layout.addWidget(self._save_button("inference"))
        self._mutable_controls.extend(self.inference_inputs.values())
        return page

    def _build_revisions_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        notice = QLabel(
            "되돌리기는 선택한 변경 직전 설정을 복원하며, 복원 작업도 새 변경 이력으로 기록됩니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.revisions_table = QTableWidget(0, 5)
        self.revisions_table.setHorizontalHeaderLabels(
            ["번호", "설정", "변경 시각", "클라이언트", "상태"]
        )
        self.revisions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.revisions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.revisions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.revisions_table)
        self.rollback_button = QPushButton("선택한 변경 되돌리기")
        self.rollback_button.clicked.connect(self._request_rollback)
        layout.addWidget(self.rollback_button)
        return page

    def _build_agent_status_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        notice = QLabel(
            "Core는 연결된 Link의 기능과 호출 상태만 표시합니다. "
            "로컬 허용 목록과 승인 정책은 Link에서만 변경할 수 있습니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.agent_status = QTextBrowser()
        self.agent_status.setPlainText("Agent 상태를 아직 불러오지 않았습니다.")
        layout.addWidget(self.agent_status)
        return page

    def _save_button(self, section: str) -> QPushButton:
        button = QPushButton("설정 검증 후 저장")
        button.clicked.connect(lambda _checked=False, name=section: self._request_save(name))
        self._save_buttons.append(button)
        return button

    def _request_save(self, section: str) -> None:
        self.save_requested.emit(section, self.settings_payload(section))

    def settings_payload(self, section: str) -> dict[str, Any]:
        if section == "server":
            payload: dict[str, Any] = {
                "host": self.server_host.text().strip(),
                "port": self.server_port.value(),
                "log_level": self.server_log_level.currentText(),
                "mock_mode": self.server_mock_mode.isChecked(),
            }
            advertised_host = self.server_advertised_host.text().strip()
            if self._server_supports_advertised_host or advertised_host:
                payload["advertised_host"] = advertised_host or None
            return payload
        if section == "models":
            models: list[dict[str, Any]] = []
            for row in range(self.models_table.rowCount()):
                role = self.models_table.cellWidget(row, 4)
                enabled = self.models_table.item(row, 5)
                path = self._table_text(self.models_table, row, 2)
                endpoint = self._table_text(self.models_table, row, 3)
                model = {
                    "id": self._table_text(self.models_table, row, 0),
                    "name": self._table_text(self.models_table, row, 1),
                    "path": path or None,
                    "role": role.currentText() if isinstance(role, QComboBox) else "primary",
                    "enabled": bool(enabled and enabled.checkState() == Qt.CheckState.Checked),
                }
                if endpoint:
                    model["endpoint"] = endpoint
                models.append(model)
            return {
                "mode": self.models_mode.currentText(),
                "llama_server_path": self.llama_server_path.text().strip() or None,
                "provider_endpoint": self.provider_endpoint.text().strip(),
                "fallback_enabled": self.fallback_enabled.isChecked(),
                "models": models,
            }
        if section == "inference":
            result: dict[str, Any] = {}
            for key, widget in self.inference_inputs.items():
                result[key] = (
                    widget.isChecked() if isinstance(widget, QCheckBox) else widget.value()
                )
            return result
        raise KeyError(section)

    @staticmethod
    def _table_text(table: QTableWidget, row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text().strip() if item else ""

    def _add_model_row(self, model: dict[str, Any] | None = None) -> None:
        value = model or {
            "id": f"model-{self.models_table.rowCount() + 1}",
            "name": "새 모델",
            "path": None,
            "endpoint": None,
            "role": "primary",
            "enabled": True,
        }
        row = self.models_table.rowCount()
        self.models_table.insertRow(row)
        for column, key in enumerate(("id", "name", "path", "endpoint")):
            self.models_table.setItem(
                row,
                column,
                QTableWidgetItem("" if value.get(key) is None else str(value.get(key))),
            )
        role = QComboBox()
        role.addItems(["primary", "fallback"])
        role.setCurrentText(str(value.get("role", "primary")))
        self.models_table.setCellWidget(row, 4, role)
        enabled = QTableWidgetItem()
        enabled.setFlags(enabled.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        enabled.setCheckState(
            Qt.CheckState.Checked if value.get("enabled", True) else Qt.CheckState.Unchecked
        )
        self.models_table.setItem(row, 5, enabled)

    def _remove_selected_model(self) -> None:
        rows = sorted({index.row() for index in self.models_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.models_table.removeRow(row)

    def _request_rollback(self) -> None:
        row = self.revisions_table.currentRow()
        item = self.revisions_table.item(row, 0) if row >= 0 else None
        if item is None:
            QMessageBox.information(self, "변경 이력", "되돌릴 변경 이력을 선택하세요.")
            return
        revision_id = int(item.text())
        answer = QMessageBox.question(
            self,
            "설정 되돌리기",
            f"변경 #{revision_id} 직전 설정으로 되돌릴까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.rollback_requested.emit(revision_id)

    def set_status(self, value: dict[str, Any]) -> None:
        metrics_value = value.get("metrics")
        metrics: dict[str, Any] = metrics_value if isinstance(metrics_value, dict) else {}
        components_value = value.get("components")
        components: dict[str, Any] = (
            components_value if isinstance(components_value, dict) else {}
        )
        backend_value = value.get("llama_server")
        if not isinstance(backend_value, dict):
            backend_value = components.get("llm")
        backend: dict[str, Any] = backend_value if isinstance(backend_value, dict) else {}
        memory_value = value.get("memory_database")
        if not isinstance(memory_value, dict):
            memory_value = components.get("memory_database")
        memory: dict[str, Any] = memory_value if isinstance(memory_value, dict) else {}
        embedding_value = value.get("embedding_model")
        if not isinstance(embedding_value, dict):
            embedding_value = components.get("embedding")
        embedding: dict[str, Any] = (
            embedding_value if isinstance(embedding_value, dict) else {}
        )
        runtime_value = value.get("runtime")
        runtime: dict[str, Any] = runtime_value if isinstance(runtime_value, dict) else {}
        request_metrics_value = value.get("last_request_metrics")
        request_metrics: dict[str, Any] = (
            request_metrics_value if isinstance(request_metrics_value, dict) else {}
        )
        network_value = value.get("network")
        network: dict[str, Any] = network_value if isinstance(network_value, dict) else {}

        if network:
            bind_host = network.get("bind_host") or network.get("bind_address")
            bind_port = network.get("bind_port") or network.get("port")
            advertised_host = (
                network.get("advertised_host")
                or network.get("advertised_address")
                or network.get("effective_advertised_host")
            )
            advertised_endpoint = network.get("advertised_endpoint")
            source = network.get("advertised_source") or network.get("source")
            bind_display = str(bind_host or "알 수 없음")
            if bind_port not in (None, ""):
                bind_display = f"{bind_display}:{bind_port}"
            advertised_display = str(
                advertised_endpoint or advertised_host or "감지 실패/설정 없음"
            )
            source_display = f" · 출처: {source}" if source not in (None, "") else ""
            self.server_effective_network.setText(
                f"현재 바인드: {bind_display}\n"
                f"Link 안내 주소: {advertised_display}{source_display}"
            )
        else:
            self.server_effective_network.setText(
                "이 서버 버전은 실제 바인드/광고 주소 상태를 제공하지 않습니다."
            )

        def observed_metric(*names: str) -> Any | None:
            return next(
                (request_metrics[name] for name in names if request_metrics.get(name) is not None),
                None,
            )

        def display(value: Any, *, unavailable: str = "unsupported") -> str:
            return unavailable if value in (None, "") else str(value)

        def metric_text(*names: str, unit: str | None = None, decimals: int = 1) -> str:
            observed = observed_metric(*names)
            if observed is None:
                return "unsupported"
            if isinstance(observed, (int, float)) and not isinstance(observed, bool) and unit:
                return f"{observed:.{decimals}f} {unit}"
            return str(observed)

        loaded_model = value.get("model_name") or backend.get("loaded_model")
        configured_model = value.get("configured_model_name") or backend.get(
            "configured_model"
        )

        uptime = float(value.get("uptime_seconds", 0) or 0)
        lines = [
            f"서버 앱 버전: {value.get('app_version') or value.get('version', '알 수 없음')}",
            f"서버 ID: {value.get('server_id') or '구버전 서버 · 미지원'}",
            f"클라이언트 앱 버전: {APP_VERSION}",
            f"서버 프로토콜: {value.get('protocol_version') or runtime.get('protocol_version', '알 수 없음')}",
            f"클라이언트 프로토콜: {PROTOCOL_VERSION}",
            f"빌드 커밋: {runtime.get('build_commit') or 'unavailable'}",
            f"빌드 시각: {runtime.get('build_time') or 'unavailable'}",
            f"실행 파일: {runtime.get('executable_path') or 'unavailable'}",
            f"게이트웨이: {value.get('gateway', '알 수 없음')}",
            "바인드 주소: "
            + str(
                network.get("bind_host")
                or network.get("bind_address")
                or "unavailable"
            ),
            "Link 안내 주소: "
            + str(
                network.get("advertised_endpoint")
                or network.get("advertised_host")
                or network.get("advertised_address")
                or "unavailable"
            ),
            f"AI 상태: {value.get('assistant_state', '알 수 없음')}",
            f"실제 로드 모델: {display(loaded_model)}",
            f"설정 모델: {display(configured_model)}",
            f"모델 역할: {value.get('model_role', '알 수 없음')}",
            f"추론 엔진: {display(backend.get('engine'))}",
            f"양자화: {display(backend.get('quantization'))}",
            f"가동 시간: {int(uptime // 3600)}시간 {int(uptime % 3600 // 60)}분",
            f"페어링 필요: {'예' if value.get('pairing_required') else '아니요'}",
            "",
            "시스템 사용량",
            f"CPU: {metrics.get('cpu_percent', '알 수 없음')}%",
            f"RAM: {metrics.get('system_ram_percent', '알 수 없음')}%",
            f"디스크: {metrics.get('disk_percent', '알 수 없음')}%",
            f"게이트웨이 메모리: {self._format_bytes(metrics.get('gateway_memory_bytes'))}",
            f"GPU: {metrics.get('gpu') or metrics.get('gpu_reason', '알 수 없음')}",
            "",
            "기억 계층",
            f"기억 DB: {memory.get('state', '알 수 없음')}",
            f"저장 백엔드: {memory.get('backend', '알 수 없음')}",
            f"검색 백엔드: {memory.get('search_backend', '알 수 없음')}",
            f"활성 기억: {memory.get('active_count', '알 수 없음')}",
            f"비활성 기억: {memory.get('inactive_count', '알 수 없음')}",
            f"임베딩: {embedding.get('state', 'unavailable')}",
            f"임베딩 공급자: {embedding.get('provider') or 'unavailable'}",
        ]
        if backend:
            lines.extend(
                [
                    "",
                    "llama-server",
                    f"상태: {backend.get('state', '알 수 없음')}",
                    f"사용 가능: {'예' if backend.get('available') else '아니요'}",
                    f"주소: {backend.get('url') or '내장 Mock'}",
                ]
            )
            if backend.get("error"):
                lines.append(f"마지막 오류: {backend['error']}")
        lines.append(f"진행 중 생성: {int(value.get('active_generations', 0) or 0)}")
        if value.get("last_error"):
            lines.append(f"최근 추론 오류: {value['last_error']}")
        lines.extend(
            [
                "",
                "최근 요청 메트릭",
                f"요청 ID: {metric_text('request_id')}",
                f"응답 모델: {metric_text('model')}",
                f"입력 토큰: {metric_text('prompt_tokens', 'input_tokens')}",
                f"출력 토큰: {metric_text('completion_tokens', 'output_tokens')}",
                f"전체 토큰: {metric_text('total_tokens')}",
                f"첫 토큰 지연: {metric_text('first_token_latency_ms', unit='ms')}",
                f"전체 지연: {metric_text('total_latency_ms', unit='ms')}",
                f"생성 속도: {metric_text('tokens_per_second', unit='token/s', decimals=2)}",
                f"종료 사유: {metric_text('finish_reason')}",
                "중단됨: "
                + (
                    "예"
                    if observed_metric("interrupted") is True
                    else "아니요"
                    if observed_metric("interrupted") is False
                    else "unsupported"
                ),
            ]
        )
        self.overview.setPlainText("\n".join(lines))
        self.set_agent_status(value.get("agent"))

    def show_pairing_code(self, code: str, expires_at: str | None) -> None:
        expiry = f" · 만료: {expires_at}" if expires_at else ""
        self.pairing_code.setText(f"페어링 코드: {code}{expiry}")

    def set_agent_status(self, value: object) -> None:
        agent: dict[str, Any] = value if isinstance(value, dict) else {}
        registry_value = agent.get("registry")
        registry: dict[str, Any] = (
            registry_value if isinstance(registry_value, dict) else {}
        )
        statistics_value = agent.get("statistics")
        statistics: dict[str, Any] = (
            statistics_value if isinstance(statistics_value, dict) else {}
        )
        clients_value = agent.get("clients")
        clients: list[Any] = clients_value if isinstance(clients_value, list) else []
        failures_value = agent.get("recent_failures")
        failures: list[Any] = failures_value if isinstance(failures_value, list) else []
        lines = [
            f"도구 오케스트레이션: {'사용' if agent.get('enabled') else '사용 안 함'}",
            f"레지스트리 버전: {registry.get('version') or 'unavailable'}",
            f"등록 도구: {registry.get('tool_count', 0)}개",
            f"연결된 Link: {len(clients)}개",
            f"승인 대기: {statistics.get('pending', 0)}",
            f"실행 중: {statistics.get('running', 0)}",
            f"완료: {statistics.get('completed', 0)}",
            f"실패: {statistics.get('failed', 0)}",
            f"선택 대상: {agent.get('selected_target_client') or '없음'}",
            "",
            "연결 클라이언트",
        ]
        for client in clients:
            if not isinstance(client, dict):
                continue
            lines.append(
                " · ".join(
                    (
                        str(client.get("client_id") or "unknown"),
                        str(client.get("session_id") or "unknown"),
                        f"도구 {client.get('tool_count', 0)}개",
                        str(client.get("protocol_status") or "unknown"),
                    )
                )
            )
        if failures:
            lines.extend(("", "최근 실패"))
            for failure in failures[:10]:
                if isinstance(failure, dict):
                    lines.append(
                        f"{failure.get('tool_name') or '-'} · "
                        f"{failure.get('error_code') or 'execution_failed'}"
                    )
        self.agent_status.setPlainText("\n".join(lines))

    def set_settings(self, value: dict[str, Any]) -> None:
        self._settings = value
        server = value.get("server", {})
        self._server_supports_advertised_host = "advertised_host" in server
        self.server_host.setText(str(server.get("host", "0.0.0.0")))
        self.server_advertised_host.setText(str(server.get("advertised_host") or ""))
        self.server_port.setValue(int(server.get("port", 8765)))
        log_level = str(server.get("log_level", "INFO")).upper()
        if self.server_log_level.findText(log_level) < 0:
            self.server_log_level.addItem(log_level)
        self.server_log_level.setCurrentText(log_level)
        self.server_mock_mode.setChecked(bool(server.get("mock_mode", False)))

        models = value.get("models", {})
        self.models_mode.setCurrentText(str(models.get("mode", "mock")))
        self.llama_server_path.setText(str(models.get("llama_server_path") or ""))
        self.provider_endpoint.setText(
            str(models.get("provider_endpoint", models.get("external_url", "")))
        )
        self.fallback_enabled.setChecked(bool(models.get("fallback_enabled", True)))
        self.models_table.setRowCount(0)
        for model in models.get("models", []):
            if isinstance(model, dict):
                self._add_model_row(model)

        inference = value.get("inference", {})
        for key, widget in self.inference_inputs.items():
            if key not in inference:
                continue
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(inference[key]))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(inference[key]))
            else:
                widget.setValue(float(inference[key]))

    def set_revisions(self, values: list[dict[str, Any]]) -> None:
        self.revisions_table.setRowCount(0)
        for value in values:
            row = self.revisions_table.rowCount()
            self.revisions_table.insertRow(row)
            columns = (
                value.get("id", ""),
                value.get("section", ""),
                value.get("created_at", ""),
                value.get("client_id", ""),
                value.get("apply_status", ""),
            )
            for column, item in enumerate(columns):
                self.revisions_table.setItem(
                    row, column, QTableWidgetItem("" if item is None else str(item))
                )

    def show_loading(self, loading: bool, text: str = "서버에서 불러오는 중…") -> None:
        self._loading = loading
        self._update_control_state()
        if loading:
            self.message.setStyleSheet("color: #555;")
            self.message.setText(text)

    def set_online(self, online: bool) -> None:
        """Keep cached server data readable while preventing offline mutations."""

        was_online = self._online
        self._online = online
        self._update_control_state()
        if not online:
            self.message.setStyleSheet("color: #8a5a00;")
            self.message.setText("서버 오프라인 · 마지막으로 불러온 정보를 읽기 전용으로 표시합니다.")
        elif not was_online:
            self.show_message("서버가 다시 연결되었습니다. 새로고침하여 최신 상태를 확인하세요.")

    def _update_control_state(self) -> None:
        self.refresh_button.setEnabled(not self._loading)
        can_modify = self._online and not self._loading
        self.rollback_button.setEnabled(can_modify)
        for button in self._save_buttons:
            button.setEnabled(can_modify)
        for control in self._mutable_controls:
            control.setEnabled(can_modify)

    def show_message(self, message: str) -> None:
        self.message.setStyleSheet("color: #176b2c;")
        self.message.setText(message)

    def show_error(self, message: str) -> None:
        self.message.setStyleSheet("color: #b00020;")
        self.message.setText(message)

    @staticmethod
    def _format_bytes(value: Any) -> str:
        if not isinstance(value, int | float):
            return "알 수 없음"
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return "알 수 없음"


class MemoryArchiveWindow(QMainWindow):
    refresh_requested = Signal()
    search_requested = Signal(str)
    create_requested = Signal(object)
    update_requested = Signal(str, object)
    delete_requested = Signal(str)

    _CATEGORY_LABELS = {
        "preference": "선호",
        "project": "프로젝트",
        "workflow": "작업 방식",
        "instruction": "지침",
        "other": "기타",
    }

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{ARCHIVE_COMPONENT_NAME} · 장기 기억")
        self.resize(940, 680)
        self._online = True
        self._loading = False
        self._selected_id: str | None = None

        root = QWidget()
        root_layout = QVBoxLayout(root)

        title_row = QHBoxLayout()
        title = QLabel("장기 기억 · Phase 2")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self.refresh_button)
        root_layout.addLayout(title_row)

        notice = QLabel(
            "여기에 직접 저장한 기억만 대화에 사용됩니다. 비밀번호, 전화번호, 주소 같은 "
            "민감한 식별 정보와 대화 전문은 저장할 수 없습니다."
        )
        notice.setWordWrap(True)
        root_layout.addWidget(notice)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("기억 내용 검색")
        self.include_inactive_search = QCheckBox("비활성 포함")
        self.include_inactive_search.setToolTip(
            "일반 대화에는 사용되지 않는 비활성 기억도 검색 결과에 포함합니다."
        )
        self.search_button = QPushButton("검색")
        self.clear_search_button = QPushButton("전체 보기")
        self.search_input.returnPressed.connect(self._request_search)
        self.search_button.clicked.connect(self._request_search)
        self.clear_search_button.clicked.connect(self._clear_search)
        search_row.addWidget(self.search_input)
        search_row.addWidget(self.include_inactive_search)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.clear_search_button)
        root_layout.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.memories_table = QTableWidget(0, 5)
        self.memories_table.setHorizontalHeaderLabels(
            ["내용", "분류", "우선순위", "상태", "수정 시각"]
        )
        self.memories_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.memories_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.memories_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.memories_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.memories_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.memories_table.itemSelectionChanged.connect(self._load_selected_memory)
        splitter.addWidget(self.memories_table)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_title = QLabel("기억 추가 또는 편집")
        editor_title.setStyleSheet("font-weight: 600;")
        editor_layout.addWidget(editor_title)
        form = QFormLayout()
        self.memory_content = QLineEdit()
        self.memory_content.setMaxLength(500)
        self.memory_content.setPlaceholderText("한 줄로 기억할 내용을 입력하세요")
        self.memory_category = QComboBox()
        for category, label in self._CATEGORY_LABELS.items():
            self.memory_category.addItem(label, category)
        self.memory_priority = QSpinBox()
        self.memory_priority.setRange(0, 100)
        self.memory_priority.setValue(50)
        self.memory_active = QCheckBox("대화 프롬프트에 사용")
        self.memory_active.setChecked(True)
        form.addRow("내용", self.memory_content)
        form.addRow("분류", self.memory_category)
        form.addRow("우선순위", self.memory_priority)
        form.addRow("활성", self.memory_active)
        editor_layout.addLayout(form)

        actions = QHBoxLayout()
        self.new_button = QPushButton("새 기억")
        self.create_button = QPushButton("새로 저장")
        self.update_button = QPushButton("선택 기억 저장")
        self.delete_button = QPushButton("선택 기억 삭제")
        self.new_button.clicked.connect(self.clear_editor)
        self.create_button.clicked.connect(self._request_create)
        self.update_button.clicked.connect(self._request_update)
        self.delete_button.clicked.connect(self._request_delete)
        actions.addWidget(self.new_button)
        actions.addWidget(self.create_button)
        actions.addWidget(self.update_button)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        editor_layout.addLayout(actions)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root_layout.addWidget(splitter)

        self.message = QLabel("서버에서 저장된 기억을 불러오려면 새로고침을 누르세요.")
        self.message.setWordWrap(True)
        root_layout.addWidget(self.message)
        self.setCentralWidget(root)
        self._update_editor_buttons()

    def current_query(self) -> str:
        return self.search_input.text().strip()

    def include_inactive(self) -> bool:
        return self.include_inactive_search.isChecked()

    def memory_payload(self) -> dict[str, Any]:
        category = self.memory_category.currentData()
        return {
            "content": self.memory_content.text().strip(),
            "category": str(category or "other"),
            "active": self.memory_active.isChecked(),
            "priority": self.memory_priority.value(),
        }

    def set_memories(self, values: list[dict[str, Any]]) -> None:
        selected_id = self._selected_id
        self.memories_table.setRowCount(0)
        selected_row = -1
        for value in values:
            row = self.memories_table.rowCount()
            self.memories_table.insertRow(row)
            category = str(value.get("category", "other"))
            updated_at = str(value.get("updated_at", "")).replace("T", " ")
            columns = (
                value.get("content", ""),
                self._CATEGORY_LABELS.get(category, category),
                value.get("priority", 50),
                "사용" if value.get("active", True) else "꺼짐",
                updated_at,
            )
            for column, item_value in enumerate(columns):
                item = QTableWidgetItem(str(item_value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, dict(value))
                self.memories_table.setItem(row, column, item)
            if str(value.get("id", "")) == selected_id:
                selected_row = row

        if self.memories_table.rowCount() == 0:
            self.clear_editor()
        else:
            self.memories_table.selectRow(selected_row if selected_row >= 0 else 0)

    def clear_editor(self) -> None:
        self._selected_id = None
        self.memories_table.clearSelection()
        self.memory_content.clear()
        self.memory_category.setCurrentIndex(self.memory_category.findData("other"))
        self.memory_priority.setValue(50)
        self.memory_active.setChecked(True)
        self.memory_content.setFocus()
        self._update_editor_buttons()

    def _load_selected_memory(self) -> None:
        row = self.memories_table.currentRow()
        item = self.memories_table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(value, dict):
            self._selected_id = None
            self._update_editor_buttons()
            return
        self._selected_id = str(value.get("id", "")) or None
        self.memory_content.setText(str(value.get("content", "")))
        category_index = self.memory_category.findData(str(value.get("category", "other")))
        self.memory_category.setCurrentIndex(category_index if category_index >= 0 else 0)
        self.memory_priority.setValue(int(value.get("priority", 50)))
        self.memory_active.setChecked(bool(value.get("active", True)))
        self._update_editor_buttons()

    def _update_editor_buttons(self) -> None:
        has_selection = self._selected_id is not None
        can_modify = self._online and not self._loading
        self.new_button.setEnabled(can_modify)
        self.create_button.setEnabled(can_modify)
        self.update_button.setEnabled(can_modify and has_selection)
        self.delete_button.setEnabled(can_modify and has_selection)
        for control in (
            self.memory_content,
            self.memory_category,
            self.memory_priority,
            self.memory_active,
        ):
            control.setEnabled(can_modify)

    def _valid_payload(self) -> dict[str, Any] | None:
        value = self.memory_payload()
        if value["content"]:
            return value
        QMessageBox.information(self, "장기 기억", "저장할 기억 내용을 입력하세요.")
        self.memory_content.setFocus()
        return None

    def _request_search(self) -> None:
        self.search_requested.emit(self.current_query())

    def _clear_search(self) -> None:
        self.search_input.clear()
        self.search_requested.emit("")

    def _request_create(self) -> None:
        value = self._valid_payload()
        if value is not None:
            self.create_requested.emit(value)

    def _request_update(self) -> None:
        if self._selected_id is None:
            QMessageBox.information(self, "장기 기억", "편집할 기억을 선택하세요.")
            return
        value = self._valid_payload()
        if value is not None:
            self.update_requested.emit(self._selected_id, value)

    def _request_delete(self) -> None:
        if self._selected_id is None:
            QMessageBox.information(self, "장기 기억", "삭제할 기억을 선택하세요.")
            return
        content = self.memory_content.text().strip()
        preview = content if len(content) <= 80 else f"{content[:77]}…"
        answer = QMessageBox.question(
            self,
            "장기 기억 삭제",
            f"다음 기억을 영구 삭제할까요?\n\n{preview}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(self._selected_id)

    def show_loading(self, loading: bool, text: str = "장기 기억을 불러오는 중…") -> None:
        self._loading = loading
        for button in (self.refresh_button, self.search_button, self.clear_search_button):
            button.setDisabled(loading)
        self.search_input.setDisabled(loading)
        self.include_inactive_search.setDisabled(loading)
        self._update_editor_buttons()
        if loading:
            self.message.setStyleSheet("color: #555;")
            self.message.setText(text)

    def set_online(self, online: bool) -> None:
        """Keep loaded memories readable while preventing offline changes."""

        was_online = self._online
        self._online = online
        self._update_editor_buttons()
        if not online:
            self.message.setStyleSheet("color: #8a5a00;")
            self.message.setText("서버 오프라인 · 마지막 기억 목록을 읽기 전용으로 표시합니다.")
        elif not was_online:
            self.show_message("서버가 다시 연결되었습니다. 새로고침하여 기억 목록을 확인하세요.")

    def show_message(self, message: str) -> None:
        self.message.setStyleSheet("color: #176b2c;")
        self.message.setText(message)

    def show_error(self, message: str) -> None:
        self.message.setStyleSheet("color: #b00020;")
        self.message.setText(message)


class PairingDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{LINK_COMPONENT_NAME} · {CORE_COMPONENT_NAME} 페어링")
        layout = QFormLayout(self)
        self.code = QLineEdit()
        self.code.setMaxLength(6)
        self.name = QLineEdit("내 PC")
        layout.addRow("6자리 코드", self.code)
        layout.addRow("기기 이름", self.name)
        button = QPushButton("페어링")
        button.clicked.connect(self.accept)
        layout.addRow(button)


class ConnectionDialog(QDialog):
    def __init__(
        self,
        profile: ConnectionProfile | None = None,
        error_message: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._source_profile = profile
        self.setWindowTitle(f"{LINK_COMPONENT_NAME} · {CORE_COMPONENT_NAME} 연결")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QFormLayout(self)
        description = QLabel(
            f"연결할 {CORE_COMPONENT_NAME} 서버 정보를 입력하세요.\n"
            "서버와 다른 PC라면 서버 PC의 IP 주소를 사용하세요."
        )
        description.setWordWrap(True)
        layout.addRow(description)

        self.host = QLineEdit(profile.host if profile else "")
        self.host.setPlaceholderText("예: 192.168.0.10")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(profile.port if profile else 8765)
        self.tls = QCheckBox("HTTPS/WSS 보안 연결 사용")
        self.tls.setChecked(profile.tls if profile else False)
        self.new_server = QCheckBox("기존 서버 연결을 해제하고 새 서버로 변경")
        self.new_server.setVisible(bool(profile and profile.server_id))
        self.new_server.setToolTip(
            "주소만 바뀐 같은 서버라면 선택하지 마세요. 다른 Core로 바꿀 때만 선택합니다."
        )
        layout.addRow("서버 주소", self.host)
        layout.addRow("포트", self.port)
        layout.addRow("TLS", self.tls)
        layout.addRow("서버 식별", self.new_server)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #a05a00;")
        self.warning.hide()
        layout.addRow(self.warning)
        self.host.textChanged.connect(self._update_host_warning)
        self._update_host_warning(self.host.text())

        self.error = QLabel(error_message or "")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b00020;")
        self.error.setVisible(bool(error_message))
        layout.addRow(self.error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장 후 연결")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def accept(self) -> None:
        host = self.host.text().strip()
        if not host:
            self._show_validation_error("서버 주소를 입력하세요.")
            return
        try:
            normalized_host = validate_connection_host(host)
        except ValueError as exc:
            messages = {
                "server host must not include a protocol or path": (
                    "서버 주소에는 http:// 같은 프로토콜이나 경로를 넣지 마세요."
                ),
                "a wildcard address cannot be used as a server destination": (
                    "0.0.0.0 같은 바인드 전용 주소로는 서버에 연결할 수 없습니다. "
                    "서버 PC의 실제 LAN IPv4 주소를 입력하세요."
                ),
                "an APIPA/link-local address cannot be used as a server destination": (
                    "169.254.x.x 같은 APIPA 주소는 서버 연결 주소로 사용할 수 없습니다."
                ),
                "a multicast address cannot be used as a server destination": (
                    "멀티캐스트 주소는 서버 연결 주소로 사용할 수 없습니다."
                ),
                "a broadcast address cannot be used as a server destination": (
                    "브로드캐스트 주소는 서버 연결 주소로 사용할 수 없습니다."
                ),
            }
            self._show_validation_error(messages.get(str(exc), "서버 주소가 올바르지 않습니다."))
            return
        self.host.setText(normalized_host)
        super().accept()

    def _update_host_warning(self, value: str) -> None:
        if is_loopback_connection_host(value):
            self.warning.setText(
                "127.0.0.1/localhost는 Core와 Link가 같은 PC에서 실행될 때만 연결됩니다."
            )
            self.warning.show()
        else:
            self.warning.clear()
            self.warning.hide()

    def connection_profile(self) -> ConnectionProfile:
        source = self._source_profile
        return ConnectionProfile(
            id=source.id if source else "primary",
            type=source.type if source else "local",
            host=self.host.text().strip().strip("[]"),
            port=self.port.value(),
            tls=self.tls.isChecked(),
            priority=source.priority if source else 1,
            enabled=True,
            server_id=(
                source.server_id
                if source is not None and not self.new_server.isChecked()
                else None
            ),
        )

    def _show_validation_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()
        self.host.setFocus()


class ConversationInfoWindow(QMainWindow):
    """Read-only connection and per-response context details."""

    _CONNECTION_FIELDS = (
        ("profile", "연결 프로필"),
        ("address", "서버 주소"),
        ("tls", "TLS"),
        ("gateway", "Gateway"),
        ("llm", "LLM"),
        ("memory_database", "기억 DB"),
        ("embedding_model", "임베딩 모델"),
        ("client_version", "클라이언트 버전"),
        ("server_version", "서버 버전"),
        ("protocol_version", "프로토콜"),
        ("compatibility", "호환성"),
        ("build_commit", "서버 빌드"),
        ("uptime", "서버 가동 시간"),
        ("last_checked", "마지막 확인"),
        ("rtt_ms", "왕복 지연시간"),
        ("consecutive_failures", "연속 확인 실패"),
        ("reconnect_attempts", "재연결 시도"),
    )
    _CONNECTION_ALIASES = {
        "profile": ("profile", "profile_name"),
        "address": ("address", "server_address"),
        "tls": ("tls", "tls_state"),
        "gateway": ("gateway", "gateway_state"),
        "llm": ("llm", "llm_state"),
        "memory_database": ("memory_database", "memory_database_state"),
        "embedding_model": ("embedding_model", "embedding_model_state"),
        "client_version": ("client_version",),
        "server_version": ("server_version",),
        "protocol_version": ("protocol_version",),
        "compatibility": ("compatibility", "compatibility_warning"),
        "build_commit": ("build_commit",),
        "uptime": ("uptime", "uptime_seconds"),
        "last_checked": ("last_checked", "last_checked_at"),
        "rtt_ms": ("rtt_ms", "latency_ms"),
        "consecutive_failures": ("consecutive_failures",),
        "reconnect_attempts": ("reconnect_attempts", "retry_count"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{LINK_COMPONENT_NAME} · 대화 정보")
        self.resize(820, 560)
        self._used_memories: list[dict[str, Any]] = []

        root = QWidget()
        layout = QVBoxLayout(root)

        title = QLabel("대화 정보")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        connection_group = QGroupBox("연결 상태")
        connection_form = QFormLayout(connection_group)
        self.connection_labels: dict[str, QLabel] = {}
        for key, label_text in self._CONNECTION_FIELDS:
            label = QLabel("확인 안 됨")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            self.connection_labels[key] = label
            connection_form.addRow(label_text, label)
        layout.addWidget(connection_group)

        memories_group = QGroupBox("이번 응답에 실제 사용된 기억")
        memories_layout = QVBoxLayout(memories_group)
        self.retrieval_summary = QLabel("검색 백엔드: 확인 안 됨")
        self.retrieval_summary.setWordWrap(True)
        memories_layout.addWidget(self.retrieval_summary)
        self.memory_summary = QLabel("이번 응답에 사용된 기억이 없습니다.")
        self.memory_summary.setWordWrap(True)
        memories_layout.addWidget(self.memory_summary)
        self.used_memories_table = QTableWidget(0, 8)
        self.used_memories_table.setHorizontalHeaderLabels(
            ["ID", "분류", "우선순위", "관련도", "최종 점수", "포함", "이유", "요약"]
        )
        self.used_memories_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.used_memories_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.used_memories_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.used_memories_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.used_memories_table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )
        memories_layout.addWidget(self.used_memories_table)
        layout.addWidget(memories_group, 1)

        generation_group = QGroupBox("최근 응답 생성 정보")
        generation_form = QFormLayout(generation_group)
        self.generation_labels: dict[str, QLabel] = {}
        for key, title_text in (
            ("model", "모델"),
            ("prompt_tokens", "입력 토큰"),
            ("completion_tokens", "출력 토큰"),
            ("total_tokens", "전체 토큰"),
            ("first_token_latency_ms", "첫 토큰 지연"),
            ("total_latency_ms", "전체 지연"),
            ("tokens_per_second", "생성 속도"),
            ("finish_reason", "종료 이유"),
            ("interrupted", "중단됨"),
            ("request_id", "요청 ID"),
        ):
            label = QLabel("unsupported")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            self.generation_labels[key] = label
            generation_form.addRow(title_text, label)
        layout.addWidget(generation_group)
        self.setCentralWidget(root)

    @property
    def used_memories(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(memory) for memory in self._used_memories)

    def set_connection_info(self, value: dict[str, Any]) -> None:
        """Replace every connection field, using placeholders for absent values."""

        for label in self.connection_labels.values():
            label.setText("확인 안 됨")
        self.update_connection_info(value)

    def update_connection_info(self, value: dict[str, Any]) -> None:
        """Update only supplied connection fields without clearing the others."""

        connection = dict(value)
        if not any(alias in connection for alias in self._CONNECTION_ALIASES["address"]):
            host = connection.get("host")
            port = connection.get("port")
            if host not in (None, ""):
                connection["address"] = f"{host}:{port}" if port not in (None, "") else host

        for key, aliases in self._CONNECTION_ALIASES.items():
            supplied = next((alias for alias in aliases if alias in connection), None)
            if supplied is None:
                continue
            raw_value = connection[supplied]
            self.connection_labels[key].setText(self._format_connection_value(key, raw_value))

    def set_used_memories(self, values: list[dict[str, Any]]) -> None:
        """Replace the read-only list of memories used for the current response."""

        self._used_memories = [dict(value) for value in values]
        self.used_memories_table.setRowCount(0)
        for value in self._used_memories:
            row = self.used_memories_table.rowCount()
            self.used_memories_table.insertRow(row)
            score = value.get("relevance_score")
            columns = (
                value.get("memory_id", value.get("id", "")),
                value.get("category", ""),
                value.get("priority", ""),
                self._format_score(score),
                self._format_score(value.get("final_score")),
                "예" if value.get("included") is True else "아니요",
                value.get("reason", "selected" if value.get("included") else ""),
                value.get("summary", value.get("content", "")),
            )
            for column, item_value in enumerate(columns):
                item = QTableWidgetItem(str(item_value))
                item.setData(Qt.ItemDataRole.UserRole, dict(value))
                self.used_memories_table.setItem(row, column, item)
        count = len(self._used_memories)
        self.memory_summary.setText(
            f"이번 응답에 사용된 기억: {count}개"
            if count
            else "이번 응답에 사용된 기억이 없습니다."
        )

    def clear_response_info(self) -> None:
        self.retrieval_summary.setText("검색 백엔드: 확인 안 됨")
        self.set_used_memories([])
        self.set_generation_metrics({})

    def set_retrieval_context(self, payload: dict[str, Any]) -> None:
        retrieval_value = payload.get("retrieval")
        retrieval = retrieval_value if isinstance(retrieval_value, dict) else {}
        backend = retrieval.get("backend") or "확인 안 됨"
        candidate_count = retrieval.get("candidate_count")
        top_k = retrieval.get("top_k")
        details = [f"검색 백엔드: {backend}"]
        if isinstance(candidate_count, int):
            details.append(f"후보 {candidate_count}개")
        if isinstance(top_k, int):
            details.append(f"top-k {top_k}")
        self.retrieval_summary.setText(" · ".join(details))
        memories = payload.get("memories")
        if isinstance(memories, list) and all(isinstance(item, dict) for item in memories):
            self.set_used_memories(memories)

    def set_generation_metrics(self, value: dict[str, Any]) -> None:
        for key, label in self.generation_labels.items():
            raw = value.get(key)
            if raw is None:
                label.setText("unsupported")
            elif key.endswith("_ms") and isinstance(raw, (int, float)):
                label.setText(f"{raw:.1f} ms")
            elif key == "tokens_per_second" and isinstance(raw, (int, float)):
                label.setText(f"{raw:.2f} token/s")
            elif key == "interrupted" and isinstance(raw, bool):
                label.setText("예" if raw else "아니요")
            else:
                label.setText(str(raw))

    @staticmethod
    def _format_connection_value(key: str, value: Any) -> str:
        if value in (None, ""):
            return "확인 안 됨"
        if key == "profile" and isinstance(value, ConnectionProfile):
            return value.id
        if key == "rtt_ms" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:g} ms"
        if key == "reconnect_attempts" and isinstance(value, int) and not isinstance(value, bool):
            return f"{value}회"
        if key == "consecutive_failures" and isinstance(value, int) and not isinstance(value, bool):
            return f"{value}회"
        if key == "tls" and isinstance(value, bool):
            return "사용" if value else "사용 안 함"
        if key == "uptime" and isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = max(0, int(value))
            return f"{seconds // 3600}시간 {(seconds % 3600) // 60}분"
        return str(value)

    @staticmethod
    def _format_score(value: Any) -> str:
        if value in (None, ""):
            return "-"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:.3f}".rstrip("0").rstrip(".")
        return str(value)


class MessageBubble(QWidget):
    """A single, plain-text chat message with role-specific alignment."""

    def __init__(
        self,
        role: str,
        content: str = "",
        parent: QWidget | None = None,
        *,
        message_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = "user" if role == "user" else "assistant"
        self.content = content
        self.message_id = message_id
        self.request_id = request_id

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        bubble = QFrame()
        bubble.setObjectName(f"{self.role}MessageBubble")
        bubble.setMaximumWidth(720)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(14, 10, 14, 10)
        bubble_layout.setSpacing(4)

        self.role_label = QLabel("나" if self.role == "user" else KOREAN_CALL_NAME)
        self.role_label.setStyleSheet("font-weight: 600; color: white;")
        self.content_label = QLabel()
        self.content_label.setStyleSheet("color: white;")
        self.content_label.setTextFormat(Qt.TextFormat.PlainText)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.content_label.setWordWrap(True)
        self.content_label.setText(content)
        bubble_layout.addWidget(self.role_label)
        bubble_layout.addWidget(self.content_label)

        if self.role == "user":
            bubble.setStyleSheet(
                "QFrame#userMessageBubble { background: #245b9e; color: white; "
                "border-radius: 10px; }"
            )
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            bubble.setStyleSheet(
                "QFrame#assistantMessageBubble { background: #353535; color: white; "
                "border-radius: 10px; }"
            )
            row.addWidget(bubble)
            row.addStretch(1)

    def append_text(self, text: str) -> None:
        self.content += text
        self.content_label.setText(self.content)

    def replace_text(self, text: str) -> None:
        self.content = text
        self.content_label.setText(text)


class ToolApprovalCard(QFrame):
    """Persistent, non-secret approval state rendered inside the chat timeline."""

    decision_requested = Signal(str, str)

    def __init__(self, payload: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.payload = dict(payload)
        self.tool_call_id = str(payload.get("tool_call_id") or "")
        self._decided = False
        self.setObjectName("toolApprovalCard")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            "QFrame#toolApprovalCard { background: #2c2c31; border: 1px solid #555; "
            "border-radius: 9px; } QLabel { color: white; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        title = QLabel(str(payload.get("display_name") or payload.get("tool_name") or "도구 요청"))
        title.setStyleSheet("font-weight: 700; font-size: 14px;")
        layout.addWidget(title)

        action = QLabel(str(payload.get("action_summary") or "로컬 작업을 요청했습니다."))
        action.setWordWrap(True)
        action.setTextFormat(Qt.TextFormat.PlainText)
        action.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(action)

        details = self._safe_details(payload)
        self.details_label = QLabel(details)
        self.details_label.setWordWrap(True)
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_label.setStyleSheet("color: #c8c8cc;")
        layout.addWidget(self.details_label)

        preview = payload.get("preview")
        if isinstance(preview, dict) and preview:
            preview_title = QLabel("승인할 내용 미리보기")
            preview_title.setStyleSheet("font-weight: 600;")
            layout.addWidget(preview_title)
            preview_text = QPlainTextEdit()
            preview_text.setReadOnly(True)
            preview_text.setPlainText(
                "\n\n".join(
                    (
                        f"제목: {preview.get('title') or '-'}",
                        str(preview.get("content") or ""),
                    )
                )
            )
            preview_text.setMaximumHeight(150)
            preview_text.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            layout.addWidget(preview_text)

        self.status_label = QLabel("승인 대기 중")
        self.status_label.setStyleSheet("color: #f0c674; font-weight: 600;")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.deny_button = QPushButton("거부")
        self.once_button = QPushButton("한 번 허용")
        self.session_button = QPushButton("이 세션에서 허용")
        self.always_button = QPushButton("이 대상만 항상 허용")
        self.cancel_button = QPushButton("실행 취소")
        for button in (
            self.deny_button,
            self.once_button,
            self.session_button,
            self.always_button,
            self.cancel_button,
        ):
            button.setAutoDefault(False)
            button.setDefault(False)
            buttons.addWidget(button)
        modes = {str(value) for value in payload.get("approval_modes", [])}
        risk = str(payload.get("risk_level") or "")
        self.session_button.setVisible(
            "allow_session" in modes and risk != "LOCAL_WRITE"
        )
        self.always_button.setVisible(
            "allow_always_exact" in modes and risk != "LOCAL_WRITE"
        )
        self._cancellation_supported = bool(payload.get("cancellation_supported"))
        self.cancel_button.setVisible(False)
        if payload.get("read_only"):
            for button in (
                self.deny_button,
                self.once_button,
                self.session_button,
                self.always_button,
                self.cancel_button,
            ):
                button.setVisible(False)
        self.deny_button.clicked.connect(lambda: self._decide("deny"))
        self.once_button.clicked.connect(lambda: self._decide("allow_once"))
        self.session_button.clicked.connect(lambda: self._decide("allow_session"))
        self.always_button.clicked.connect(lambda: self._decide("allow_always_exact"))
        self.cancel_button.clicked.connect(self._cancel)
        layout.addLayout(buttons)

        self._expiration_timer = QTimer(self)
        self._expiration_timer.setSingleShot(True)
        self._expiration_timer.timeout.connect(self.expire)
        expires_in = payload.get("expires_in_seconds")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            self._expiration_timer.start(max(1, int(expires_in * 1000)))

    @staticmethod
    def _safe_details(payload: dict[str, Any]) -> str:
        lines = [
            f"대상 Link: {payload.get('target_client_name') or payload.get('target_client_id') or '-'}",
            f"위험 등급: {payload.get('risk_level') or '-'}",
        ]
        target = payload.get("target_summary")
        if target:
            lines.append(f"대상: {target}")
        reason = payload.get("reason") or payload.get("user_intent_summary")
        if reason:
            lines.append(f"요청 이유: {reason}")
        arguments = payload.get("arguments")
        if isinstance(arguments, dict):
            safe_values: list[str] = []
            for key in (
                "application_id",
                "root_id",
                "path_ref",
                "query",
                "title",
                "scheduled_at",
                "format",
            ):
                value = arguments.get(key)
                if value not in (None, ""):
                    safe_values.append(f"{key}: {value}")
            lines.extend(safe_values)
        return "\n".join(str(line) for line in lines)

    def _decide(self, decision: str) -> None:
        if self._decided or not self.tool_call_id:
            return
        self._decided = True
        self._expiration_timer.stop()
        self._set_buttons_enabled(False)
        label = {
            "deny": "거부됨",
            "allow_once": "한 번 허용됨",
            "allow_session": "이 세션에서 허용됨",
            "allow_always_exact": "이 대상만 항상 허용됨",
        }.get(decision, decision)
        self.status_label.setText(label)
        self.decision_requested.emit(self.tool_call_id, decision)

    def expire(self) -> None:
        if self._decided:
            return
        self._decided = True
        self._set_buttons_enabled(False)
        self.status_label.setText("승인 시간이 만료되었습니다.")
        if self.tool_call_id:
            self.decision_requested.emit(self.tool_call_id, "deny_expired")

    def _cancel(self) -> None:
        if not self._cancellation_supported or not self.tool_call_id:
            return
        self.cancel_button.setEnabled(False)
        self.status_label.setText("취소 요청 중")
        self.decision_requested.emit(self.tool_call_id, "cancel")

    def set_status(self, status: str, message: str | None = None) -> None:
        terminal = status in {
            "completed",
            "failed",
            "cancelled",
            "timed_out",
            "denied",
            "client_disconnected",
        }
        if terminal:
            self._decided = True
            self._expiration_timer.stop()
            self._set_buttons_enabled(False)
            self.cancel_button.setVisible(False)
        elif status in {"queued", "running", "cancelling"}:
            self.cancel_button.setVisible(self._cancellation_supported)
            self.cancel_button.setEnabled(status != "cancelling")
        self.status_label.setText(message or status)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.deny_button,
            self.once_button,
            self.session_button,
            self.always_button,
            self.cancel_button,
        ):
            button.setEnabled(enabled)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._decide("deny")
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            event.accept()
            return
        super().keyPressEvent(event)


class ConversationHistoryWindow(QMainWindow):
    refresh_requested = Signal()
    conversation_requested = Signal(str)
    new_conversation_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{LINK_COMPONENT_NAME} · 대화 기록")
        self.resize(820, 620)

        root = QWidget()
        layout = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        title = QLabel("저장된 대화")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.new_button = QPushButton("새 대화")
        self.refresh_button = QPushButton("새로고침")
        self.new_button.clicked.connect(self.new_conversation_requested.emit)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.new_button)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.message = QLabel("대화 기록을 불러오려면 새로고침을 누르세요.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.conversations = QListWidget()
        self.conversations.setMinimumWidth(260)
        self.conversations.currentItemChanged.connect(self._conversation_changed)
        splitter.addWidget(self.conversations)
        self.preview = QTextBrowser()
        self.preview.setPlaceholderText("왼쪽에서 대화를 선택하면 내용이 표시됩니다.")
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)
        self.setCentralWidget(root)

    def _conversation_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        conversation_id = current.data(Qt.ItemDataRole.UserRole)
        if conversation_id:
            self.conversation_requested.emit(str(conversation_id))

    def set_conversations(self, conversations: list[dict[str, Any]]) -> None:
        selected = self.conversations.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        self.conversations.blockSignals(True)
        self.conversations.clear()
        selected_row = -1
        for row, conversation in enumerate(conversations):
            title = str(conversation.get("title") or "제목 없는 대화")
            updated_at = str(conversation.get("updated_at") or "")
            label = f"{title}\n{updated_at}" if updated_at else title
            item = QListWidgetItem(label)
            conversation_id = str(conversation.get("id") or "")
            item.setData(Qt.ItemDataRole.UserRole, conversation_id)
            self.conversations.addItem(item)
            if selected_id and conversation_id == selected_id:
                selected_row = row
        self.conversations.blockSignals(False)
        if selected_row >= 0:
            self.conversations.blockSignals(True)
            self.conversations.setCurrentRow(selected_row)
            self.conversations.blockSignals(False)

    def set_preview(self, title: str, messages: list[dict[str, Any]]) -> None:
        lines = [title.strip() or "제목 없는 대화", ""]
        for message in messages:
            role = "나" if message.get("role") == "user" else KOREAN_CALL_NAME
            lines.extend((role, str(message.get("content") or ""), ""))
        self.preview.setPlainText("\n".join(lines).rstrip())

    def show_loading(self, loading: bool, text: str = "대화 기록을 불러오는 중…") -> None:
        self.refresh_button.setEnabled(not loading)
        self.new_button.setEnabled(not loading)
        self.conversations.setEnabled(not loading)
        if loading:
            self.message.setStyleSheet("color: #555;")
            self.message.setText(text)

    def show_message(self, message: str) -> None:
        self.message.setStyleSheet("color: #176b2c;")
        self.message.setText(message)

    def show_error(self, message: str) -> None:
        self.message.setStyleSheet("color: #b00020;")
        self.message.setText(message)


class PersonaWindow(QMainWindow):
    refresh_requested = Signal()
    save_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{KOREAN_FULL_NAME} · Persona")
        self.resize(680, 650)
        self._online = True
        self._loading = False
        self._persona: dict[str, Any] = {}

        root = QWidget()
        layout = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        title = QLabel(f"{KOREAN_FULL_NAME}의 성격")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        notice = QLabel(
            f"이 설정은 {KOREAN_CALL_NAME}의 말투와 응답 방식에 적용됩니다. "
            "안전 경계 설정은 이 화면에서 "
            "변경하지 않습니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        identity_group = QGroupBox("정체성과 말투")
        identity_form = QFormLayout(identity_group)
        self.persona_name = QLineEdit()
        self.full_name = QLineEdit()
        self.korean_full_name = QLineEdit()
        self.call_name = QLineEdit()
        self.profile_version = QLineEdit()
        self.persona_role = QLineEdit()
        self.user_name = QLineEdit()
        self.user_address = QLineEdit()
        self.default_language = QLineEdit()
        self.tone = QLineEdit()
        self.relationship_description = QLineEdit()
        self.lore = QTextEdit()
        self.lore.setMaximumHeight(100)
        identity_form.addRow("이름", self.persona_name)
        identity_form.addRow("영문 전체 이름", self.full_name)
        identity_form.addRow("한글 전체 이름", self.korean_full_name)
        identity_form.addRow("평상시 호칭", self.call_name)
        identity_form.addRow("Persona 버전", self.profile_version)
        identity_form.addRow("역할", self.persona_role)
        identity_form.addRow("사용자 이름", self.user_name)
        identity_form.addRow("사용자 호칭", self.user_address)
        identity_form.addRow("기본 언어", self.default_language)
        identity_form.addRow("말투", self.tone)
        identity_form.addRow("관계 설명", self.relationship_description)
        identity_form.addRow("간결한 설정", self.lore)
        content_layout.addWidget(identity_group)

        behavior_group = QGroupBox("응답 방식")
        behavior_form = QFormLayout(behavior_group)
        self.everyday_conversation = QLineEdit()
        self.technical_work = QLineEdit()
        self.correction_style = QLineEdit()
        self.praise_style = QLineEdit()
        self.persona_directives = QTextEdit()
        self.persona_directives.setMaximumHeight(180)
        self.verbosity = QComboBox()
        self.verbosity.addItems(["간결", "보통", "상세"])
        self.humor = QComboBox()
        self.humor.addItems(["없음", "절제됨", "적극적"])
        self.avoid_excessive_flattery = QCheckBox("과도한 칭찬 피하기")
        self.user_correction_priority = QCheckBox("사용자의 정정 우선")
        behavior_form.addRow("일상 대화", self.everyday_conversation)
        behavior_form.addRow("기술 작업", self.technical_work)
        behavior_form.addRow("정정 방식", self.correction_style)
        behavior_form.addRow("칭찬 방식", self.praise_style)
        behavior_form.addRow("Persona v1.0 원칙", self.persona_directives)
        behavior_form.addRow("답변 길이", self.verbosity)
        behavior_form.addRow("유머", self.humor)
        behavior_form.addRow(self.avoid_excessive_flattery)
        behavior_form.addRow(self.user_correction_priority)
        content_layout.addWidget(behavior_group)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        self.message = QLabel("서버에서 현재 성격 설정을 불러오세요.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.save_button = QPushButton("성격 저장")
        self.save_button.clicked.connect(self._request_save)
        layout.addWidget(self.save_button)
        self.setCentralWidget(root)
        self._editable_controls = (
            self.persona_name,
            self.full_name,
            self.korean_full_name,
            self.call_name,
            self.profile_version,
            self.persona_role,
            self.user_name,
            self.user_address,
            self.default_language,
            self.tone,
            self.relationship_description,
            self.lore,
            self.everyday_conversation,
            self.technical_work,
            self.correction_style,
            self.praise_style,
            self.persona_directives,
            self.verbosity,
            self.humor,
            self.avoid_excessive_flattery,
            self.user_correction_priority,
        )

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: object) -> None:
        text = str(value or "")
        if text and combo.findText(text) < 0:
            combo.addItem(text)
        if text:
            combo.setCurrentText(text)

    def set_persona(self, value: dict[str, Any]) -> None:
        self._persona = dict(value)
        identity_value = value.get("identity")
        behavior_value = value.get("behavior")
        identity: dict[str, Any] = dict(identity_value) if isinstance(identity_value, dict) else {}
        behavior: dict[str, Any] = dict(behavior_value) if isinstance(behavior_value, dict) else {}
        self.persona_name.setText(str(identity.get("name") or PRODUCT_NAME))
        self.full_name.setText(str(identity.get("full_name") or FULL_CHARACTER_NAME))
        self.korean_full_name.setText(
            str(identity.get("korean_full_name") or KOREAN_FULL_NAME)
        )
        self.call_name.setText(str(identity.get("call_name") or CALL_NAME))
        self.profile_version.setText(
            str(identity.get("profile_version") or PERSONA_VERSION)
        )
        self.persona_role.setText(str(identity.get("role") or DEFAULT_ROLE))
        self.user_name.setText(str(identity.get("user_name") or USER_NAME))
        self.user_address.setText(str(identity.get("user_address") or USER_NAME))
        self.default_language.setText(str(identity.get("default_language") or "ko"))
        self.tone.setText(str(identity.get("tone") or DEFAULT_TONE))
        self.relationship_description.setText(
            str(identity.get("relationship_description") or DEFAULT_RELATIONSHIP)
        )
        self.lore.setPlainText(str(identity.get("lore") or DEFAULT_LORE))
        self.everyday_conversation.setText(str(behavior.get("everyday_conversation") or ""))
        self.technical_work.setText(str(behavior.get("technical_work") or ""))
        self.correction_style.setText(str(behavior.get("correction_style") or ""))
        self.praise_style.setText(str(behavior.get("praise_style") or ""))
        self.persona_directives.setPlainText(
            str(behavior.get("persona_directives") or DEFAULT_PERSONA_DIRECTIVES)
        )
        self._set_combo_text(self.verbosity, behavior.get("verbosity") or "보통")
        self._set_combo_text(self.humor, behavior.get("humor") or "절제됨")
        self.avoid_excessive_flattery.setChecked(
            bool(behavior.get("avoid_excessive_flattery", True))
        )
        self.user_correction_priority.setChecked(
            bool(behavior.get("user_correction_priority", True))
        )

    def persona_payload(self) -> dict[str, Any]:
        return {
            "identity": {
                "name": self.persona_name.text().strip(),
                "full_name": self.full_name.text().strip(),
                "korean_full_name": self.korean_full_name.text().strip(),
                "call_name": self.call_name.text().strip(),
                "profile_version": self.profile_version.text().strip(),
                "role": self.persona_role.text().strip(),
                "user_name": self.user_name.text().strip(),
                "user_address": self.user_address.text().strip(),
                "default_language": self.default_language.text().strip(),
                "tone": self.tone.text().strip(),
                "relationship_description": self.relationship_description.text().strip(),
                "lore": self.lore.toPlainText().strip(),
            },
            "behavior": {
                "everyday_conversation": self.everyday_conversation.text().strip(),
                "technical_work": self.technical_work.text().strip(),
                "correction_style": self.correction_style.text().strip(),
                "praise_style": self.praise_style.text().strip(),
                "persona_directives": self.persona_directives.toPlainText().strip(),
                "verbosity": self.verbosity.currentText(),
                "humor": self.humor.currentText(),
                "avoid_excessive_flattery": self.avoid_excessive_flattery.isChecked(),
                "user_correction_priority": self.user_correction_priority.isChecked(),
            },
        }

    def _request_save(self) -> None:
        self.save_requested.emit(self.persona_payload())

    def show_loading(self, loading: bool, text: str = "성격 설정을 불러오는 중…") -> None:
        self._loading = loading
        self._update_control_state()
        if loading:
            self.message.setStyleSheet("color: #555;")
            self.message.setText(text)

    def set_online(self, online: bool) -> None:
        """Keep the loaded persona readable while preventing offline edits."""

        was_online = self._online
        self._online = online
        self._update_control_state()
        if not online:
            self.message.setStyleSheet("color: #8a5a00;")
            self.message.setText("서버 오프라인 · 마지막 성격 설정을 읽기 전용으로 표시합니다.")
        elif not was_online:
            self.show_message("서버가 다시 연결되었습니다. 새로고침하여 최신 설정을 확인하세요.")

    def _update_control_state(self) -> None:
        self.refresh_button.setEnabled(not self._loading)
        can_modify = self._online and not self._loading
        self.save_button.setEnabled(can_modify)
        for control in self._editable_controls:
            control.setEnabled(can_modify)

    def show_message(self, message: str) -> None:
        self.message.setStyleSheet("color: #176b2c;")
        self.message.setText(message)

    def show_error(self, message: str) -> None:
        self.message.setStyleSheet("color: #b00020;")
        self.message.setText(message)


class ToolPolicyDialog(QDialog):
    """Edit one advertised tool without exposing any server-owned setting."""

    def __init__(self, row: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"도구 정책 · {row.get('name', '-')}")
        form = QFormLayout(self)
        self.enabled = QCheckBox("이 클라이언트에서 사용")
        self.enabled.setChecked(bool(row.get("enabled")))
        self.approval = QComboBox()
        risk = str(row.get("risk_level") or "")
        if risk == "LOCAL_WRITE":
            modes = [("매번 한 번만 허용", "allow_once")]
        elif row.get("name") == "get_system_status":
            modes = [
                ("승인 불필요", "not_required"),
                ("매번 한 번만 허용", "allow_once"),
                ("현재 세션에서 허용", "allow_session"),
            ]
        else:
            modes = [
                ("매번 한 번만 허용", "allow_once"),
                ("현재 세션에서 허용", "allow_session"),
            ]
        for label, value in modes:
            self.approval.addItem(label, value)
        configured_mode = str(row.get("approval_mode") or "allow_once")
        configured_index = self.approval.findData(configured_mode)
        self.approval.setCurrentIndex(max(0, configured_index))
        self.timeout = QSpinBox()
        self.timeout.setRange(100, int(row.get("maximum_timeout_ms") or 120_000))
        self.timeout.setSuffix(" ms")
        self.timeout.setValue(int(row.get("timeout_ms") or 10_000))
        availability = QLabel("사용 가능" if row.get("available") else "구현되지 않음")
        form.addRow("도구", QLabel(str(row.get("name") or "-")))
        form.addRow("위험 등급", QLabel(risk or "-"))
        form.addRow("상태", self.enabled)
        form.addRow("기본 승인", self.approval)
        form.addRow("제한 시간", self.timeout)
        form.addRow("가용성", availability)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ApplicationPolicyDialog(QDialog):
    def __init__(self, row: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = row or {}
        self.setWindowTitle("등록 애플리케이션 편집" if row else "등록 애플리케이션 추가")
        form = QFormLayout(self)
        self.application_id = QLineEdit(str(row.get("application_id") or ""))
        self.application_id.setPlaceholderText("예: vscode")
        self.display_name = QLineEdit(str(row.get("display_name") or ""))
        self.executable_path = QLineEdit(str(row.get("executable_path") or ""))
        browse = QPushButton("찾아보기")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.executable_path)
        path_row.addWidget(browse)
        self.enabled = QCheckBox("사용")
        self.enabled.setChecked(bool(row.get("enabled", True)))
        form.addRow("애플리케이션 ID", self.application_id)
        form.addRow("표시 이름", self.display_name)
        form.addRow("실행 파일 (.exe)", path_row)
        form.addRow("상태", self.enabled)
        warning = QLabel("셸, 인터프리터, 스크립트 호스트와 설치 프로그램은 등록할 수 없습니다.")
        warning.setWordWrap(True)
        form.addRow(warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_complete)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "실행 파일 선택", self.executable_path.text(), "Windows 실행 파일 (*.exe)"
        )
        if path:
            self.executable_path.setText(path)

    def _accept_if_complete(self) -> None:
        if not all(
            field.text().strip()
            for field in (self.application_id, self.display_name, self.executable_path)
        ):
            QMessageBox.warning(self, "입력 확인", "ID, 표시 이름, 실행 파일을 모두 입력하세요.")
            return
        self.accept()


class FilesystemRootDialog(QDialog):
    def __init__(self, row: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = row or {}
        self.setWindowTitle("파일시스템 루트 편집" if row else "파일시스템 루트 추가")
        form = QFormLayout(self)
        self.root_id = QLineEdit(str(row.get("root_id") or ""))
        self.root_id.setPlaceholderText("예: projects")
        self.display_name = QLineEdit(str(row.get("display_name") or ""))
        self.path = QLineEdit(str(row.get("path") or ""))
        browse = QPushButton("찾아보기")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path)
        path_row.addWidget(browse)
        self.allow_search = QCheckBox("파일 이름 검색")
        self.allow_search.setChecked(bool(row.get("allow_search")))
        self.allow_read = QCheckBox("텍스트 파일 읽기")
        self.allow_read.setChecked(bool(row.get("allow_read")))
        self.allow_open = QCheckBox("폴더 열기")
        self.allow_open.setChecked(bool(row.get("allow_open")))
        permissions = QHBoxLayout()
        permissions.addWidget(self.allow_search)
        permissions.addWidget(self.allow_read)
        permissions.addWidget(self.allow_open)
        form.addRow("Root ID", self.root_id)
        form.addRow("표시 이름", self.display_name)
        form.addRow("폴더", path_row)
        form.addRow("권한", permissions)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_complete)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "루트 폴더 선택", self.path.text())
        if path:
            self.path.setText(path)

    def _accept_if_complete(self) -> None:
        if not all(field.text().strip() for field in (self.root_id, self.display_name, self.path)):
            QMessageBox.warning(self, "입력 확인", "Root ID, 표시 이름, 폴더를 모두 입력하세요.")
            return
        self.accept()


class AgentManagementWindow(QMainWindow):
    """Local-only Nivelle Agent policy, capability, approval, and audit view."""

    refresh_requested = Signal()
    enabled_changed = Signal(bool)
    revoke_requested = Signal(str)
    tool_policy_changed = Signal(str, bool, str, int)
    application_upsert_requested = Signal(str, str, str, str, bool)
    application_remove_requested = Signal(str)
    root_upsert_requested = Signal(str, str, str, str, bool, bool, bool)
    root_remove_requested = Signal(str)
    path_policy_changed = Signal(bool, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{AGENT_COMPONENT_NAME} · 도구와 권한")
        self.resize(980, 700)
        self._loading = False
        self._snapshot: dict[str, Any] = {}
        geometry = QSettings("Nivelle", "NivelleLink").value(
            "agent_management/geometry"
        )
        if geometry is not None:
            self.restoreGeometry(geometry)

        root = QWidget()
        layout = QHBoxLayout(root)
        self.sections = QListWidget()
        self.sections.addItems(["개요", "도구", "애플리케이션", "파일시스템", "승인", "감사"])
        self.sections.setMaximumWidth(180)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        toolbar = QHBoxLayout()
        title = QLabel(f"{AGENT_COMPONENT_NAME} · Tools and Permissions")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.enabled = QCheckBox("Agent 사용")
        self.refresh_button = QPushButton("새로고침")
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.enabled)
        toolbar.addWidget(self.refresh_button)
        right_layout.addLayout(toolbar)

        self.message = QLabel("로컬 정책을 불러오려면 새로고침을 누르세요.")
        self.message.setWordWrap(True)
        right_layout.addWidget(self.message)

        self.pages = QStackedWidget()
        self.overview = QTextBrowser()
        self.pages.addWidget(self.overview)
        tools_page = QWidget()
        tools_layout = QVBoxLayout(tools_page)
        self.tools_table = self._table(
            ["이름", "사용", "위험", "기본 승인", "가용성", "제한 시간", "마지막 사용"]
        )
        self.edit_tool_button = QPushButton("선택 도구 정책 편집")
        self.edit_tool_button.clicked.connect(self._edit_selected_tool)
        self.tools_table.doubleClicked.connect(lambda _index: self._edit_selected_tool())
        tools_layout.addWidget(self.tools_table)
        tools_layout.addWidget(self.edit_tool_button)
        self.pages.addWidget(tools_page)
        applications_page = QWidget()
        applications_layout = QVBoxLayout(applications_page)
        self.applications_table = self._table(
            ["ID", "표시 이름", "실행 파일", "사용", "영구 승인"]
        )
        application_buttons = QHBoxLayout()
        self.add_application_button = QPushButton("추가")
        self.edit_application_button = QPushButton("편집")
        self.remove_application_button = QPushButton("제거")
        self.add_application_button.clicked.connect(self._add_application)
        self.edit_application_button.clicked.connect(self._edit_selected_application)
        self.remove_application_button.clicked.connect(self._remove_selected_application)
        application_buttons.addWidget(self.add_application_button)
        application_buttons.addWidget(self.edit_application_button)
        application_buttons.addWidget(self.remove_application_button)
        application_buttons.addStretch()
        applications_layout.addWidget(self.applications_table)
        applications_layout.addLayout(application_buttons)
        self.pages.addWidget(applications_page)
        roots_page = QWidget()
        roots_layout = QVBoxLayout(roots_page)
        path_policy = QGroupBox("공통 경로 정책")
        path_policy_layout = QHBoxLayout(path_policy)
        self.allow_hidden_files = QCheckBox("숨김 파일 포함")
        self.allow_network_paths = QCheckBox("네트워크 경로 허용")
        self.save_path_policy_button = QPushButton("경로 정책 저장")
        self.save_path_policy_button.clicked.connect(
            lambda: self.path_policy_changed.emit(
                self.allow_hidden_files.isChecked(),
                self.allow_network_paths.isChecked(),
            )
        )
        path_policy_layout.addWidget(self.allow_hidden_files)
        path_policy_layout.addWidget(self.allow_network_paths)
        path_policy_layout.addStretch()
        path_policy_layout.addWidget(self.save_path_policy_button)
        self.roots_table = self._table(
            ["Root ID", "표시 이름", "정규 경로", "검색", "읽기", "폴더 열기", "숨김", "네트워크"]
        )
        root_buttons = QHBoxLayout()
        self.add_root_button = QPushButton("추가")
        self.edit_root_button = QPushButton("편집")
        self.remove_root_button = QPushButton("제거")
        self.add_root_button.clicked.connect(self._add_root)
        self.edit_root_button.clicked.connect(self._edit_selected_root)
        self.remove_root_button.clicked.connect(self._remove_selected_root)
        root_buttons.addWidget(self.add_root_button)
        root_buttons.addWidget(self.edit_root_button)
        root_buttons.addWidget(self.remove_root_button)
        root_buttons.addStretch()
        roots_layout.addWidget(path_policy)
        roots_layout.addWidget(self.roots_table)
        roots_layout.addLayout(root_buttons)
        self.pages.addWidget(roots_page)
        approval_page = QWidget()
        approval_layout = QVBoxLayout(approval_page)
        self.approvals_table = self._table(
            ["승인 ID", "도구", "범위", "종류", "생성", "마지막 사용"]
        )
        self.revoke_button = QPushButton("선택 승인 철회")
        self.revoke_button.clicked.connect(self._revoke_selected)
        approval_layout.addWidget(self.approvals_table)
        approval_layout.addWidget(self.revoke_button)
        self.pages.addWidget(approval_page)
        self.audit_table = self._table(
            ["시각", "도구", "상태", "대상 요약", "소요 시간", "오류"]
        )
        self.pages.addWidget(self.audit_table)
        right_layout.addWidget(self.pages)

        layout.addWidget(self.sections, 1)
        layout.addWidget(right, 5)
        self.setCentralWidget(root)
        self.sections.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sections.setCurrentRow(0)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.enabled.toggled.connect(self.enabled_changed.emit)

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return table

    def set_snapshot(self, value: dict[str, Any]) -> None:
        self._snapshot = dict(value)
        self.enabled.blockSignals(True)
        self.enabled.setChecked(bool(value.get("enabled", False)))
        self.enabled.blockSignals(False)
        overview = {
            "Agent 상태": "사용" if value.get("enabled") else "사용 안 함",
            "연결된 Core": value.get("connected_core") or "오프라인",
            "클라이언트 ID": value.get("client_id") or "확인 안 됨",
            "세션 ID": value.get("session_id") or "확인 안 됨",
            "활성 도구": value.get("enabled_tool_count", 0),
            "승인 대기": value.get("pending_approval_count", 0),
            "최근 실패": value.get("recent_failure_count", 0),
        }
        self.overview.setPlainText(
            "\n".join(f"{key}: {item}" for key, item in overview.items())
        )
        last_used_by_tool: dict[str, str] = {}
        audit_rows = value.get("audit")
        if isinstance(audit_rows, list):
            for audit_row in audit_rows:
                if not isinstance(audit_row, dict):
                    continue
                name = str(audit_row.get("tool_name") or "")
                timestamp = str(audit_row.get("created_at") or "")
                if name and timestamp:
                    last_used_by_tool[name] = max(
                        timestamp, last_used_by_tool.get(name, "")
                    )
        tool_rows = []
        values = value.get("tools")
        if isinstance(values, list):
            for row in values:
                if isinstance(row, dict):
                    enriched = dict(row)
                    enriched["last_used_at"] = last_used_by_tool.get(
                        str(row.get("name") or "")
                    )
                    tool_rows.append(enriched)
        self._populate(
            self.tools_table,
            tool_rows,
            (
                "name",
                "enabled",
                "risk_level",
                "approval_mode",
                "available",
                "timeout_ms",
                "last_used_at",
            ),
            id_key="name",
        )
        self._populate(
            self.applications_table,
            value.get("applications"),
            ("application_id", "display_name", "executable_path", "enabled", "persistent_approval"),
            id_key="application_id",
        )
        self.allow_hidden_files.blockSignals(True)
        self.allow_network_paths.blockSignals(True)
        self.allow_hidden_files.setChecked(bool(value.get("allow_hidden_files", False)))
        self.allow_network_paths.setChecked(bool(value.get("allow_network_paths", False)))
        self.allow_hidden_files.blockSignals(False)
        self.allow_network_paths.blockSignals(False)
        roots = []
        root_values = value.get("roots")
        if isinstance(root_values, list):
            for row in root_values:
                if isinstance(row, dict):
                    enriched = dict(row)
                    enriched["hidden_policy"] = bool(value.get("allow_hidden_files", False))
                    enriched["network_policy"] = bool(value.get("allow_network_paths", False))
                    roots.append(enriched)
        self._populate(
            self.roots_table,
            roots,
            (
                "root_id",
                "display_name",
                "path",
                "allow_search",
                "allow_read",
                "allow_open",
                "hidden_policy",
                "network_policy",
            ),
            id_key="root_id",
        )
        self._populate(
            self.approvals_table,
            value.get("approvals"),
            ("approval_id", "tool_name", "scope", "mode", "created_at", "last_used_at"),
            id_key="approval_id",
        )
        self._populate(
            self.audit_table,
            value.get("audit"),
            ("created_at", "tool_name", "status", "target_summary", "duration_ms", "error_code"),
        )
        self.show_message("로컬 Agent 정책을 불러왔습니다.")

    def _selected_payload(self, table: QTableWidget, key: str) -> dict[str, Any] | None:
        row_index = table.currentRow()
        if row_index < 0:
            return None
        item = table.item(row_index, 0)
        selected_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        rows = self._snapshot.get(key)
        if not isinstance(rows, list):
            return None
        id_keys = {"tools": "name", "applications": "application_id", "roots": "root_id"}
        id_key = id_keys[key]
        return next(
            (
                dict(value)
                for value in rows
                if isinstance(value, dict) and str(value.get(id_key) or "") == str(selected_id)
            ),
            None,
        )

    def _edit_selected_tool(self) -> None:
        row = self._selected_payload(self.tools_table, "tools")
        if row is None:
            self.show_error("편집할 도구를 선택하세요.")
            return
        definition = next(
            (item for item in TOOL_REGISTRY if item.name == row.get("name")), None
        )
        if definition is not None:
            row["maximum_timeout_ms"] = definition.maximum_timeout_ms
        dialog = ToolPolicyDialog(row, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.tool_policy_changed.emit(
            str(row["name"]),
            dialog.enabled.isChecked(),
            str(dialog.approval.currentData()),
            dialog.timeout.value(),
        )

    def _application_dialog(
        self, row: dict[str, Any] | None = None
    ) -> ApplicationPolicyDialog:
        return ApplicationPolicyDialog(row, self)

    def _add_application(self) -> None:
        self._save_application_dialog(None)

    def _edit_selected_application(self) -> None:
        row = self._selected_payload(self.applications_table, "applications")
        if row is None:
            self.show_error("편집할 애플리케이션을 선택하세요.")
            return
        self._save_application_dialog(row)

    def _save_application_dialog(self, row: dict[str, Any] | None) -> None:
        dialog = self._application_dialog(row)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        previous_id = str((row or {}).get("application_id") or "")
        self.application_upsert_requested.emit(
            previous_id,
            dialog.application_id.text().strip(),
            dialog.display_name.text().strip(),
            dialog.executable_path.text().strip(),
            dialog.enabled.isChecked(),
        )

    def _remove_selected_application(self) -> None:
        row = self._selected_payload(self.applications_table, "applications")
        if row is None:
            self.show_error("제거할 애플리케이션을 선택하세요.")
            return
        application_id = str(row.get("application_id") or "")
        if QMessageBox.question(
            self,
            "등록 제거",
            f"'{application_id}' 애플리케이션 등록을 제거할까요?",
        ) == QMessageBox.StandardButton.Yes:
            self.application_remove_requested.emit(application_id)

    def _root_dialog(self, row: dict[str, Any] | None = None) -> FilesystemRootDialog:
        return FilesystemRootDialog(row, self)

    def _add_root(self) -> None:
        self._save_root_dialog(None)

    def _edit_selected_root(self) -> None:
        row = self._selected_payload(self.roots_table, "roots")
        if row is None:
            self.show_error("편집할 파일시스템 루트를 선택하세요.")
            return
        self._save_root_dialog(row)

    def _save_root_dialog(self, row: dict[str, Any] | None) -> None:
        dialog = self._root_dialog(row)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        previous_id = str((row or {}).get("root_id") or "")
        self.root_upsert_requested.emit(
            previous_id,
            dialog.root_id.text().strip(),
            dialog.display_name.text().strip(),
            dialog.path.text().strip(),
            dialog.allow_search.isChecked(),
            dialog.allow_read.isChecked(),
            dialog.allow_open.isChecked(),
        )

    def _remove_selected_root(self) -> None:
        row = self._selected_payload(self.roots_table, "roots")
        if row is None:
            self.show_error("제거할 파일시스템 루트를 선택하세요.")
            return
        root_id = str(row.get("root_id") or "")
        if QMessageBox.question(
            self,
            "루트 제거",
            f"'{root_id}' 파일시스템 루트를 제거할까요?",
        ) == QMessageBox.StandardButton.Yes:
            self.root_remove_requested.emit(root_id)

    @staticmethod
    def _populate(
        table: QTableWidget,
        rows: object,
        keys: tuple[str, ...],
        *,
        id_key: str | None = None,
    ) -> None:
        values = rows if isinstance(rows, list) else []
        safe_rows = [row for row in values if isinstance(row, dict)]
        table.setRowCount(len(safe_rows))
        for row_index, row in enumerate(safe_rows):
            for column, key in enumerate(keys):
                raw = row.get(key)
                text = "예" if raw is True else "아니요" if raw is False else str(raw or "-")
                item = QTableWidgetItem(text)
                if id_key is not None:
                    item.setData(Qt.ItemDataRole.UserRole, str(row.get(id_key) or ""))
                table.setItem(row_index, column, item)

    def _revoke_selected(self) -> None:
        row = self.approvals_table.currentRow()
        if row < 0:
            return
        item = self.approvals_table.item(row, 0)
        approval_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if approval_id:
            self.revoke_requested.emit(str(approval_id))

    def show_loading(self, loading: bool) -> None:
        self._loading = loading
        self.refresh_button.setEnabled(not loading)
        self.enabled.setEnabled(not loading)
        for control in (
            self.edit_tool_button,
            self.add_application_button,
            self.edit_application_button,
            self.remove_application_button,
            self.add_root_button,
            self.edit_root_button,
            self.remove_root_button,
            self.save_path_policy_button,
            self.revoke_button,
        ):
            control.setEnabled(not loading)
        if loading:
            self.message.setText("로컬 Agent 정책을 불러오는 중…")

    def closeEvent(self, event: QCloseEvent) -> None:
        QSettings("Nivelle", "NivelleLink").setValue(
            "agent_management/geometry", self.saveGeometry()
        )
        super().closeEvent(event)

    def show_message(self, message: str) -> None:
        self.message.setStyleSheet("color: #176b2c;")
        self.message.setText(message)

    def show_error(self, message: str) -> None:
        self.message.setStyleSheet("color: #b00020;")
        self.message.setText(message)


class MainChatWindow(QMainWindow):
    send_requested = Signal(str)
    reconnect_requested = Signal()
    disconnect_requested = Signal()
    conversation_info_requested = Signal()
    admin_requested = Signal()
    memory_requested = Signal()
    history_requested = Signal()
    persona_requested = Signal()
    agent_requested = Signal()
    tool_decision_requested = Signal(str, str)
    new_conversation_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{LINK_COMPONENT_NAME} · {KOREAN_FULL_NAME}")
        self.resize(900, 720)
        self.console: ServerConsoleWindow | None = None
        self.memory_window: MemoryArchiveWindow | None = None
        self.history_window: ConversationHistoryWindow | None = None
        self.persona_window: PersonaWindow | None = None
        self.agent_window: AgentManagementWindow | None = None
        self.conversation_info_window: ConversationInfoWindow | None = None
        self._management_online = False
        self._message_bubbles: list[MessageBubble] = []
        self._message_bubbles_by_id: dict[str, MessageBubble] = {}
        self._streaming_bubble: MessageBubble | None = None
        self._streaming_request_id: str | None = None
        self._streaming_message_id: str | None = None
        self._last_delta_sequence = 0
        self._completed_assistant_message_ids: set[str] = set()
        self._tool_cards_by_id: dict[str, ToolApprovalCard] = {}

        central = QWidget()
        layout = QVBoxLayout(central)
        header = QHBoxLayout()
        self.menu_button = QToolButton()
        self.menu_button.setText("≡")
        self.menu_button.setToolTip("메뉴")
        self.menu_button.setFixedSize(40, 32)
        self.menu_button.setStyleSheet(
            "QToolButton { font-size: 20px; border: none; } "
            "QToolButton::menu-indicator { image: none; }"
        )
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self.menu_button)
        self.new_conversation_action = QAction("새 대화", self)
        self.history_action = QAction("대화 기록", self)
        self.conversation_info_action = QAction("대화 정보", self)
        self.connection_action = QAction(f"{CORE_COMPONENT_NAME} 연결", self)
        self.disconnect_action = QAction("연결 끊기", self)
        self.disconnect_action.setEnabled(False)
        self.admin_action = QAction(f"{CORE_COMPONENT_NAME} 관리", self)
        self.memory_action = QAction(f"{ARCHIVE_COMPONENT_NAME} · 장기 기억", self)
        self.persona_action = QAction(f"{KOREAN_FULL_NAME} · 성격", self)
        self.agent_action = QAction(f"{AGENT_COMPONENT_NAME} · 도구와 권한", self)
        self.menu.addAction(self.new_conversation_action)
        self.menu.addAction(self.history_action)
        self.menu.addAction(self.conversation_info_action)
        self.menu.addSeparator()
        self.menu.addAction(self.connection_action)
        self.menu.addAction(self.disconnect_action)
        self.menu.addAction(self.admin_action)
        self.menu.addAction(self.memory_action)
        self.menu.addAction(self.persona_action)
        self.menu.addAction(self.agent_action)
        self.menu_button.setMenu(self.menu)
        self.new_conversation_action.triggered.connect(self._new_conversation)
        self.history_action.triggered.connect(self.open_history)
        self.conversation_info_action.triggered.connect(self.open_conversation_info)
        self.connection_action.triggered.connect(self.reconnect_requested.emit)
        self.disconnect_action.triggered.connect(self.disconnect_requested.emit)
        self.admin_action.triggered.connect(self.open_console)
        self.memory_action.triggered.connect(self.open_memory)
        self.persona_action.triggered.connect(self.open_persona)
        self.agent_action.triggered.connect(self.open_agent)

        # Kept as hidden compatibility targets until app.py writes status through
        # update_connection_info(). The main surface intentionally remains chat-only.
        self.status = QLabel("오프라인", self)
        self.model = QLabel("모델: 확인 중", self)
        self.status.hide()
        self.model.hide()
        header.addWidget(self.menu_button)
        header.addStretch()
        self.compact_status = QLabel("오프라인")
        self.compact_status.setStyleSheet("font-size: 11px; color: #777;")
        self.compact_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.compact_status)
        layout.addLayout(header)

        self.message_scroll = QScrollArea()
        self.message_scroll.setWidgetResizable(True)
        self.message_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.message_container = QWidget()
        self.message_layout = QVBoxLayout(self.message_container)
        self.message_layout.setContentsMargins(4, 8, 4, 8)
        self.message_layout.setSpacing(6)
        self.message_layout.addStretch(1)
        self.message_scroll.setWidget(self.message_container)
        layout.addWidget(self.message_scroll, 1)

        composer = QWidget()
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(0, 0, 0, 0)
        self.input = QTextEdit()
        self.input.setPlaceholderText("메시지를 입력하세요")
        self.input.setMaximumHeight(100)
        self.send_button = QPushButton("전송")
        self.send_button.setFixedWidth(88)
        self.send_button.setMinimumHeight(42)
        self.send_button.clicked.connect(self._send)
        composer_layout.addWidget(self.input)
        composer_layout.addWidget(self.send_button)
        layout.addWidget(composer)
        self.setCentralWidget(central)

    @property
    def message_bubbles(self) -> tuple[MessageBubble, ...]:
        return tuple(self._message_bubbles)

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.send_requested.emit(text)

    @staticmethod
    def _clean_id(value: object) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    def _add_message(
        self,
        role: str,
        content: str = "",
        *,
        message_id: str | None = None,
        request_id: str | None = None,
    ) -> MessageBubble:
        canonical_id = self._clean_id(message_id)
        if canonical_id is not None:
            existing = self._message_bubbles_by_id.get(canonical_id)
            if existing is not None:
                return existing
        bubble = MessageBubble(
            role,
            content,
            self.message_container,
            message_id=canonical_id,
            request_id=self._clean_id(request_id),
        )
        self._message_bubbles.append(bubble)
        if canonical_id is not None:
            self._message_bubbles_by_id[canonical_id] = bubble
        self.message_layout.insertWidget(self.message_layout.count() - 1, bubble)
        self._scroll_to_bottom()
        return bubble

    def _bind_message_id(self, bubble: MessageBubble, message_id: object) -> MessageBubble:
        canonical_id = self._clean_id(message_id)
        if canonical_id is None or bubble.message_id == canonical_id:
            return bubble
        existing = self._message_bubbles_by_id.get(canonical_id)
        if existing is not None and existing is not bubble:
            return existing
        old_id = bubble.message_id
        if old_id is not None and self._message_bubbles_by_id.get(old_id) is bubble:
            self._message_bubbles_by_id.pop(old_id, None)
        bubble.message_id = canonical_id
        self._message_bubbles_by_id[canonical_id] = bubble
        return bubble

    def append_user_message(
        self,
        text: str,
        *,
        client_message_id: str | None = None,
        request_id: str | None = None,
    ) -> MessageBubble:
        if self._streaming_bubble is not None:
            self.finish_assistant_message()
        local_id = f"client:{client_message_id}" if client_message_id else None
        return self._add_message(
            "user",
            text,
            message_id=local_id,
            request_id=request_id,
        )

    def restore_input(self, text: str) -> None:
        """Restore a message that could not pass the send preflight checks."""

        if not self.input.toPlainText():
            self.input.setPlainText(text)
        self.input.setFocus()

    def bind_turn_message_ids(
        self,
        *,
        request_id: str,
        client_message_id: str,
        user_message_id: object,
        assistant_message_id: object,
    ) -> bool:
        if (
            self._streaming_request_id is not None
            and self._streaming_request_id != request_id
        ):
            return False
        user_bubble = self._message_bubbles_by_id.get(f"client:{client_message_id}")
        if user_bubble is not None:
            self._bind_message_id(user_bubble, user_message_id)
        if self._streaming_bubble is None:
            self.begin_assistant_message(
                request_id=request_id,
                assistant_message_id=self._clean_id(assistant_message_id),
            )
        elif assistant_message_id not in (None, ""):
            bound = self._bind_message_id(self._streaming_bubble, assistant_message_id)
            if bound is not self._streaming_bubble:
                return False
            self._streaming_message_id = str(assistant_message_id)
        return True

    def begin_assistant_message(
        self,
        *,
        request_id: str | None = None,
        assistant_message_id: str | None = None,
    ) -> MessageBubble | None:
        request_key = self._clean_id(request_id)
        message_key = self._clean_id(assistant_message_id)
        if message_key is not None and message_key in self._completed_assistant_message_ids:
            return None
        if self._streaming_bubble is not None:
            if (
                request_key is not None
                and self._streaming_request_id is not None
                and request_key != self._streaming_request_id
            ):
                self.finish_assistant_message()
            else:
                if message_key is not None:
                    bound = self._bind_message_id(self._streaming_bubble, message_key)
                    if bound is not self._streaming_bubble:
                        return None
                    self._streaming_message_id = message_key
                return self._streaming_bubble
        pending_id = message_key or (f"pending:{request_key}" if request_key else None)
        self._streaming_bubble = self._add_message(
            "assistant",
            message_id=pending_id,
            request_id=request_key,
        )
        self._streaming_request_id = request_key
        self._streaming_message_id = message_key
        self._last_delta_sequence = 0
        return self._streaming_bubble

    def append_delta(
        self,
        text: str,
        *,
        request_id: str | None = None,
        assistant_message_id: str | None = None,
        sequence: int | None = None,
    ) -> bool:
        request_key = self._clean_id(request_id)
        message_key = self._clean_id(assistant_message_id)
        if message_key is not None and message_key in self._completed_assistant_message_ids:
            return False
        if (
            request_key is not None
            and self._streaming_request_id is not None
            and request_key != self._streaming_request_id
        ):
            return False
        if (
            message_key is not None
            and self._streaming_message_id is not None
            and message_key != self._streaming_message_id
        ):
            return False
        if sequence is not None and sequence <= self._last_delta_sequence:
            return False
        bubble = self.begin_assistant_message(
            request_id=request_key,
            assistant_message_id=message_key,
        )
        if bubble is None:
            return False
        if sequence is not None:
            self._last_delta_sequence = sequence
        bubble.append_text(text)
        self._scroll_to_bottom()
        return True

    def complete_assistant_message(
        self,
        content: str,
        *,
        request_id: str,
        assistant_message_id: str,
    ) -> bool:
        message_key = self._clean_id(assistant_message_id)
        if message_key is None or message_key in self._completed_assistant_message_ids:
            return False
        if (
            self._streaming_request_id is not None
            and self._streaming_request_id != request_id
        ):
            return False
        if (
            self._streaming_message_id is not None
            and self._streaming_message_id != message_key
        ):
            return False
        bubble = self.begin_assistant_message(
            request_id=request_id,
            assistant_message_id=message_key,
        )
        if bubble is None:
            return False
        if bubble.content != content:
            bubble.replace_text(content)
            self._scroll_to_bottom()
        self._completed_assistant_message_ids.add(message_key)
        self.finish_assistant_message(
            request_id=request_id,
            assistant_message_id=message_key,
        )
        return True

    def finish_assistant_message(
        self,
        *,
        request_id: str | None = None,
        assistant_message_id: str | None = None,
    ) -> bool:
        request_key = self._clean_id(request_id)
        message_key = self._clean_id(assistant_message_id)
        if (
            request_key is not None
            and self._streaming_request_id is not None
            and request_key != self._streaming_request_id
        ):
            return False
        if (
            message_key is not None
            and self._streaming_message_id is not None
            and message_key != self._streaming_message_id
        ):
            return False
        self._streaming_bubble = None
        self._streaming_request_id = None
        self._streaming_message_id = None
        self._last_delta_sequence = 0
        return True

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        self.clear_conversation()
        for message in messages:
            role = "user" if message.get("role") == "user" else "assistant"
            message_id = self._clean_id(message.get("id"))
            bubble = self._add_message(
                role,
                str(message.get("content") or ""),
                message_id=message_id,
            )
            if (
                role == "assistant"
                and message_id is not None
                and str(message.get("state") or "completed") != "generating"
            ):
                self._completed_assistant_message_ids.add(message_id)
            if bubble.message_id is None and message_id is not None:
                self._bind_message_id(bubble, message_id)

    def load_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Restore metadata-only, non-actionable tool cards from conversation history."""

        for value in tool_calls:
            tool_name = str(value.get("tool_name") or "")
            try:
                display_name = TOOL_REGISTRY.require(tool_name).display_name
            except ValueError:
                display_name = tool_name or "도구 호출"
            card = self.show_tool_approval(
                {
                    "tool_call_id": value.get("tool_call_id"),
                    "request_id": value.get("request_id"),
                    "display_name": display_name,
                    "tool_name": tool_name,
                    "action_summary": "이 대화에서 요청된 로컬 도구 호출 기록입니다.",
                    "target_client_id": value.get("target_client_id"),
                    "target_summary": value.get("arguments_summary"),
                    "risk_level": value.get("risk_level"),
                    "approval_modes": [],
                    "read_only": True,
                }
            )
            if card is not None:
                status = str(value.get("status") or "failed")
                summary = str(
                    value.get("result_summary")
                    or value.get("error_code")
                    or status
                )
                card.set_status(status, summary)

    def clear_conversation(self) -> None:
        self._streaming_bubble = None
        self._streaming_request_id = None
        self._streaming_message_id = None
        self._last_delta_sequence = 0
        self._completed_assistant_message_ids.clear()
        self._message_bubbles_by_id.clear()
        self._message_bubbles.clear()
        self._tool_cards_by_id.clear()
        if self.conversation_info_window is not None:
            self.conversation_info_window.clear_response_info()
        while self.message_layout.count() > 1:
            item = self.message_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_generating(self, generating: bool) -> None:
        self.input.setEnabled(not generating)
        self.send_button.setEnabled(not generating)
        self.new_conversation_action.setEnabled(not generating)
        self.connection_action.setEnabled(not generating)
        if not generating:
            self.input.setFocus()

    def show_tool_approval(self, payload: dict[str, Any]) -> ToolApprovalCard | None:
        tool_call_id = str(payload.get("tool_call_id") or "")
        if not tool_call_id:
            return None
        existing = self._tool_cards_by_id.get(tool_call_id)
        if existing is not None:
            return existing
        card = ToolApprovalCard(payload, self.message_container)
        card.decision_requested.connect(self.tool_decision_requested.emit)
        self._tool_cards_by_id[tool_call_id] = card
        self.message_layout.insertWidget(self.message_layout.count() - 1, card)
        self._scroll_to_bottom()
        return card

    def update_tool_status(
        self,
        tool_call_id: str,
        status: str,
        message: str | None = None,
    ) -> bool:
        card = self._tool_cards_by_id.get(tool_call_id)
        if card is None:
            return False
        card.set_status(status, message)
        return True

    def _scroll_to_bottom(self) -> None:
        def scroll() -> None:
            bar = self.message_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

        QTimer.singleShot(0, scroll)

    def _new_conversation(self) -> None:
        self.clear_conversation()
        self.new_conversation_requested.emit()

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, f"{LINK_COMPONENT_NAME} 오류", message)

    def update_connection_info(self, value: dict[str, Any]) -> None:
        """Forward incremental connection status to the read-only info window."""

        self._ensure_conversation_info_window().update_connection_info(value)

    def set_connection_info(self, value: dict[str, Any]) -> None:
        """Replace connection status in the read-only info window."""

        self._ensure_conversation_info_window().set_connection_info(value)

    def set_connection_context(self, value: dict[str, Any]) -> None:
        """Adapt application/network context into the public conversation-info fields."""

        server_status_value = value.get("server_status")
        server_status = server_status_value if isinstance(server_status_value, dict) else {}
        components_value = server_status.get("components")
        components = components_value if isinstance(components_value, dict) else {}
        backend_value = server_status.get("llama_server")
        if not isinstance(backend_value, dict):
            backend_value = components.get("llm")
        backend = backend_value if isinstance(backend_value, dict) else {}
        memory_value = server_status.get("memory_database")
        if not isinstance(memory_value, dict):
            memory_value = components.get("memory_database")
        memory_database = memory_value if isinstance(memory_value, dict) else {}
        embedding_value = server_status.get("embedding_model")
        if not isinstance(embedding_value, dict):
            embedding_value = components.get("embedding")
        embedding_model = embedding_value if isinstance(embedding_value, dict) else {}
        assistant_state = server_status.get("assistant_state")
        llm_state = (
            "generating"
            if assistant_state == "generating"
            else backend.get("state") or assistant_state
        )
        profile_id = value.get("profile_id")
        profile_type = value.get("profile_type")
        profile_parts = [str(part) for part in (profile_id, profile_type) if part not in (None, "")]
        version_value = server_status.get("version")
        version = version_value if isinstance(version_value, dict) else {}
        runtime_value = server_status.get("runtime")
        runtime = runtime_value if isinstance(runtime_value, dict) else {}
        server_version = (
            version.get("app_version")
            or server_status.get("app_version")
            or runtime.get("app_version")
            or server_status.get("version")
        )
        model_name = server_status.get("model_name") or backend.get("loaded_model")
        model_summary = str(model_name) if model_name else f"모델 상태: {llm_state or '미확인'}"
        state = str(value.get("state") or "offline")
        compact_parts = [state]
        if profile_id:
            compact_parts.append(str(profile_id))
        compact_parts.append(model_summary)
        latency = value.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            compact_parts.append(f"{latency:.0f} ms")
        self.compact_status.setText(" · ".join(compact_parts))
        compatibility_warning = value.get("compatibility_warning")
        if compatibility_warning:
            self.compact_status.setText(self.compact_status.text() + " · ⚠ 호환성")
        self.set_connection_info(
            {
                "profile": " · ".join(profile_parts) if profile_parts else None,
                "host": value.get("host"),
                "port": value.get("port"),
                "tls": value.get("tls"),
                "gateway": value.get("state") or server_status.get("gateway"),
                "llm": llm_state,
                "memory_database": self._format_component_status(memory_database),
                "embedding_model": self._format_component_status(embedding_model),
                "client_version": value.get("client_version"),
                "server_version": server_version,
                "protocol_version": version.get("protocol_version")
                or server_status.get("protocol_version")
                or runtime.get("protocol_version"),
                "compatibility_warning": compatibility_warning or "호환됨",
                "build_commit": version.get("build_commit")
                or server_status.get("build_commit")
                or runtime.get("build_commit"),
                "uptime_seconds": server_status.get("uptime_seconds"),
                "last_checked_at": value.get("last_checked_at"),
                "latency_ms": value.get("latency_ms"),
                "consecutive_failures": value.get("consecutive_failures"),
                "reconnect_attempts": value.get("reconnect_attempts"),
            }
        )

    @staticmethod
    def _format_component_status(value: dict[str, Any]) -> str | None:
        state = value.get("state")
        if state in (None, ""):
            return None
        details = [str(state)]
        backend = value.get("backend") or value.get("provider")
        if backend:
            details.append(str(backend))
        if isinstance(value.get("active_count"), int):
            details.append(f"활성 {value['active_count']}개")
        reason = value.get("reason")
        if reason:
            details.append(str(reason))
        return " · ".join(details)

    def set_used_memories(self, values: list[dict[str, Any]]) -> None:
        """Show memories actually used for the current/most recent response."""

        self._ensure_conversation_info_window().set_used_memories(values)

    def set_retrieval_context(self, payload: dict[str, Any]) -> None:
        self._ensure_conversation_info_window().set_retrieval_context(payload)

    def set_generation_metrics(self, value: dict[str, Any]) -> None:
        self._ensure_conversation_info_window().set_generation_metrics(value)

    def set_management_online(self, online: bool) -> None:
        """Apply server availability to all instantiated management windows."""

        self._management_online = online
        self.disconnect_action.setEnabled(online)
        for window in (self.console, self.memory_window, self.persona_window):
            if window is not None:
                window.set_online(online)

    def _ensure_conversation_info_window(self) -> ConversationInfoWindow:
        if self.conversation_info_window is None:
            self.conversation_info_window = ConversationInfoWindow()
        return self.conversation_info_window

    def open_conversation_info(self) -> None:
        window = self._ensure_conversation_info_window()
        window.show()
        window.raise_()
        window.activateWindow()
        self.conversation_info_requested.emit()

    def open_console(self) -> None:
        if self.console is None:
            self.console = ServerConsoleWindow()
        self.console.set_online(self._management_online)
        self.console.show()
        self.console.raise_()
        self.console.activateWindow()
        self.admin_requested.emit()

    def open_memory(self) -> None:
        if self.memory_window is None:
            self.memory_window = MemoryArchiveWindow()
        self.memory_window.set_online(self._management_online)
        self.memory_window.show()
        self.memory_window.raise_()
        self.memory_window.activateWindow()
        self.memory_requested.emit()

    def open_history(self) -> None:
        if self.history_window is None:
            self.history_window = ConversationHistoryWindow()
        self.history_window.show()
        self.history_window.raise_()
        self.history_window.activateWindow()
        self.history_requested.emit()

    def open_persona(self) -> None:
        if self.persona_window is None:
            self.persona_window = PersonaWindow()
        self.persona_window.set_online(self._management_online)
        self.persona_window.show()
        self.persona_window.raise_()
        self.persona_window.activateWindow()
        self.persona_requested.emit()

    def open_agent(self) -> None:
        if self.agent_window is None:
            self.agent_window = AgentManagementWindow()
        self.agent_window.show()
        self.agent_window.raise_()
        self.agent_window.activateWindow()
        self.agent_requested.emit()

    def set_agent_snapshot(self, value: dict[str, Any]) -> None:
        if self.agent_window is None:
            self.agent_window = AgentManagementWindow()
        self.agent_window.set_snapshot(value)

    def closeEvent(self, event: QCloseEvent) -> None:
        for window in (
            self.console,
            self.memory_window,
            self.history_window,
            self.persona_window,
            self.agent_window,
            self.conversation_info_window,
        ):
            if window is not None:
                window.close()
        super().closeEvent(event)

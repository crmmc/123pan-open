from pathlib import Path
import threading
import time
import uuid

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, QItemSelectionModel
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    InfoBar,
    MessageBox,
    PushButton,
    SegmentedWidget,
    TableWidget,
)
from qfluentwidgets import FluentIcon as FIF

from ..common.api import format_file_size
from ..common.database import Database, UPLOAD_PART_SIZE, get_upload_part_size, _safe_int
from ..common.download_metadata import (
    DOWNLOAD_METADATA_VERSION,
    LEGACY_RESUME_TASK_ERROR,
    is_resume_metadata_compatible,
    resolve_download_file_detail,
)
from ..common.download_resume import (
    build_resume_id,
    cleanup_temp_dir,
    stream_download_from_url as _stream_download_from_url,
)
from ..common.log import get_logger
from ..common.speed_tracker import SpeedTracker
from ..common.style_sheet import StyleSheet

logger = get_logger(__name__)

DOWNLOAD_STATUS_FILTERS = [
    "全部", "等待中", "校验中", "下载中", "合并中",
    "已暂停", "已完成", "失败", "已取消",
]
UPLOAD_STATUS_FILTERS = [
    "全部", "等待中", "校验中", "上传中",
    "已暂停", "已完成", "失败", "已取消",
]

COL_NAME = 0
COL_SIZE = 1
COL_PERCENT = 2
COL_SPEED = 3
COL_ETA = 4
COL_STATUS = 5
COL_CONN = 6
COL_ACTION = 7
NUM_COLS = 8
HEADER_LABELS = ["文件名", "大小", "进度", "速度", "剩余时间", "状态", "连接数", "操作"]
UPLOAD_ACTIVE_STATUSES = frozenset({"校验中", "上传中"})
DOWNLOAD_ACTIVE_STATUSES = frozenset({"下载中", "校验中", "合并中"})
RECOVERABLE_DOWNLOAD_STATUSES = frozenset({"校验中", "下载中", "合并中"})
BUTTON_CLICK_HANDLER_ATTR = "_transfer_click_handler"
_SPEED_EXCLUDE_STATUSES = frozenset({"校验中", "合并中"})


def format_speed(bps: float) -> str:
    if bps <= 0:
        return "--"
    if bps >= 1073741824:
        return f"{bps / 1073741824:.1f} GB/s"
    if bps >= 1048576:
        return f"{bps / 1048576:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{int(bps)} B/s"


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "--"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}分{s}秒"
    h, remainder = divmod(int(seconds), 3600)
    m, _ = divmod(remainder, 60)
    return f"{h}时{m}分"


def _normalize_download_version(metadata):
    if not metadata:
        return None
    return {
        "file_size": int(metadata.get("file_size", metadata.get("Size", 0)) or 0),
        "etag": str(metadata.get("etag", metadata.get("Etag", "")) or "").strip().strip('"').lower(),
        "s3key_flag": bool(metadata.get("s3key_flag", metadata.get("S3KeyFlag", False))),
    }


def _download_version_changed(stored_metadata, file_detail):
    stored = _normalize_download_version(stored_metadata)
    current = _normalize_download_version(file_detail)
    if not stored or not current:
        return False
    return any(stored[key] != current[key] for key in ("file_size", "etag", "s3key_flag"))


def _clear_download_resume_state(resume_id):
    db = Database.instance()
    for part in db.get_download_parts(resume_id):
        db.remove_download_part(resume_id, part["part_index"])
    cleanup_temp_dir(resume_id)


class TransferTask:
    """传输任务基类。"""
    def __init__(self, file_name, file_size):
        self.file_name = file_name
        self.file_size = file_size
        self.progress = 0
        self.status = "等待中"
        self.speed_bps = 0.0
        self.eta_seconds = -1.0
        self.speed_tracker = SpeedTracker()
        self.start_time: float = 0.0
        self.finish_duration: float = -1.0
        self.finish_avg_speed: float = 0.0


class UploadTask(TransferTask):
    """上传任务。"""
    def __init__(self, file_name, file_size, local_path, target_dir_id):
        super().__init__(file_name, file_size)
        self.local_path = local_path
        self.target_dir_id = target_dir_id
        self.active_workers = 0
        self.max_workers = 0
        self.thread = None
        self.is_cancelled = False
        self.pause_requested = False
        self.delete_requested = False
        self.db_task_id = None
        self.last_error = ""
        # S3 session 字段（断点续传用）
        self.bucket = ""
        self.storage_node = ""
        self.upload_key = ""
        self.upload_id_s3 = ""
        self.up_file_id = 0
        self.total_parts = 0
        self.block_size = get_upload_part_size()
        self.etag = ""
        self.file_mtime = 0.0


class DownloadTask(TransferTask):
    """下载任务。"""
    def __init__(
        self, file_name, file_size, file_id, save_path,
        current_dir_id=0, file_type=0, etag="", s3key_flag=False,
        account_name="", resume_id=None, last_error="",
        metadata_version=DOWNLOAD_METADATA_VERSION,
        resume_metadata_valid=True,
    ):
        super().__init__(file_name, file_size)
        self.file_id = file_id
        self.save_path = save_path
        self.current_dir_id = current_dir_id
        self.file_type = file_type
        self.etag = etag or ""
        self.s3key_flag = bool(s3key_flag)
        self.account_name = account_name or ""
        self.resume_id = resume_id or build_resume_id(
            self.account_name, self.file_id, self.save_path,
        )
        self.last_error = last_error
        self.metadata_version = metadata_version
        self.resume_metadata_valid = resume_metadata_valid
        self.supports_resume = True
        self.active_workers = 0
        self.max_workers = 0
        self.thread = None
        self.is_cancelled = False
        self.pause_requested = False
        self.cleanup_on_cancel = False
        self.delete_requested = False
        self._active_response = None
        self._response_lock = threading.Lock()  # P0-2: 保护 _active_response 跨线程访问
        self._last_db_progress = -1
        self._last_db_progress_time = 0.0


class UploadThread(QThread):
    """上传线程，支持暂停、取消与断点续传。"""
    progress_updated = Signal(int)
    status_updated = Signal(str)
    finished = Signal()
    error = Signal(str)
    conn_info_updated = Signal(int, int)
    session_info = Signal(dict)
    part_done = Signal(int, str)

    def __init__(self, task, pan):
        super().__init__()
        self.task = task
        self.pan = pan

    def pause(self):
        self.task.pause_requested = True

    def cancel(self):
        self.task.is_cancelled = True
        self.task.pause_requested = False

    def _build_resume_info(self):
        t = self.task
        if not (t.bucket and t.upload_key and t.upload_id_s3):
            return None
        db = Database.instance()
        done_parts = {r["part_index"] for r in db.get_upload_parts(t.db_task_id)}
        return {
            "bucket": t.bucket,
            "storage_node": t.storage_node,
            "upload_key": t.upload_key,
            "upload_id": t.upload_id_s3,
            "up_file_id": t.up_file_id,
            "total_parts": t.total_parts,
            "block_size": t.block_size,
            "etag": t.etag,
            "file_mtime": t.file_mtime,
            "file_size": t.file_size,
            "done_parts": done_parts,
        }

    def run(self):
        try:
            self.status_updated.emit("上传中")

            class _SignalsAdapter:
                class _SignalProxy:
                    def __init__(self, signal):
                        self.emit = signal.emit

                def __init__(self, thread):
                    self.progress = self._SignalProxy(thread.progress_updated)
                    self.conn_info = self._SignalProxy(thread.conn_info_updated)
                    self.status = self._SignalProxy(thread.status_updated)
                    self.session_info = self._SignalProxy(thread.session_info)
                    self.part_done = self._SignalProxy(thread.part_done)

            resume_info = self._build_resume_info()
            result = self.pan.upload_file_stream(
                self.task.local_path,
                parent_id=self.task.target_dir_id,
                signals=_SignalsAdapter(self),
                task=self.task,
                speed_tracker=self.task.speed_tracker,
                resume_info=resume_info,
                file_name_override=self.task.file_name,
            )
            # 统一提交所有分片记录（配合 record_upload_part(commit=False)）
            try:
                Database.instance().flush()
            except Exception:
                pass
            if result == "已取消":
                self.status_updated.emit("已取消")
                return
            if result == "已暂停":
                self.status_updated.emit("已暂停")
                return
            self.progress_updated.emit(100)
            self.status_updated.emit("已完成")
        except Exception as exc:
            logger.error("上传错误: %s", exc, exc_info=True)
            self.error.emit(str(exc))
            self.status_updated.emit("失败")


class DownloadThread(QThread):
    """下载线程，支持暂停、取消与恢复。"""
    progress_updated = Signal(int)
    status_updated = Signal(str)
    finished = Signal()
    error = Signal(str)
    conn_info_updated = Signal(int, int)

    def __init__(self, task, pan):
        super().__init__()
        self.task = task
        self.pan = pan

    def pause(self):
        self.task.pause_requested = True

    def cancel(self):
        self.task.is_cancelled = True
        self.task.pause_requested = False
        # P0-2: 通过锁安全访问 _active_response
        with self.task._response_lock:
            active_response = self.task._active_response
        if active_response is not None:
            try:
                active_response.close()
            except Exception:
                logger.warning("主动关闭下载响应失败", exc_info=True)

    def _resolve_download_detail(self):
        db = Database.instance()
        stored_task = db.get_download_task(self.task.resume_id)
        file_detail = resolve_download_file_detail(
            self.pan, self.task.file_id,
            current_dir_id=self.task.current_dir_id,
        )
        if _download_version_changed(stored_task, file_detail):
            logger.info("检测到远端文件版本变化，清理旧续传分片: %s", self.task.resume_id)
            _clear_download_resume_state(self.task.resume_id)
            self.task.progress = 0
        self.task.file_type = int(file_detail.get("Type", 0) or 0)
        self.task.file_size = int(file_detail.get("Size", 0) or 0)
        self.task.etag = file_detail.get("Etag", "") or ""
        self.task.s3key_flag = bool(file_detail.get("S3KeyFlag", False))
        db.update_download_task(
            self.task.resume_id,
            file_size=self.task.file_size,
            file_type=self.task.file_type,
            etag=self.task.etag,
            s3key_flag=int(self.task.s3key_flag),
            progress=self.task.progress,
        )
        return file_detail

    def run(self):
        try:
            file_detail = self._resolve_download_detail()
            download_url = self.pan.link_by_fileDetail(file_detail, showlink=False)
            if isinstance(download_url, int):
                raise RuntimeError(f"获取下载链接失败，返回码: {download_url}")

            class _SignalsAdapter:
                class _SignalProxy:
                    def __init__(self, signal):
                        self.emit = signal.emit

                def __init__(self, thread):
                    self.progress = self._SignalProxy(thread.progress_updated)
                    self.conn_info = self._SignalProxy(thread.conn_info_updated)
                    self.status = self._SignalProxy(thread.status_updated)

            def _refresh_url():
                new_url = self.pan.link_by_fileDetail(file_detail, showlink=False)
                if isinstance(new_url, int):
                    return None
                return new_url

            result = _stream_download_from_url(
                download_url, Path(self.task.save_path),
                signals=_SignalsAdapter(self), task=self.task,
                overwrite=True, resume_task=self.task,
                speed_tracker=self.task.speed_tracker,
                refresh_url_fn=_refresh_url,
            )
            if result == "已取消":
                self.status_updated.emit("已取消")
                return
            if result == "已暂停":
                self.status_updated.emit("已暂停")
                return
            self.progress_updated.emit(100)
            self.status_updated.emit("已完成")
        except Exception as exc:
            logger.error("下载错误: %s", exc)
            self.error.emit(str(exc))
            self.status_updated.emit("失败")


class TransferInterface(QWidget):
    """传输页面。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("TransferInterface")
        self.mainLayout = QVBoxLayout(self)
        self.mainLayout.setContentsMargins(24, 20, 24, 24)
        self.mainLayout.setSpacing(12)
        self.upload_tasks = []
        self.download_tasks = []
        self.upload_threads = []
        self.download_threads = []
        self.pan = None
        self.current_account_name = ""
        self.download_status_filter = "全部"
        self.upload_status_filter = "全部"
        self._auto_start_suppressed = False
        self.__createTopBar()
        self.__createContent()
        self.__initWidget()

    def set_pan(self, pan, force=False):
        self.pan = pan
        account_name = getattr(pan, "user_name", "") if pan else ""
        if not force and account_name == self.current_account_name:
            return
        self.current_account_name = account_name
        self.__reload_download_tasks()
        self.__reload_upload_tasks()

    def suspend_auto_start(self):
        self._auto_start_suppressed = True

    def resume_auto_start(self):
        self._auto_start_suppressed = False

    def __createTopBar(self):
        self.topBarFrame = QFrame(self)
        self.topBarFrame.setObjectName("frame")
        self.topBarLayout = QHBoxLayout(self.topBarFrame)
        self.topBarLayout.setContentsMargins(12, 10, 12, 10)
        self.topBarLayout.setSpacing(8)
        self.titleLabel = QLabel("传输管理", self.topBarFrame)
        self.segmentedWidget = SegmentedWidget(self.topBarFrame)
        self.segmentedWidget.addItem(routeKey="upload", icon=FIF.UP.icon(), text="上传")
        self.segmentedWidget.addItem(routeKey="download", icon=FIF.DOWNLOAD.icon(), text="下载")
        self.segmentedWidget.setCurrentItem("upload")
        self.topBarLayout.addWidget(self.titleLabel)
        self.topBarLayout.addWidget(self.segmentedWidget)
        self.topBarLayout.addStretch(1)

        self.downloadFilterLabel = QLabel("状态", self.topBarFrame)
        self.downloadFilterCombo = ComboBox(self.topBarFrame)
        self.downloadFilterCombo.addItems(DOWNLOAD_STATUS_FILTERS)
        self.downloadFilterCombo.setCurrentText(self.download_status_filter)
        self.downloadFilterCombo.setMinimumWidth(120)
        self.downloadFilterLabel.hide()
        self.downloadFilterCombo.hide()
        self.uploadFilterLabel = QLabel("状态", self.topBarFrame)
        self.uploadFilterCombo = ComboBox(self.topBarFrame)
        self.uploadFilterCombo.addItems(UPLOAD_STATUS_FILTERS)
        self.uploadFilterCombo.setCurrentText(self.upload_status_filter)
        self.uploadFilterCombo.setMinimumWidth(120)
        self.openDownloadFolderButton = PushButton(FIF.FOLDER.icon(), "打开下载文件夹", self.topBarFrame)
        self.openDownloadFolderButton.hide()
        self.topBarLayout.addWidget(self.uploadFilterLabel)
        self.topBarLayout.addWidget(self.uploadFilterCombo)
        self.topBarLayout.addWidget(self.openDownloadFolderButton)
        self.topBarLayout.addWidget(self.downloadFilterLabel)
        self.topBarLayout.addWidget(self.downloadFilterCombo)
        self.mainLayout.addWidget(self.topBarFrame)

    def __createContent(self):
        self.uploadFrame = QFrame(self)
        self.uploadFrame.setObjectName("frame")
        self.uploadLayout = QVBoxLayout(self.uploadFrame)
        self.uploadLayout.setContentsMargins(0, 8, 0, 0)
        self.uploadTable = TableWidget(self.uploadFrame)
        self.uploadTable.setAlternatingRowColors(True)
        self.uploadTable.setColumnCount(NUM_COLS)
        self.uploadTable.setHorizontalHeaderLabels(HEADER_LABELS)
        self.uploadTable.setBorderRadius(8)
        self.uploadTable.setBorderVisible(True)
        self.uploadTable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.uploadTable.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.uploadTable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        uh = self.uploadTable.horizontalHeader()
        if uh:
            self.__setup_transfer_header(self.uploadTable)
        self.uploadBatchBar, self._upload_batch_btns = self.__create_batch_toolbar(self.uploadFrame)
        self.uploadLayout.addWidget(self.uploadBatchBar)
        self.uploadLayout.addWidget(self.uploadTable)

        self.downloadFrame = QFrame(self)
        self.downloadFrame.setObjectName("frame")
        self.downloadLayout = QVBoxLayout(self.downloadFrame)
        self.downloadLayout.setContentsMargins(0, 8, 0, 0)
        self.downloadTable = TableWidget(self.downloadFrame)
        self.downloadTable.setAlternatingRowColors(True)
        self.downloadTable.setColumnCount(NUM_COLS)
        self.downloadTable.setHorizontalHeaderLabels(HEADER_LABELS)
        self.downloadTable.setBorderRadius(8)
        self.downloadTable.setBorderVisible(True)
        self.downloadTable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.downloadTable.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.downloadTable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        dh = self.downloadTable.horizontalHeader()
        if dh:
            self.__setup_transfer_header(self.downloadTable)
        self.downloadBatchBar, self._download_batch_btns = self.__create_batch_toolbar(self.downloadFrame)
        self.downloadLayout.addWidget(self.downloadBatchBar)
        self.downloadLayout.addWidget(self.downloadTable)
        self.downloadFrame.hide()
        self.mainLayout.addWidget(self.uploadFrame)
        self.mainLayout.addWidget(self.downloadFrame)

    def __setup_transfer_header(self, table):
        """设置传输表格 header：名称列 Stretch，其余列自适应内容"""
        h = table.horizontalHeader()
        if not h:
            return
        h.setSectionResizeMode(COL_NAME, h.ResizeMode.Stretch)
        for c in range(1, NUM_COLS):
            h.setSectionResizeMode(c, h.ResizeMode.ResizeToContents)

    def __create_batch_toolbar(self, parent):
        """创建批量操作工具栏，返回 (frame, buttons_dict)。"""
        frame = QFrame(parent)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(6)

        btn_select_all = PushButton(FIF.CHECKBOX.icon(), "全选", frame)
        btn_invert = PushButton(FIF.SYNC.icon(), "反选", frame)
        btn_pause = PushButton(FIF.PAUSE.icon(), "暂停", frame)
        btn_resume = PushButton(FIF.SYNC.icon(), "继续", frame)
        btn_delete = PushButton(FIF.DELETE.icon(), "删除", frame)
        for b in (btn_select_all, btn_invert, btn_pause, btn_resume, btn_delete):
            b.setFixedHeight(28)
            layout.addWidget(b)
        layout.addStretch()
        select_count = QLabel("已选 0 项", frame)
        layout.addWidget(select_count)
        layout.addSpacing(16)
        speed_label = QLabel("总速度: --", frame)
        layout.addWidget(speed_label)

        return frame, {
            'select_all': btn_select_all,
            'invert': btn_invert,
            'pause': btn_pause,
            'resume': btn_resume,
            'delete': btn_delete,
            'count': select_count,
            'speed': speed_label,
        }

    def __initWidget(self):
        StyleSheet.VIEW_INTERFACE.apply(self)
        self.segmentedWidget.currentItemChanged.connect(self.__onSegmentChanged)
        self.uploadFilterCombo.currentTextChanged.connect(self.__onUploadFilterChanged)
        self.downloadFilterCombo.currentTextChanged.connect(self.__onDownloadFilterChanged)
        self.openDownloadFolderButton.clicked.connect(self.__open_download_folder)
        # 上传批量操作信号
        ub = self._upload_batch_btns
        ub['select_all'].clicked.connect(lambda: self.__select_all(self.uploadTable, self.__get_filtered_upload_tasks()))
        ub['invert'].clicked.connect(lambda: self.__invert_selection(self.uploadTable, self.__get_filtered_upload_tasks()))
        ub['pause'].clicked.connect(lambda: self.__batch_pause(self.uploadTable, self.__get_filtered_upload_tasks(), "upload"))
        ub['resume'].clicked.connect(lambda: self.__batch_resume(self.uploadTable, self.__get_filtered_upload_tasks(), "upload"))
        ub['delete'].clicked.connect(lambda: self.__batch_delete(self.uploadTable, self.__get_filtered_upload_tasks(), "upload"))
        # 下载批量操作信号
        db = self._download_batch_btns
        db['select_all'].clicked.connect(lambda: self.__select_all(self.downloadTable, self.__get_filtered_download_tasks()))
        db['invert'].clicked.connect(lambda: self.__invert_selection(self.downloadTable, self.__get_filtered_download_tasks()))
        db['pause'].clicked.connect(lambda: self.__batch_pause(self.downloadTable, self.__get_filtered_download_tasks(), "download"))
        db['resume'].clicked.connect(lambda: self.__batch_resume(self.downloadTable, self.__get_filtered_download_tasks(), "download"))
        db['delete'].clicked.connect(lambda: self.__batch_delete(self.downloadTable, self.__get_filtered_download_tasks(), "download"))
        # 选中变化 → 更新计数
        self.uploadTable.itemSelectionChanged.connect(lambda: self.__update_batch_bar(self._upload_batch_btns, self.uploadTable))
        self.downloadTable.itemSelectionChanged.connect(lambda: self.__update_batch_bar(self._download_batch_btns, self.downloadTable))

    def __onSegmentChanged(self, route_key):
        is_upload = route_key == "upload"
        self.uploadFrame.setVisible(is_upload)
        self.downloadFrame.setVisible(not is_upload)
        self.uploadFilterLabel.setVisible(is_upload)
        self.uploadFilterCombo.setVisible(is_upload)
        self.downloadFilterLabel.setVisible(not is_upload)
        self.downloadFilterCombo.setVisible(not is_upload)
        self.openDownloadFolderButton.setVisible(not is_upload)

    def __onDownloadFilterChanged(self, status):
        self.download_status_filter = status
        self.__update_download_table()

    def __onUploadFilterChanged(self, status):
        self.upload_status_filter = status
        self.__update_upload_table()

    def __get_filtered_download_tasks(self):
        if self.download_status_filter == "全部":
            return self.download_tasks
        return [t for t in self.download_tasks if t.status == self.download_status_filter]

    def __get_filtered_upload_tasks(self):
        if self.upload_status_filter == "全部":
            return self.upload_tasks
        return [t for t in self.upload_tasks if t.status == self.upload_status_filter]

    def __resolve_download_folder(self):
        row = self.downloadTable.currentRow()
        if row >= 0:
            visible = self.__get_filtered_download_tasks()
            if row < len(visible) and visible[row].save_path:
                return str(Path(visible[row].save_path).parent)
        return Database.instance().get_config("defaultDownloadPath", str(Path.home() / "Downloads"))

    def __open_download_folder(self):
        folder = Path(self.__resolve_download_folder())
        if not folder.exists():
            InfoBar.error(title="打开失败", content=f"下载文件夹不存在: {folder}", parent=self)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- reload ----

    def __reload_download_tasks(self):
        self.download_tasks = []
        db = Database.instance()
        for record in db.get_download_tasks(account_name=self.current_account_name):
            if record.get("status") in ("已完成", "已取消"):
                db.delete_download_task(record["resume_id"])
                continue
            resume_metadata_valid = is_resume_metadata_compatible(record)
            last_error = record.get("error", "")
            status = record.get("status", "失败")
            if not resume_metadata_valid:
                last_error = LEGACY_RESUME_TASK_ERROR
                status = "失败"
            task = DownloadTask(
                file_name=record.get("file_name", ""),
                file_size=int(record.get("file_size", 0) or 0),
                file_id=record.get("file_id", 0),
                save_path=record.get("save_path", ""),
                current_dir_id=int(record.get("current_dir_id", 0) or 0),
                file_type=int(record.get("file_type", 0) or 0),
                etag=record.get("etag", ""),
                s3key_flag=record.get("s3key_flag", False),
                account_name=record.get("account_name", self.current_account_name),
                resume_id=record.get("resume_id"),
                last_error=last_error,
                metadata_version=record.get("metadata_version"),
                resume_metadata_valid=resume_metadata_valid,
            )
            task.progress = int(record.get("progress", 0) or 0)
            task.status = status
            task.supports_resume = bool(record.get("supports_resume", 0))
            if task.status in RECOVERABLE_DOWNLOAD_STATUSES:
                task.status = "失败"
                task.last_error = task.last_error or "下载中断，等待重试"
            if task.last_error:
                db.update_download_task(task.resume_id, status=task.status, error=task.last_error)
            self.download_tasks.append(task)
        self.__update_download_table()

    def __reload_upload_tasks(self):
        self.upload_tasks = []
        db = Database.instance()
        for record in db.get_upload_tasks(account_name=self.current_account_name):
            if record["status"] in ("已完成", "已取消") or record.get("delete_requested", 0):
                db.delete_upload_task(record["task_id"])
                continue
            task = UploadTask(
                file_name=record["file_name"],
                file_size=record.get("file_size", 0),
                local_path=record["local_path"],
                target_dir_id=record.get("target_dir_id", 0),
            )
            task.db_task_id = record["task_id"]
            task.progress = record.get("progress", 0)
            task.status = record.get("status", "失败")
            task.last_error = record.get("error", "")
            if task.status in UPLOAD_ACTIVE_STATUSES:
                task.status = "已暂停"
                if task.db_task_id:
                    db.update_upload_task(task.db_task_id, status="已暂停")
            # 恢复 S3 session 字段
            task.bucket = record.get("bucket", "")
            task.storage_node = record.get("storage_node", "")
            task.upload_key = record.get("upload_key", "")
            task.upload_id_s3 = record.get("upload_id_s3", "")
            task.up_file_id = record.get("up_file_id", 0)
            task.total_parts = record.get("total_parts", 0)
            task.block_size = record.get("block_size", UPLOAD_PART_SIZE)
            task.etag = record.get("etag", "")
            task.file_mtime = record.get("file_mtime", 0)
            self.upload_tasks.append(task)
        self.__update_upload_table()

    # ---- concurrency control ----

    @staticmethod
    def __upload_occupies_slot(task):
        return getattr(task, "thread", None) is not None or task.status in UPLOAD_ACTIVE_STATUSES

    def __active_upload_count(self):
        return sum(1 for t in self.upload_tasks if self.__upload_occupies_slot(t))

    def __active_download_count(self):
        return sum(1 for t in self.download_tasks
                   if getattr(t, "thread", None) is not None or t.status in DOWNLOAD_ACTIVE_STATUSES)

    @staticmethod
    def __download_supports_resume(task):
        if hasattr(task, "supports_resume"):
            return bool(task.supports_resume)
        stored = Database.instance().get_download_task(task.resume_id)
        return bool(stored and stored.get("supports_resume", 0))

    def __max_concurrent_uploads(self):
        return max(1, min(5, _safe_int(Database.instance().get_config("maxConcurrentUploads", 3))))

    def __max_concurrent_downloads(self):
        return max(1, min(5, _safe_int(Database.instance().get_config("maxConcurrentDownloads", 5))))

    def __try_start_pending_uploads(self):
        if getattr(self, '_batch_rebuild_suppressed', False):
            return
        if getattr(self, "_auto_start_suppressed", False):
            return
        limit = self.__max_concurrent_uploads()
        for task in self.upload_tasks:
            if self.__active_upload_count() >= limit:
                break
            if task.delete_requested:
                continue
            if task.status == "等待中" and task.thread is None:
                self.__start_upload_task(task)

    def __try_start_pending_downloads(self):
        if getattr(self, '_batch_rebuild_suppressed', False):
            return
        if getattr(self, "_auto_start_suppressed", False):
            return
        limit = self.__max_concurrent_downloads()
        for task in self.download_tasks:
            if self.__active_download_count() >= limit:
                break
            if task.status == "等待中" and task.thread is None:
                self.__start_download_task(task)

    # ---- add tasks ----

    def add_upload_task(self, file_name, file_size, local_path, target_dir_id):
        # 去重检查：同一文件 + 同目标目录不重复添加
        for t in self.upload_tasks:
            if t.local_path == local_path and t.target_dir_id == target_dir_id and t.status not in ("已完成", "已取消"):
                return t
        task = UploadTask(file_name, file_size, local_path, target_dir_id)
        task.db_task_id = uuid.uuid4().hex
        db = Database.instance()
        db.save_upload_task({
            "task_id": task.db_task_id,
            "account_name": self.current_account_name,
            "file_name": file_name,
            "file_size": file_size,
            "local_path": local_path,
            "target_dir_id": target_dir_id,
            "status": "等待中",
            "delete_requested": 0,
        })
        self.upload_tasks.append(task)
        self.__update_upload_table()
        if self.pan:
            self.__try_start_pending_uploads()
        return task

    def __start_upload_task(self, task):
        if not self.pan or task.thread is not None or task.delete_requested:
            return
        task.is_cancelled = False
        task.pause_requested = False
        task.delete_requested = False
        task.last_error = ""
        task.active_workers = 0
        task.max_workers = 0
        task._finish_handled = False
        task.finish_duration = -1.0
        task.finish_avg_speed = 0.0
        task.start_time = time.monotonic()
        task.speed_tracker.reset()
        thread = UploadThread(task, self.pan)
        task.thread = thread
        thread.progress_updated.connect(lambda p, t=task: self.__update_task_progress(t, p))
        thread.status_updated.connect(lambda s, t=task: self.__update_task_status(t, s))
        thread.conn_info_updated.connect(lambda a, m, t=task: self.__update_task_conn_info(t, a, m))
        thread.session_info.connect(lambda info, t=task: self.__on_upload_session_info(t, info))
        thread.part_done.connect(lambda pn, etag, t=task: self.__on_upload_part_done(t, pn, etag))
        thread.finished.connect(lambda t=task: self.__task_finished(t, "upload"))
        thread.error.connect(lambda e, t=task: self.__task_error(t, e))
        self.upload_threads.append(thread)
        thread.start()
        self._ensure_speed_timer()

    def add_download_task(
        self, file_name, file_size, file_id, save_path,
        current_dir_id=0, file_type=0, etag="", s3key_flag=False,
    ):
        account_name = self.current_account_name
        resume_id = build_resume_id(account_name, file_id, save_path)
        existing = next((t for t in self.download_tasks if t.resume_id == resume_id and t.status not in ("已完成", "已取消")), None)
        if existing:
            return existing
        task = DownloadTask(
            file_name=file_name, file_size=file_size, file_id=file_id,
            save_path=save_path, current_dir_id=current_dir_id,
            file_type=file_type, etag=etag, s3key_flag=s3key_flag,
            account_name=account_name, resume_id=resume_id,
            metadata_version=DOWNLOAD_METADATA_VERSION,
        )
        self.download_tasks.append(task)
        db = Database.instance()
        db.save_download_task({
            "resume_id": resume_id, "account_name": account_name,
            "file_name": file_name, "file_id": file_id,
            "file_type": file_type, "file_size": file_size,
            "save_path": save_path, "current_dir_id": current_dir_id,
            "etag": etag, "s3key_flag": int(bool(s3key_flag)),
            "status": "等待中", "metadata_version": DOWNLOAD_METADATA_VERSION,
        })
        self.__update_download_table()
        self.__try_start_pending_downloads()
        return task

    def __start_download_task(self, task):
        if not self.pan or task.thread is not None:
            return
        if not task.resume_metadata_valid:
            self.__mark_download_failed(task, LEGACY_RESUME_TASK_ERROR, notify=True)
            return
        task.is_cancelled = False
        task.pause_requested = False
        task.cleanup_on_cancel = False
        task.delete_requested = False
        task.last_error = ""
        task._finish_handled = False
        task.finish_duration = -1.0
        task.finish_avg_speed = 0.0
        task.start_time = time.monotonic()
        task.speed_tracker.reset()
        thread = DownloadThread(task, self.pan)
        task.thread = thread
        thread.progress_updated.connect(lambda p, t=task: self.__update_task_progress(t, p))
        thread.status_updated.connect(lambda s, t=task: self.__update_task_status(t, s))
        thread.conn_info_updated.connect(lambda a, m, t=task: self.__update_task_conn_info(t, a, m))
        thread.finished.connect(lambda t=task: self.__task_finished(t, "download"))
        thread.error.connect(lambda e, t=task: self.__task_error(t, e))
        self.download_threads.append(thread)
        thread.start()
        self._ensure_speed_timer()

    # ---- progress/status/speed slots ----

    def __update_task_progress(self, task, progress):
        task.progress = progress
        if isinstance(task, DownloadTask):
            now = time.time()
            last_p = getattr(task, '_last_db_progress', -1)
            last_t = getattr(task, '_last_db_progress_time', 0.0)
            if abs(progress - last_p) >= 1 or (now - last_t) > 2.0:
                Database.instance().update_download_task(task.resume_id, progress=progress)
                task._last_db_progress = progress
                task._last_db_progress_time = now
        elif isinstance(task, UploadTask) and task.db_task_id:
            Database.instance().update_upload_task(task.db_task_id, progress=progress)
        self.__partial_refresh(task)

    def __update_task_status(self, task, status):
        task.status = status
        terminal = status in {"失败", "已完成", "已取消", "已暂停"}
        if terminal:
            # H7: 从线程列表中移除，防止内存泄漏
            if task.thread:
                try:
                    task.thread.disconnect()
                except TypeError:
                    pass
                task.thread.deleteLater()
                if isinstance(task, UploadTask) and task.thread in self.upload_threads:
                    self.upload_threads.remove(task.thread)
                elif isinstance(task, DownloadTask) and task.thread in self.download_threads:
                    self.download_threads.remove(task.thread)
            task.thread = None
            task.active_workers = 0
            task.max_workers = 0
            task.speed_bps = 0.0
            task.eta_seconds = -1.0
        if isinstance(task, DownloadTask):
            task.supports_resume = self.__download_supports_resume(task)
            if (
                not task.supports_resume
                and status in {"已暂停", "等待中", "失败"}
                and not task.delete_requested
            ):
                task.progress = 0
            if task.delete_requested and terminal:
                Database.instance().delete_download_task(task.resume_id)
                cleanup_temp_dir(task.resume_id)
                if task in self.download_tasks:
                    self.download_tasks.remove(task)
            elif status != "已完成":
                Database.instance().update_download_task(
                    task.resume_id, status=status,
                    error=task.last_error, progress=task.progress,
                )
        elif isinstance(task, UploadTask) and task.db_task_id:
            if task.delete_requested and terminal:
                Database.instance().delete_upload_task(task.db_task_id)
                task.db_task_id = None
                if task in self.upload_tasks:
                    self.upload_tasks.remove(task)
            else:
                Database.instance().update_upload_task(
                    task.db_task_id,
                    status=status,
                    progress=task.progress,
                    error=task.last_error,
                    delete_requested=int(task.delete_requested),
                )
        if isinstance(task, UploadTask):
            self.__update_upload_table()
        else:
            self.__update_download_table()
        if terminal:
            # 直接内联 __task_finished 逻辑，避免依赖 finished 信号
            # （disconnect 可能吞掉已排队的 finished 信号）
            self.__task_finished(task, "upload" if isinstance(task, UploadTask) else "download")
            if isinstance(task, UploadTask):
                self.__try_start_pending_uploads()
            else:
                self.__try_start_pending_downloads()

    def __update_task_conn_info(self, task, active, max_w):
        task.active_workers = active
        task.max_workers = max_w
        self.__partial_refresh(task)

    def _ensure_speed_timer(self):
        if not hasattr(self, "_speed_timer"):
            self._speed_timer = QTimer(self)
            self._speed_timer.setInterval(1000)
            self._speed_timer.timeout.connect(self.__tick_speed)
        if not self._speed_timer.isActive():
            self._speed_timer.start()

    def __tick_speed(self):
        has_active = False
        upload_dirty = []
        download_dirty = []

        for task in self.upload_tasks:
            if task.status in UPLOAD_ACTIVE_STATUSES:
                has_active = True
                task.speed_tracker.flush()
                remaining = task.file_size - int(task.file_size * task.progress / 100) if task.file_size else 0
                task.speed_bps = task.speed_tracker.speed()
                task.eta_seconds = task.speed_tracker.eta(remaining)
                upload_dirty.append(task)
            elif task.speed_bps != 0.0 and task.status != "已完成":
                task.speed_bps = 0.0
                task.eta_seconds = -1.0
                upload_dirty.append(task)

        for task in self.download_tasks:
            if task.status in DOWNLOAD_ACTIVE_STATUSES:
                has_active = True
                task.speed_tracker.flush()
                remaining = task.file_size - int(task.file_size * task.progress / 100) if task.file_size else 0
                task.speed_bps = task.speed_tracker.speed()
                task.eta_seconds = task.speed_tracker.eta(remaining)
                download_dirty.append(task)
            elif task.speed_bps != 0.0 and task.status != "已完成":
                task.speed_bps = 0.0
                task.eta_seconds = -1.0
                download_dirty.append(task)

        if upload_dirty:
            visible = self.__get_filtered_upload_tasks()
            for task in upload_dirty:
                row = self.__find_task_row(task, visible)
                self.__refresh_task_cells(self.uploadTable, row, task)
            total = sum(t.speed_bps for t in self.upload_tasks
                        if t.speed_bps > 0 and t.status not in _SPEED_EXCLUDE_STATUSES)
            self._upload_batch_btns['speed'].setText(f"总速度: {format_speed(total)}")
        if download_dirty:
            visible = self.__get_filtered_download_tasks()
            for task in download_dirty:
                row = self.__find_task_row(task, visible)
                self.__refresh_task_cells(self.downloadTable, row, task)
            total = sum(t.speed_bps for t in self.download_tasks
                        if t.speed_bps > 0 and t.status not in _SPEED_EXCLUDE_STATUSES)
            self._download_batch_btns['speed'].setText(f"总速度: {format_speed(total)}")

        if not has_active:
            self._speed_timer.stop()

    def __on_upload_session_info(self, task, info):
        task.bucket = info.get("bucket", "")
        task.storage_node = info.get("storage_node", "")
        task.upload_key = info.get("upload_key", "")
        task.upload_id_s3 = info.get("upload_id", "")
        task.up_file_id = info.get("up_file_id", 0)
        task.total_parts = info.get("total_parts", 0)
        task.block_size = info.get("block_size", UPLOAD_PART_SIZE)
        task.etag = info.get("etag", "")
        task.file_mtime = info.get("file_mtime", 0)
        if task.db_task_id:
            Database.instance().update_upload_task(
                task.db_task_id,
                bucket=task.bucket, storage_node=task.storage_node,
                upload_key=task.upload_key, upload_id_s3=task.upload_id_s3,
                up_file_id=task.up_file_id, total_parts=task.total_parts,
                block_size=task.block_size, etag=task.etag,
                file_mtime=task.file_mtime,
            )

    def __on_upload_part_done(self, task, part_index, etag):
        if task.db_task_id:
            Database.instance().record_upload_part(task.db_task_id, part_index, etag, commit=False)

    def __reset_upload_session(self, task, *, clear_progress):
        task.bucket = ""
        task.storage_node = ""
        task.upload_key = ""
        task.upload_id_s3 = ""
        task.up_file_id = 0
        task.total_parts = 0
        task.block_size = get_upload_part_size()
        task.etag = ""
        if clear_progress:
            task.progress = 0
        if task.db_task_id:
            db = Database.instance()
            db.delete_upload_parts(task.db_task_id)
            db.update_upload_task(
                task.db_task_id,
                bucket="",
                storage_node="",
                upload_key="",
                upload_id_s3="",
                up_file_id=0,
                total_parts=0,
                block_size=task.block_size,
                etag="",
                progress=task.progress,
                delete_requested=int(task.delete_requested),
            )

    def __partial_refresh(self, task):
        """局部刷新单个 task 的 speed/ETA/conn/percent 列。"""
        if isinstance(task, UploadTask):
            visible = self.__get_filtered_upload_tasks()
            table = self.uploadTable
        else:
            visible = self.__get_filtered_download_tasks()
            table = self.downloadTable
        row = self.__find_task_row(task, visible)
        self.__refresh_task_cells(table, row, task)

    def __task_finished(self, task, task_type):
        # 防重入：status_updated 和 QThread.finished 都会触发此方法
        if getattr(task, "_finish_handled", False):
            return
        task._finish_handled = True

        # 计算耗时和平均速度
        if task.start_time > 0:
            task.finish_duration = time.monotonic() - task.start_time
            if task.finish_duration > 0 and task.file_size > 0:
                task.finish_avg_speed = task.file_size / task.finish_duration

        if task_type == "upload":
            if task.delete_requested:
                return
            if task.status == "失败":
                return
            if task.status == "已完成":
                InfoBar.success(title="上传完成", content=f"文件 '{task.file_name}' 上传成功", parent=self)
                if task.db_task_id:
                    Database.instance().delete_upload_task(task.db_task_id)
                    task.db_task_id = None
                self.__update_upload_table()
            return
        # ---- 以下为下载逻辑 ----
        if task.status != "已完成":
            return
        task.active_workers = 0
        task.max_workers = 0
        task.speed_bps = 0.0
        task.eta_seconds = -1.0
        task.speed_tracker = None
        Database.instance().delete_download_task(task.resume_id)
        self.__update_download_table()

    def __task_error(self, task, error):
        logger.error("任务错误: %s", error)
        if isinstance(task, DownloadTask):
            task.last_error = error
            task.status = "失败"
            InfoBar.error(title="下载失败", content=f"{task.file_name}: {error}", parent=self)
            return
        task.last_error = error
        task.status = "失败"
        InfoBar.error(title="上传失败", content=f"{task.file_name}: {error}", parent=self)
        if task.db_task_id:
            Database.instance().update_upload_task(
                task.db_task_id,
                status="失败",
                error=error,
                progress=task.progress,
            )
        self.__update_upload_table()

    def __mark_download_failed(self, task, error, notify):
        task.status = "失败"
        task.thread = None
        task.active_workers = 0
        task.max_workers = 0
        task.last_error = error
        Database.instance().update_download_task(
            task.resume_id, status="失败", error=error, progress=task.progress,
        )
        if notify:
            InfoBar.error(title="下载失败", content=f"{task.file_name}: {error}", parent=self)
        self.__update_download_table()

    # ---- pause/resume/retry ----

    def __toggle_pause_upload(self, task):
        if task.status == "已暂停":
            if task.thread is not None:
                return
            task.status = "等待中"
            self.__update_upload_table()
            self.__try_start_pending_uploads()
            return
        if not task.thread:
            return
        task.thread.pause()
        task.status = "已暂停"
        task.active_workers = 0
        task.max_workers = 0
        if task.db_task_id:
            Database.instance().update_upload_task(task.db_task_id, status="已暂停")
        self.__update_upload_table()

    def __retry_upload(self, task):
        if task.thread is not None:
            return
        task.status = "等待中"
        task.is_cancelled = False
        task.pause_requested = False
        task.delete_requested = False
        task.last_error = ""
        task.active_workers = 0
        task.max_workers = 0
        # P1-7: S3 session 有效时保留，复用已上传分片
        has_valid_session = bool(task.bucket and task.upload_id_s3)
        if not has_valid_session:
            self.__reset_upload_session(task, clear_progress=False)
        if task.db_task_id:
            Database.instance().update_upload_task(
                task.db_task_id,
                status="等待中",
                error="",
                progress=task.progress,
            )
        self.__update_upload_table()
        self.__try_start_pending_uploads()

    def __toggle_pause(self, task):
        if task.status == "已暂停":
            if task.thread is not None:
                # 线程仍在退出中，等待 finished 信号后自动变为 thread=None
                return
            if not self.__download_supports_resume(task):
                task.progress = 0
                Database.instance().update_download_task(task.resume_id, progress=0)
            task.status = "等待中"
            self.__update_download_table()
            self.__try_start_pending_downloads()
            return
        if not task.thread:
            return
        task.thread.pause()
        task.status = "已暂停"
        task.active_workers = 0
        task.max_workers = 0
        if not self.__download_supports_resume(task):
            task.progress = 0
        Database.instance().update_download_task(task.resume_id, status="已暂停", progress=task.progress)
        self.__update_download_table()

    def __retry_download(self, task):
        if task.thread is not None:
            return
        if not task.resume_metadata_valid:
            self.__mark_download_failed(task, LEGACY_RESUME_TASK_ERROR, notify=True)
            return
        task.is_cancelled = False
        task.pause_requested = False
        task.delete_requested = False
        task.active_workers = 0
        task.max_workers = 0
        task.last_error = ""
        if not self.__download_supports_resume(task):
            task.progress = 0
        Database.instance().update_download_task(task.resume_id, status="等待中", error="", progress=task.progress)
        task.status = "等待中"
        self.__update_download_table()
        self.__try_start_pending_downloads()

    # ---- remove ----

    def __remove_task(self, task, task_type):
        if task_type == "upload":
            if task.thread:
                task.delete_requested = True
                task.is_cancelled = True
                task.pause_requested = False
                task.status = "已取消"
                if task.db_task_id:
                    Database.instance().update_upload_task(
                        task.db_task_id,
                        status="已取消",
                        delete_requested=1,
                    )
                self.__update_upload_table()
            elif task.db_task_id:
                Database.instance().delete_upload_task(task.db_task_id)
                task.db_task_id = None
                if task in self.upload_tasks:
                    self.upload_tasks.remove(task)
                    self.__update_upload_table()
                self.__try_start_pending_uploads()
            elif task in self.upload_tasks:
                self.upload_tasks.remove(task)
                self.__update_upload_table()
            return
        if task.thread and task.status in {"下载中", "已暂停", "等待中", "校验中", "合并中"}:
            task.delete_requested = True
            task.cleanup_on_cancel = True
            task.status = "已取消"
            Database.instance().update_download_task(
                task.resume_id,
                status="已取消",
                error="用户删除任务",
            )
            task.thread.cancel()
            # 不 disconnect：线程检测 cancel 后发射 status_updated("已取消")
            # → __update_task_status 终态处理 disconnect + deleteLater
        else:
            Database.instance().delete_download_task(task.resume_id)
            cleanup_temp_dir(task.resume_id)
        if not task.delete_requested and task in self.download_tasks:
            self.download_tasks.remove(task)
            self.__update_download_table()
        elif task.delete_requested:
            self.__update_download_table()
        if not task.delete_requested:
            self.__try_start_pending_downloads()

    # ---- batch operations ----

    def __update_batch_bar(self, btns, table):
        count = len(table.selectionModel().selectedRows())
        btns['count'].setText(f"已选 {count} 项")

    @staticmethod
    def __get_selected_tasks(table, tasks):
        rows = sorted({idx.row() for idx in table.selectionModel().selectedRows()})
        return [tasks[r] for r in rows if 0 <= r < len(tasks)]

    @staticmethod
    def __select_all(table, _tasks):
        table.selectAll()

    @staticmethod
    def __invert_selection(table, tasks):
        current = {idx.row() for idx in table.selectionModel().selectedRows()}
        table.blockSignals(True)
        table.clearSelection()
        sel_model = table.selectionModel()
        for i in range(len(tasks)):
            if i not in current:
                idx = table.model().index(i, 0)
                sel_model.select(idx, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        table.blockSignals(False)
        table.itemSelectionChanged.emit()

    def __batch_pause(self, table, tasks, task_type):
        selected = self.__get_selected_tasks(table, tasks)
        if not selected:
            InfoBar.warning(title="提示", content="请先选择任务", parent=self)
            return
        count = 0
        for task in selected:
            if task_type == "upload":
                if task.status in UPLOAD_ACTIVE_STATUSES:
                    self.__toggle_pause_upload(task)
                    count += 1
            else:
                if task.status in DOWNLOAD_ACTIVE_STATUSES:
                    self.__toggle_pause(task)
                    count += 1
        if count:
            InfoBar.success(title="批量暂停", content=f"已暂停 {count} 个任务", parent=self)

    def __batch_resume(self, table, tasks, task_type):
        selected = self.__get_selected_tasks(table, tasks)
        if not selected:
            InfoBar.warning(title="提示", content="请先选择任务", parent=self)
            return
        count = 0
        for task in selected:
            if task_type == "upload":
                if task.status == "已暂停" and task.thread is None:
                    task.status = "等待中"
                    if task.db_task_id:
                        Database.instance().update_upload_task(task.db_task_id, status="等待中")
                    else:
                        task.db_task_id = uuid.uuid4().hex
                        Database.instance().save_upload_task({
                            "task_id": task.db_task_id,
                            "account_name": self.current_account_name,
                            "file_name": task.file_name,
                            "file_size": task.file_size,
                            "local_path": task.local_path,
                            "target_dir_id": task.target_dir_id,
                            "status": "等待中",
                        })
                    count += 1
                elif task.status == "失败" and task.thread is None:
                    self.__retry_upload(task)
                    count += 1
            else:
                if task.status == "已暂停" and task.thread is None:
                    task.status = "等待中"
                    Database.instance().update_download_task(task.resume_id, status="等待中")
                    count += 1
                elif task.status == "失败" and task.thread is None:
                    self.__retry_download(task)
                    count += 1
        if count:
            if task_type == "upload":
                self.__update_upload_table()
                self.__try_start_pending_uploads()
            else:
                self.__update_download_table()
                self.__try_start_pending_downloads()
            InfoBar.success(title="批量继续", content=f"已恢复 {count} 个任务", parent=self)

    def __batch_delete(self, table, tasks, task_type):
        selected = self.__get_selected_tasks(table, tasks)
        if not selected:
            InfoBar.warning(title="提示", content="请先选择任务", parent=self)
            return
        msg = MessageBox("确认批量删除", f"确定要删除选中的 {len(selected)} 个任务吗？", self)
        if not msg.exec():
            return
        self._batch_rebuild_suppressed = True
        try:
            for task in selected:
                self.__remove_task(task, task_type)
        finally:
            self._batch_rebuild_suppressed = False
        if task_type == "upload":
            self.__update_upload_table()
            self.__try_start_pending_uploads()
        else:
            self.__update_download_table()
            self.__try_start_pending_downloads()
        InfoBar.success(title="批量删除", content=f"已删除 {len(selected)} 个任务", parent=self)

    # ---- table helpers ----

    def __set_table_item_text(self, table, row, col, text, alignment=None):
        item = table.item(row, col)
        if item is None:
            item = QTableWidgetItem(text)
            if alignment is not None:
                item.setTextAlignment(alignment)
            table.setItem(row, col, item)
            return item
        if item.text() != text:
            item.setText(text)
        if alignment is not None:
            item.setTextAlignment(alignment)
        return item

    def __bind_button(self, button, handler):
        self.__clear_button_handler(button)
        button.clicked.connect(handler)
        setattr(button, BUTTON_CLICK_HANDLER_ATTR, handler)

    def __clear_button_handler(self, button):
        handler = getattr(button, BUTTON_CLICK_HANDLER_ATTR, None)
        if handler is None:
            return
        try:
            button.clicked.disconnect(handler)
        except (RuntimeError, TypeError):
            pass
        setattr(button, BUTTON_CLICK_HANDLER_ATTR, None)

    def __disable_button(self, button, text):
        button.setText(text)
        button.setEnabled(False)
        self.__clear_button_handler(button)

    def __get_or_create_actions(self, table, row, col):
        w = table.cellWidget(row, col)
        if w is None:
            layout = QHBoxLayout()
            layout.setContentsMargins(4, 0, 4, 0)
            layout.setSpacing(4)
            pb = PushButton(table)
            sb = PushButton(table)
            pb.setFixedSize(80, 24)
            sb.setFixedSize(80, 24)
            layout.addWidget(pb)
            layout.addWidget(sb)
            w = QWidget()
            w.setLayout(layout)
            w.primary_button = pb  # type: ignore[attr-defined]
            w.secondary_button = sb  # type: ignore[attr-defined]
            table.setCellWidget(row, col, w)
        return w

    def __configure_upload_actions(self, row, task):
        w = self.__get_or_create_actions(self.uploadTable, row, COL_ACTION)
        pb, sb = w.primary_button, w.secondary_button
        if task.status in UPLOAD_ACTIVE_STATUSES:
            pb.setIcon(FIF.PAUSE.icon())
            pb.setText("暂停")
            pb.setEnabled(True)
            self.__bind_button(pb, lambda _, t=task: self.__toggle_pause_upload(t))
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("取消")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "upload"))
        elif task.status == "已暂停":
            pb.setIcon(FIF.SYNC.icon())
            pb.setText("继续")
            pb.setEnabled(True)
            self.__bind_button(pb, lambda _, t=task: self.__toggle_pause_upload(t))
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("取消")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "upload"))
        elif task.status == "失败":
            pb.setIcon(FIF.SYNC.icon())
            pb.setText("重试")
            pb.setEnabled(True)
            self.__bind_button(pb, lambda _, t=task: self.__retry_upload(t))
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("删除")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "upload"))
        else:
            self.__disable_button(pb, "")
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("删除")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "upload"))

    def __configure_download_actions(self, row, task):
        w = self.__get_or_create_actions(self.downloadTable, row, COL_ACTION)
        pb, sb = w.primary_button, w.secondary_button
        if task.status in DOWNLOAD_ACTIVE_STATUSES:
            pb.setIcon(FIF.PAUSE.icon())
            pb.setText("暂停")
            pb.setEnabled(True)
            self.__bind_button(pb, lambda _, t=task: self.__toggle_pause(t))
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("取消")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "download"))
        elif task.status == "已暂停":
            pb.setIcon(FIF.SYNC.icon())
            pb.setText("继续")
            pb.setEnabled(True)
            self.__bind_button(pb, lambda _, t=task: self.__toggle_pause(t))
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("取消")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "download"))
        elif task.status == "失败":
            pb.setIcon(FIF.SYNC.icon())
            pb.setText("重试")
            pb.setEnabled(True)
            self.__bind_button(pb, lambda _, t=task: self.__retry_download(t))
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("删除")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "download"))
        else:
            self.__disable_button(pb, task.status)
            sb.setIcon(FIF.DELETE.icon())
            sb.setText("删除")
            sb.setEnabled(True)
            self.__bind_button(sb, lambda _, t=task: self.__remove_task(t, "download"))

    # ---- selection persistence ----

    @staticmethod
    def __save_selection(table):
        """从表格 COL_NAME item 的 UserRole 读取选中行 task id 集合。"""
        saved = set()
        for idx in table.selectionModel().selectedRows():
            item = table.item(idx.row(), COL_NAME)
            if item:
                tid = item.data(Qt.ItemDataRole.UserRole)
                if tid:
                    saved.add(tid)
        return saved

    @staticmethod
    def __restore_selection(table, tasks, saved):
        """根据 saved 集合恢复选中行。"""
        if not saved:
            return
        table.blockSignals(True)
        sel_model = table.selectionModel()
        for row, task in enumerate(tasks):
            tid = getattr(task, 'db_task_id', None) or getattr(task, 'resume_id', None)
            if tid and tid in saved:
                idx = table.model().index(row, 0)
                sel_model.select(idx, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        table.blockSignals(False)

    @staticmethod
    def __find_task_row(task, visible_tasks):
        """在可见任务列表中找到 task 对应行号，返回 -1 表示不可见。"""
        for i, t in enumerate(visible_tasks):
            if t is task:
                return i
        return -1

    def __refresh_task_cells(self, table, row, task):
        """局部刷新：只更新 progress/speed/ETA/conn 四列，不动结构/选中/action 按钮。"""
        if row < 0 or row >= table.rowCount():
            return
        center = Qt.AlignmentFlag.AlignCenter
        self.__set_table_item_text(table, row, COL_PERCENT, f"{task.progress}%", center)
        if task.status == "已完成" and task.finish_avg_speed > 0:
            self.__set_table_item_text(table, row, COL_SPEED, f"均速 {format_speed(task.finish_avg_speed)}", center)
        else:
            self.__set_table_item_text(table, row, COL_SPEED, format_speed(task.speed_bps), center)
        if task.status == "已完成" and task.finish_duration > 0:
            self.__set_table_item_text(table, row, COL_ETA, f"耗时 {format_eta(task.finish_duration)}", center)
        else:
            self.__set_table_item_text(table, row, COL_ETA, format_eta(task.eta_seconds), center)
        conn = f"{task.active_workers}/{task.max_workers}" if task.active_workers or task.max_workers else "-"
        self.__set_table_item_text(table, row, COL_CONN, conn, center)

    # ---- table updates ----

    def __update_upload_table(self):
        if getattr(self, '_batch_rebuild_suppressed', False):
            return
        sel = self.__save_selection(self.uploadTable)
        visible = self.__get_filtered_upload_tasks()
        self.uploadTable.setRowCount(len(visible))
        center = Qt.AlignmentFlag.AlignCenter
        for row, task in enumerate(visible):
            self.__set_table_item_text(self.uploadTable, row, COL_NAME, task.file_name or "(未知)")
            item = self.uploadTable.item(row, COL_NAME)
            if item:
                item.setData(Qt.ItemDataRole.UserRole, task.db_task_id or "")
            self.__set_table_item_text(self.uploadTable, row, COL_SIZE, format_file_size(task.file_size))
            self.__set_table_item_text(self.uploadTable, row, COL_PERCENT, f"{task.progress}%", center)
            if task.status == "已完成" and task.finish_avg_speed > 0:
                self.__set_table_item_text(self.uploadTable, row, COL_SPEED, f"均速 {format_speed(task.finish_avg_speed)}", center)
            else:
                self.__set_table_item_text(self.uploadTable, row, COL_SPEED, format_speed(task.speed_bps), center)
            if task.status == "已完成" and task.finish_duration > 0:
                self.__set_table_item_text(self.uploadTable, row, COL_ETA, f"耗时 {format_eta(task.finish_duration)}", center)
            else:
                self.__set_table_item_text(self.uploadTable, row, COL_ETA, format_eta(task.eta_seconds), center)
            self.__set_table_item_text(self.uploadTable, row, COL_STATUS, task.status)
            conn = f"{task.active_workers}/{task.max_workers}" if task.active_workers or task.max_workers else "-"
            self.__set_table_item_text(self.uploadTable, row, COL_CONN, conn, center)
            self.__configure_upload_actions(row, task)
        self.__restore_selection(self.uploadTable, visible, sel)

    def __update_download_table(self):
        if getattr(self, '_batch_rebuild_suppressed', False):
            return
        sel = self.__save_selection(self.downloadTable)
        visible = self.__get_filtered_download_tasks()
        self.downloadTable.setRowCount(len(visible))
        center = Qt.AlignmentFlag.AlignCenter
        for row, task in enumerate(visible):
            self.__set_table_item_text(self.downloadTable, row, COL_NAME, task.file_name or "(未知)")
            item = self.downloadTable.item(row, COL_NAME)
            if item:
                item.setData(Qt.ItemDataRole.UserRole, task.resume_id or "")
            self.__set_table_item_text(self.downloadTable, row, COL_SIZE, format_file_size(task.file_size))
            self.__set_table_item_text(self.downloadTable, row, COL_PERCENT, f"{task.progress}%", center)
            if task.status == "已完成" and task.finish_avg_speed > 0:
                self.__set_table_item_text(self.downloadTable, row, COL_SPEED, f"均速 {format_speed(task.finish_avg_speed)}", center)
            else:
                self.__set_table_item_text(self.downloadTable, row, COL_SPEED, format_speed(task.speed_bps), center)
            if task.status == "已完成" and task.finish_duration > 0:
                self.__set_table_item_text(self.downloadTable, row, COL_ETA, f"耗时 {format_eta(task.finish_duration)}", center)
            else:
                self.__set_table_item_text(self.downloadTable, row, COL_ETA, format_eta(task.eta_seconds), center)
            self.__set_table_item_text(self.downloadTable, row, COL_STATUS, task.status)
            conn = f"{task.active_workers}/{task.max_workers}" if task.active_workers or task.max_workers else "-"
            self.__set_table_item_text(self.downloadTable, row, COL_CONN, conn, center)
            self.__configure_download_actions(row, task)
        self.__restore_selection(self.downloadTable, visible, sel)

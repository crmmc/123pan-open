from pathlib import Path

import pytest

from src.app.common.api import format_file_size, FileDataManager, TransferTask


class TestFormatFileSize:
    def test_bytes(self):
        assert format_file_size(0) == "0 B"
        assert format_file_size(512) == "512 B"
        assert format_file_size(1023) == "1023 B"

    def test_kilobytes(self):
        assert format_file_size(1025) == "1.0 KB"
        assert format_file_size(1536) == "1.5 KB"

    def test_megabytes(self):
        assert format_file_size(1048577) == "1.0 MB"
        assert format_file_size(5242880) == "5.0 MB"

    def test_gigabytes(self):
        assert format_file_size(1073741825) == "1.0 GB"
        assert format_file_size(10737418240) == "10.0 GB"

    def test_large_values(self):
        result = format_file_size(1073741824 * 100)  # 100 GB
        assert "GB" in result


class TestTransferTask:
    def test_init(self):
        task = TransferTask(0, "下载", "test.txt", 1024)
        assert task.id == 0
        assert task.type == "下载"
        assert task.name == "test.txt"
        assert task.size == 1024
        assert task.progress == 0
        assert task.status == "等待中"
        assert task.file_path is None

    def test_to_dict(self):
        task = TransferTask(1, "上传", "a.zip", 2048)
        task.progress = 50
        task.status = "上传中"
        d = task.to_dict()
        assert d == {
            "id": 1,
            "type": "上传",
            "name": "a.zip",
            "size": 2048,
            "progress": 50,
            "status": "上传中",
            "file_path": None,
        }


class TestFileDataManager:
    def test_get_file_type_name(self):
        assert FileDataManager.get_file_type_name(1) == "文件夹"
        assert FileDataManager.get_file_type_name(0) == "文件"
        assert FileDataManager.get_file_type_name(2) == "文件"

    def test_format_file_size_value(self):
        assert FileDataManager.format_file_size_value(1025) == "1.0 KB"
        assert FileDataManager.format_file_size_value(0) == "0 B"

    def test_get_file_extension(self):
        assert FileDataManager.get_file_extension("test.txt") == ".txt"
        assert FileDataManager.get_file_extension("archive.tar.gz") == ".gz"
        assert FileDataManager.get_file_extension("noext") == ""
        assert FileDataManager.get_file_extension(".hidden") == ""
        assert FileDataManager.get_file_extension("UPPER.TXT") == ".txt"

    def test_validate_file_exists(self, tmp_path):
        real_file = tmp_path / "exists.txt"
        real_file.write_text("hello")
        assert FileDataManager.validate_file_exists(str(real_file)) is True
        assert FileDataManager.validate_file_exists(str(tmp_path / "nope.txt")) is False
        assert FileDataManager.validate_file_exists(str(tmp_path)) is False  # 目录不算文件

    def test_is_duplicate_filename(self):
        class FakePan:
            list = [
                {"FileName": "a.txt"},
                {"FileName": "b.txt"},
            ]
        assert FileDataManager.is_duplicate_filename(FakePan(), "a.txt") is True
        assert FileDataManager.is_duplicate_filename(FakePan(), "c.txt") is False

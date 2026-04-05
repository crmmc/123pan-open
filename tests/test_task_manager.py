import threading

import pytest

from src.app.common.api import TransferTaskManager, TransferTask


@pytest.fixture
def manager():
    return TransferTaskManager()


class TestCreateTask:
    def test_create_task_returns_incrementing_ids(self, manager):
        id1 = manager.create_task("下载", "file1.txt", 1024)
        id2 = manager.create_task("上传", "file2.txt", 2048)
        assert id1 == 0
        assert id2 == 1

    def test_created_task_has_correct_fields(self, manager):
        task_id = manager.create_task("下载", "test.zip", 4096)
        task = manager.get_task(task_id)
        assert task.type == "下载"
        assert task.name == "test.zip"
        assert task.size == 4096
        assert task.progress == 0
        assert task.status == "等待中"


class TestGetTask:
    def test_get_existing_task(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        task = manager.get_task(task_id)
        assert task is not None
        assert task.name == "f.txt"

    def test_get_nonexistent_task(self, manager):
        assert manager.get_task(999) is None


class TestUpdateTaskProgress:
    def test_update_progress_clamps_range(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        manager.update_task_progress(task_id, 50)
        assert manager.get_task(task_id).progress == 50

        manager.update_task_progress(task_id, -10)
        assert manager.get_task(task_id).progress == 0

        manager.update_task_progress(task_id, 200)
        assert manager.get_task(task_id).progress == 100

    def test_update_progress_nonexistent(self, manager):
        manager.update_task_progress(999, 50)  # 不应抛异常


class TestUpdateTaskStatus:
    def test_update_status(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        manager.update_task_status(task_id, "下载中")
        assert manager.get_task(task_id).status == "下载中"

    def test_update_status_nonexistent(self, manager):
        manager.update_task_status(999, "下载中")  # 不应抛异常


class TestUpdateTask:
    def test_update_both(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        manager.update_task(task_id, progress=75, status="下载中")
        task = manager.get_task(task_id)
        assert task.progress == 75
        assert task.status == "下载中"

    def test_update_only_progress(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        manager.update_task(task_id, progress=30)
        task = manager.get_task(task_id)
        assert task.progress == 30
        assert task.status == "等待中"

    def test_update_only_status(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        manager.update_task(task_id, status="已完成")
        assert manager.get_task(task_id).status == "已完成"
        assert manager.get_task(task_id).progress == 0


class TestRemoveTask:
    def test_remove_existing(self, manager):
        task_id = manager.create_task("下载", "f.txt", 0)
        assert manager.remove_task(task_id) is True
        assert manager.get_task(task_id) is None

    def test_remove_nonexistent(self, manager):
        assert manager.remove_task(999) is False


class TestGetAllTasks:
    def test_get_all_empty(self, manager):
        assert manager.get_all_tasks() == []

    def test_get_all_returns_all(self, manager):
        manager.create_task("下载", "a.txt", 0)
        manager.create_task("上传", "b.txt", 0)
        assert len(manager.get_all_tasks()) == 2


class TestClearCompletedTasks:
    def test_clear_removes_completed(self, manager):
        id1 = manager.create_task("下载", "a.txt", 0)
        id2 = manager.create_task("下载", "b.txt", 0)
        id3 = manager.create_task("下载", "c.txt", 0)
        manager.update_task_status(id1, "已完成")
        manager.update_task_status(id2, "已取消")
        manager.update_task_status(id3, "失败")
        manager.clear_completed_tasks()
        assert len(manager.get_all_tasks()) == 0

    def test_clear_keeps_active(self, manager):
        id1 = manager.create_task("下载", "a.txt", 0)
        id2 = manager.create_task("下载", "b.txt", 0)
        manager.update_task_status(id1, "下载中")
        manager.update_task_status(id2, "已完成")
        manager.clear_completed_tasks()
        assert len(manager.get_all_tasks()) == 1
        assert manager.get_task(id1) is not None


class TestThreadSafety:
    def test_concurrent_creates(self, manager):
        ids = []
        errors = []

        def create_many():
            try:
                for _ in range(100):
                    ids.append(manager.create_task("下载", "f.txt", 0))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(ids) == 500
        assert len(set(ids)) == 500  # 所有 ID 唯一

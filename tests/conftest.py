"""测试夹具：每个测试使用独立的临时 SQLite 库，并种入入职示例。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    from app import main as main_module

    db_file = str(tmp_path / "test_drill.db")
    main_module.app.state.db_path = db_file
    main_module.init_app(db_file, seed_demo=True)

    with TestClient(main_module.app) as c:  # lifespan 会检测到示例已存在，不重复种入
        c.main = main_module
        yield c

    main_module.app.state.db_path = None

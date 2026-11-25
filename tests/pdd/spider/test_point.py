import argparse
import json
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from PIL import Image
from psycopg.rows import tuple_row

from automation.pdd.spider import point
from automation.pdd.spider.mobile_database import Store, connect


def options(**kwargs):
    values = dict(base_url=None, model=None, batch=6, max_images=36, max_edge=1280,
                  quality=80, timeout=180, retries=2, product=None, ranking=None,
                  force=False, only=None, limit=None, no_copy=False, dry_run=False)
    return argparse.Namespace(**(values | kwargs))


def result():
    return {"product_name": "产品", "main_selling_points": [
        {"point": "柔软亲肤", "image_evidence": [
            {"image_type": "main", "file": "001.png", "quote": "柔软亲肤"}]}],
        "secondary_selling_points": []}


def test_deepseek_configuration(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "VISION_MODEL", "TEXT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    cfg = point.Config(options())
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.vision_model == cfg.text_model == "deepseek-flash"
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        cfg.check()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("TEXT_MODEL", "merge-model")
    cfg = point.Config(options(model="vision-model", base_url="https://example.com/v1/"))
    cfg.check()
    assert cfg.base_url == "https://example.com/v1"
    assert cfg.vision_model == "vision-model"
    assert cfg.text_model == "merge-model"


def test_selection_filters_both_ids_and_resolves_business_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(point.spider_config, "BUSINESS_ROOT", tmp_path)
    cur = Mock()
    cur.fetchall.return_value = [(55, 31, "123", "data/榜01", "产品", False)]
    products = point.select_products(cur, options(product=55, ranking=31))
    sql, params = cur.execute.call_args.args
    assert "rp.id = %s" in sql and "rp.ranking_id = %s" in sql and "sp.id is null" in sql
    assert params == [55, 31]
    assert products[0]["directory"] == str(tmp_path / "data/榜01")


def test_extract_images_repair_and_json_output(tmp_path, monkeypatch):
    directory = tmp_path / "商品"
    (directory / "主图").mkdir(parents=True)
    Image.new("RGB", (20, 10)).save(directory / "主图/001.png")
    Image.new("RGB", (20, 10)).save(directory / "主图/长图.png")
    cfg = point.Config(options())
    claims = {"claims": [{"kind": "main", "point": "柔软亲肤", "image_type": "main",
                           "file": "001.png", "quote": "柔软亲肤"}]}
    bad = result()
    bad["main_selling_points"][0]["point"] = "超过十个字的卖点需要重新修正"
    chat = Mock(side_effect=[json.dumps(claims), json.dumps(bad), json.dumps(result())])
    monkeypatch.setattr(point, "chat", chat)
    status, data, path = point.extract_one(cfg, {"directory": str(directory), "folder": "榜01"}, str(tmp_path / "out"))
    assert status == "ok" and data == result()
    assert json.loads((tmp_path / "out/榜01.json").read_text(encoding="utf-8")) == result()
    content = chat.call_args_list[0].args[2]
    assert len(content) == 3  # Long image was excluded.
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_invalid_evidence_cannot_pass_validation(tmp_path):
    data = result()
    evidence = data["main_selling_points"][0]["image_evidence"][0]
    evidence.update(file="../outside.png", quote="")
    errors = point.validate(data, str(tmp_path))
    assert any("file missing" in e for e in errors)
    assert any("empty quote" in e for e in errors)
    assert point.validate({}, str(tmp_path))


def test_dry_run_never_writes_database(tmp_path, monkeypatch):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(point, "connect", Mock(return_value=connection))
    monkeypatch.setattr(point, "extract_one", Mock(return_value=("ok", result(), "result.json")))
    save = Mock()
    monkeypatch.setattr(point, "upsert", save)
    rc, _ = point.run_product(point.Config(options()),
        {"id": 55, "ranking": 31, "folder": "榜01"}, options(dry_run=True, no_copy=True, out_root=str(tmp_path)))
    assert rc == 0
    save.assert_not_called()


def test_write_serializes_on_parent_and_updates_existing_result():
    cur = Mock()
    cur.connection.transaction.return_value = nullcontext()
    cur.fetchone.side_effect = [(55,), (8,)]
    point.upsert(cur, {"id": 55, "pid": "123"}, result())
    calls = cur.execute.call_args_list
    assert "FOR UPDATE" in calls[0].args[0]
    assert "UPDATE ods.t_pdd_selling_point" in calls[2].args[0]
    assert calls[2].args[1][:3] == ("123", "柔软亲肤", "")


def test_force_reextracts_instead_of_copying_and_invalid_result_does_not_write(tmp_path, monkeypatch):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(point, "connect", Mock(return_value=connection))
    extract = Mock(return_value=("invalid", result(), "bad evidence"))
    monkeypatch.setattr(point, "extract_one", extract)
    copy = Mock()
    monkeypatch.setattr(point, "find_copy", copy)
    save = Mock()
    monkeypatch.setattr(point, "upsert", save)
    rc, _ = point.run_product(point.Config(options()),
        {"id": 55, "ranking": 31, "folder": "榜01"}, options(force=True, out_root=str(tmp_path)))
    assert rc == 1
    extract.assert_called_once()
    copy.assert_not_called()
    save.assert_not_called()


def test_chat_uses_configured_endpoint_and_authorization(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    cfg = point.Config(options())
    response = Mock()
    response.read.return_value = b'{"choices":[{"message":{"content":"{}"}}]}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr(point.urllib.request, "urlopen", request)
    assert point.chat(cfg, cfg.vision_model, [{"type": "text", "text": "test"}]) == "{}"
    req = request.call_args.args[0]
    assert req.full_url == "https://api.deepseek.com/chat/completions"
    assert req.get_header("Authorization") == "Bearer test-key"
    assert json.loads(req.data)["model"] == "deepseek-flash"


@pytest.mark.parametrize("flag", ["--ranking-id", "--ranking-product-id", "--batch"])
def test_invalid_arguments_fail_before_database_connection(flag, monkeypatch):
    connect_mock = Mock()
    monkeypatch.setattr(point, "connect", connect_mock)
    with pytest.raises(SystemExit) as error:
        point.main([flag, "0"])
    assert error.value.code == 2
    connect_mock.assert_not_called()


def test_postgres_write_copy_and_force_selection(pg_database, tmp_path):
    store = Store()
    try:
        entry = store.seed_rankings('2026-11-10', [dict(name='来源', main_name='主榜', tab='畅销榜',
            url='https://example.com/rank', tab_index=0)], tmp_path / 'data')[0]
        store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '产品'}, {'排行': 2, '标题': '同款'}])
        store.db.execute("UPDATE t_pdd_ranking_products SET platform_product_id='123' WHERE ranking_id=%s", (entry['id'],))
        with connect() as db:
            cur = db.cursor(row_factory=tuple_row)
            selected = point.select_products(cur, options(ranking=entry['id']))
            point.upsert(cur, selected[0], result())
            point.upsert(cur, selected[0], result())
            source = point.find_copy(cur, '123')
            point.insert_copy(cur, selected[1], source)
            assert not point.select_products(cur, options(ranking=entry['id']))
            assert len(point.select_products(cur, options(ranking=entry['id'], force=True))) == 2
        assert store.db.execute('SELECT count(*) AS n FROM t_pdd_selling_point').fetchone()['n'] == 2
    finally:
        store.close()
